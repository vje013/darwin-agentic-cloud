"""
darwin.agenticcloud.substrate.aws_lambda_event
==============================================

Shared contract for the event payload sent to the darwin-runner Lambda.

This module is imported by TWO different deployment artifacts:

1. `darwin.agenticcloud.substrate.aws_lambda.LambdaSubstrate` (this repo,
   client side) constructs the event and invokes the Lambda.

2. `darwin-runner` Lambda function (step 3b, AWS deployment artifact)
   parses the event and executes the workload.

Keeping the schema in ONE file that BOTH import is intentional. If the
contract drifts, attestations silently break and we have no test signal
until production. By co-locating the schema, any version bump shows up
in code review on both sides at once.

Schema URI: darwin.cloud/event/aws-lambda-runner/v1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ============================================================================
# Constants
# ============================================================================

#: Schema URI for the runner-event payload. Bumped when the event shape
#: changes in a way that breaks runner compatibility.
EVENT_SCHEMA_URI: str = "darwin.cloud/event/aws-lambda-runner/v1"

#: Supported workload languages. Each maps to a runner image
#: (darwin-runner-python, darwin-runner-node). The substrate refuses
#: workloads outside this set.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"python", "node"})

#: Maximum workload code size accepted by the runner. Lambda's event
#: payload limit is 256 KB but base64 encoding plus overhead means we
#: cap at 192 KB raw to leave headroom.
MAX_CODE_BYTES: int = 192 * 1024

#: Minimum Lambda memory in MB. Lambda allows 128 MB minimum.
MIN_MEMORY_MB: int = 128

#: Maximum Lambda memory in MB. Lambda allows up to 10240 MB.
MAX_MEMORY_MB: int = 10240

#: Maximum Lambda timeout in seconds. Lambda's hard limit is 900s.
MAX_TIMEOUT_SEC: int = 900


# ============================================================================
# Errors
# ============================================================================


class RunnerEventError(Exception):
    """Raised when a runner-event payload is malformed."""


# ============================================================================
# Schema
# ============================================================================


@dataclass(frozen=True)
class RunnerEvent:
    """The event payload sent to the darwin-runner Lambda.

    Always serialized via `to_dict()` / parsed via `from_dict()` so both
    sides go through validation. Never directly serialized to JSON by
    the caller.
    """

    schema: str
    request_id: str
    workload_id: str
    language: str
    code: str
    timeout_sec: int
    memory_mb: int
    inputs: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize for transmission as the Lambda invocation payload."""
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "workload_id": self.workload_id,
            "language": self.language,
            "code": self.code,
            "timeout_sec": self.timeout_sec,
            "memory_mb": self.memory_mb,
            "inputs": self.inputs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunnerEvent:
        """Parse and validate an incoming event payload (runner side)."""
        validate_event_dict(data)
        return cls(
            schema=data["schema"],
            request_id=data["request_id"],
            workload_id=data["workload_id"],
            language=data["language"],
            code=data["code"],
            timeout_sec=int(data["timeout_sec"]),
            memory_mb=int(data["memory_mb"]),
            inputs=dict(data.get("inputs", {})),
        )


@dataclass(frozen=True)
class RunnerResponse:
    """The response the darwin-runner returns to the substrate.

    Mirrors the v0.2 evidence shape so the substrate can pass it through
    to the attestation builder with minimal translation.
    """

    schema: str
    request_id: str
    workload_id: str
    status: str  # "ok" | "error" | "timeout" | "oom"
    stdout: str
    stderr: str
    exit_code: int | None
    started_at: float
    ended_at: float
    wall_time_sec: float
    output_hash: str
    stderr_hash: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema": self.schema,
            "request_id": self.request_id,
            "workload_id": self.workload_id,
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "wall_time_sec": self.wall_time_sec,
            "output_hash": self.output_hash,
            "stderr_hash": self.stderr_hash,
        }
        if self.error is not None:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunnerResponse:
        validate_response_dict(data)
        return cls(
            schema=data["schema"],
            request_id=data["request_id"],
            workload_id=data["workload_id"],
            status=data["status"],
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            exit_code=data.get("exit_code"),
            started_at=float(data["started_at"]),
            ended_at=float(data["ended_at"]),
            wall_time_sec=float(data["wall_time_sec"]),
            output_hash=data["output_hash"],
            stderr_hash=data["stderr_hash"],
            error=data.get("error"),
        )


