"""
darwin-runner Lambda function (Python).

Deployed as a container image to AWS Lambda in each supported region.
One function per language: darwin-runner-python-{region},
darwin-runner-node-{region}. The substrate adapter
(darwin.agenticcloud.substrate.aws_lambda.LambdaSubstrate) invokes
this function via aiobotocore.

This file is the Lambda handler. It is NOT imported by the rest of
the codebase — it is packaged into a container image and shipped
to AWS. Tests for the schema it consumes/produces live alongside the
substrate adapter (tests/substrate/test_aws_lambda.py).

Schema contract (shared with the substrate adapter):
    darwin.agenticcloud.substrate.aws_lambda_event
        → EVENT_SCHEMA_URI, RunnerEvent, RunnerResponse

Execution model:
    1. Lambda invokes `lambda_handler(event, context)`.
    2. We validate the event payload (domain separator, language,
       size cap).
    3. We materialize the workload code into /tmp/workload.{py,js}.
    4. We subprocess.run() the interpreter against the file with a
       timeout matching the workload's declared timeout_sec.
    5. We capture stdout, stderr, exit_code, wall time.
    6. We compute SHA-256 hashes of stdout and stderr.
    7. We return a RunnerResponse with everything the substrate
       adapter needs to build the v0.2 evidence dict.

Failure modes:
    - Event validation fails → raise (Lambda surfaces as FunctionError).
    - Workload exits non-zero → status="error", exit_code populated.
    - Workload exceeds timeout → status="timeout", exit_code=None.
    - Workload OOMs → Lambda kills the function before this code
      returns. The substrate adapter sees Lambda's FunctionError and
      surfaces it as LambdaInvocationFailed (substrate-side handling).

Cold start: pulled image from ECR. Warm starts reuse the container
and skip the first-import latency.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
import traceback
from typing import Any

# ============================================================================
# Schema contract (kept in lockstep with darwin.agenticcloud.substrate.aws_lambda_event)
# ============================================================================
#
# We do NOT import the substrate module here — the Lambda image
# shouldn\'t carry the entire darwin package. Instead we duplicate the
# small set of constants and validators we need. Drift is caught by
# tests/substrate/test_aws_lambda.py which uses the canonical module.

EVENT_SCHEMA_URI = "darwin.cloud/event/aws-lambda-runner/v1"

SUPPORTED_LANGUAGES = {"python", "node"}

MAX_CODE_BYTES = 192 * 1024
MIN_MEMORY_MB = 128
MAX_MEMORY_MB = 10240
MAX_TIMEOUT_SEC = 900

LANGUAGE_INTERPRETER = {
    "python": ["python3", "/tmp/workload.py"],
    "node": ["node", "/tmp/workload.js"],
}

LANGUAGE_FILENAME = {
    "python": "workload.py",
    "node": "workload.js",
}


class RunnerEventError(Exception):
    """Raised when the incoming event payload is malformed."""


# ============================================================================
# Event validation
# ============================================================================


def _validate_event(event: dict[str, Any]) -> None:
    """Replica of darwin.agenticcloud.substrate.aws_lambda_event.validate_event_dict."""
    if not isinstance(event, dict):
        raise RunnerEventError("event must be a JSON object")
    required = {
        "schema",
        "request_id",
        "workload_id",
        "language",
        "code",
        "timeout_sec",
        "memory_mb",
    }
    missing = required - event.keys()
    if missing:
        raise RunnerEventError(f"event missing fields: {sorted(missing)}")
    if event["schema"] != EVENT_SCHEMA_URI:
        raise RunnerEventError(
            f"event schema mismatch: got {event['schema']!r}, expected {EVENT_SCHEMA_URI!r}"
        )
    if event["language"] not in SUPPORTED_LANGUAGES:
        raise RunnerEventError(f"event language not supported: {event['language']!r}")
    code = event["code"]
    if not isinstance(code, str) or len(code) == 0:
        raise RunnerEventError("event.code must be a non-empty string")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise RunnerEventError(f"event.code exceeds {MAX_CODE_BYTES} bytes")
    timeout = event["timeout_sec"]
    if not isinstance(timeout, int) or timeout < 1 or timeout > MAX_TIMEOUT_SEC:
        raise RunnerEventError(f"event.timeout_sec must be 1..{MAX_TIMEOUT_SEC}, got {timeout!r}")
    memory = event["memory_mb"]
    if not isinstance(memory, int) or memory < MIN_MEMORY_MB or memory > MAX_MEMORY_MB:
        raise RunnerEventError(
            f"event.memory_mb must be {MIN_MEMORY_MB}..{MAX_MEMORY_MB}, got {memory!r}"
        )


# ============================================================================
# Execution
# ============================================================================


def _execute_workload(
    *,
    language: str,
    code: str,
    timeout_sec: int,
) -> dict[str, Any]:
    """Run the workload and return raw execution metrics.

    Writes code to /tmp/workload.{ext}, runs the appropriate
    interpreter, captures stdout/stderr/exit_code, returns a dict
    that maps directly onto the RunnerResponse fields.
    """
    filename = LANGUAGE_FILENAME[language]
    file_path = f"/tmp/{filename}"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(code)

    interpreter = LANGUAGE_INTERPRETER[language]
    started_at = time.time()

    try:
        proc = subprocess.run(
            interpreter,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        ended_at = time.time()
        status = "ok" if proc.returncode == 0 else "error"
        exit_code: int | None = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
        error = None
    except subprocess.TimeoutExpired as e:
        ended_at = time.time()
        status = "timeout"
        exit_code = None
        stdout = (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        stderr = (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        error = f"timeout after {timeout_sec}s"
    except Exception as e:
        ended_at = time.time()
        status = "error"
        exit_code = None
        stdout = ""
        stderr = traceback.format_exc()
        error = f"{type(e).__name__}: {e}"

    wall_time_sec = ended_at - started_at
    stdout_hash = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
    stderr_hash = hashlib.sha256(stderr.encode("utf-8")).hexdigest()

    return {
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": ended_at,
        "wall_time_sec": wall_time_sec,
        "output_hash": stdout_hash,
        "stderr_hash": stderr_hash,
        "error": error,
    }


# ============================================================================
# Lambda handler
# ============================================================================


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point.

    `context` is the AWS Lambda context object — we use it for
    aws_request_id and remaining time. `event` is the RunnerEvent
    payload from the substrate adapter.
    """
    # 1. Validate. Validation failures raise so Lambda surfaces as
    #    FunctionError, which the substrate adapter handles.
    _validate_event(event)

    request_id = event["request_id"]
    workload_id = event["workload_id"]
    language = event["language"]
    code = event["code"]
    timeout_sec = event["timeout_sec"]

    # Leave a safety margin so we return BEFORE Lambda kills us.
    # Lambda will hard-kill the function at its configured timeout;
    # we want to surface a clean "timeout" status with full evidence
    # rather than a Lambda-level FunctionError.
    effective_timeout = min(timeout_sec, MAX_TIMEOUT_SEC - 5)

    exec_result = _execute_workload(
        language=language,
        code=code,
        timeout_sec=effective_timeout,
    )

    response = {
        "schema": EVENT_SCHEMA_URI,
        "request_id": request_id,
        "workload_id": workload_id,
        "status": exec_result["status"],
        "stdout": exec_result["stdout"],
        "stderr": exec_result["stderr"],
        "exit_code": exec_result["exit_code"],
        "started_at": exec_result["started_at"],
        "ended_at": exec_result["ended_at"],
        "wall_time_sec": exec_result["wall_time_sec"],
        "output_hash": exec_result["output_hash"],
        "stderr_hash": exec_result["stderr_hash"],
    }
    if exec_result["error"] is not None:
        response["error"] = exec_result["error"]
    return response