# ============================================================================
# Validators
# ============================================================================


_REQUIRED_EVENT_FIELDS: frozenset[str] = frozenset(
    {
        "schema",
        "request_id",
        "workload_id",
        "language",
        "code",
        "timeout_sec",
        "memory_mb",
    }
)


_REQUIRED_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {
        "schema",
        "request_id",
        "workload_id",
        "status",
        "started_at",
        "ended_at",
        "wall_time_sec",
        "output_hash",
        "stderr_hash",
    }
)


_VALID_STATUSES: frozenset[str] = frozenset({"ok", "error", "timeout", "oom"})


def validate_event_dict(data: dict[str, Any]) -> None:
    """Validate an event payload. Raises RunnerEventError on any issue."""
    if not isinstance(data, dict):
        raise RunnerEventError("event must be a JSON object")
    missing = _REQUIRED_EVENT_FIELDS - data.keys()
    if missing:
        raise RunnerEventError(f"event missing required fields: {sorted(missing)}")
    if data["schema"] != EVENT_SCHEMA_URI:
        raise RunnerEventError(
            f"event schema mismatch: expected {EVENT_SCHEMA_URI!r}, got {data['schema']!r}"
        )
    if data["language"] not in SUPPORTED_LANGUAGES:
        raise RunnerEventError(
            f"event language must be one of {sorted(SUPPORTED_LANGUAGES)}, got {data['language']!r}"
        )
    code = data["code"]
    if not isinstance(code, str):
        raise RunnerEventError("event.code must be a string")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise RunnerEventError(
            f"event.code exceeds max size: {len(code.encode('utf-8'))} > {MAX_CODE_BYTES}"
        )
    if len(code) == 0:
        raise RunnerEventError("event.code must not be empty")
    timeout = data["timeout_sec"]
    if not isinstance(timeout, int) or timeout < 1 or timeout > MAX_TIMEOUT_SEC:
        raise RunnerEventError(f"event.timeout_sec must be 1..{MAX_TIMEOUT_SEC}, got {timeout!r}")
    memory = data["memory_mb"]
    if not isinstance(memory, int) or memory < MIN_MEMORY_MB or memory > MAX_MEMORY_MB:
        raise RunnerEventError(
            f"event.memory_mb must be {MIN_MEMORY_MB}..{MAX_MEMORY_MB}, got {memory!r}"
        )


def validate_response_dict(data: dict[str, Any]) -> None:
    """Validate a runner response. Raises RunnerEventError on any issue."""
    if not isinstance(data, dict):
        raise RunnerEventError("response must be a JSON object")
    missing = _REQUIRED_RESPONSE_FIELDS - data.keys()
    if missing:
        raise RunnerEventError(f"response missing required fields: {sorted(missing)}")
    if data["schema"] != EVENT_SCHEMA_URI:
        raise RunnerEventError(
            f"response schema mismatch: expected {EVENT_SCHEMA_URI!r}, got {data['schema']!r}"
        )
    if data["status"] not in _VALID_STATUSES:
        raise RunnerEventError(
            f"response.status must be one of {sorted(_VALID_STATUSES)}, got {data['status']!r}"
        )


__all__ = [
    "EVENT_SCHEMA_URI",
    "MAX_CODE_BYTES",
    "MAX_MEMORY_MB",
    "MAX_TIMEOUT_SEC",
    "MIN_MEMORY_MB",
    "SUPPORTED_LANGUAGES",
    "RunnerEvent",
    "RunnerEventError",
    "RunnerResponse",
    "validate_event_dict",
    "validate_response_dict",
]
