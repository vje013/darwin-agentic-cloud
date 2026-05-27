"""
darwin.agenticcloud.substrate.modal
====================================

COST MODEL NOTE: this adapter\'s `preflight()` returns the WHOLESALE cost
— what Darwin pays Modal for the sandbox execution. NOT the price
Darwin charges the end customer. Customer-facing markup (per Phase 6
hosted tier) is applied in the billing layer, never in the substrate.

Modal substrate adapter for Phase 2 v3.0.0 — ephemeral sandbox mode.

Each workload spawns a fresh `modal.Sandbox.create()` with the workload
code, runs to completion, captures stdout/stderr/exit_code, and tears
down the sandbox. No pre-deployed Modal app to manage.

Architecture:
    ModalSubstrate              (this file, client side)
        |
        |  modal.Sandbox.create(image, cpu, memory, code, ...)
        v
    fresh Modal sandbox container
        |
        |  exec() workload, capture stdout/stderr/exit_code
        v
    sandbox_result dict -> evidence dict -> RunResult

Substrate id: `modal-v0`
Evidence schema URI: `darwin.cloud/evidence/modal/v1`

Required evidence fields:
- sandbox_id          Modal sandbox object id
- task_id             Per-workload task id (uuid)
- image_tag           Modal image (e.g. python:3.12-slim)
- exit_code           subprocess exit code
- container_status    \'ok\' | \'error\' | \'timeout\' | \'killed\'
- wall_time_sec       wall-clock seconds
- stdout_hash         sha256 of stdout
- stderr_hash         sha256 of stderr

Identity signing: per spec, every substrate signs its own identity
declaration. `identity_signer()` resolves via `resolve_identity_signer()`
which routes to RemoteClassKeySigner (hosted) or OperatorFallbackSigner
(self-hosted).

Why ephemeral sandboxes (not pre-deployed runner):
    • Stronger isolation: fresh container per workload, no state bleed
    • No deploy ceremony: `pip install darwin-agentic-cloud` + Modal token = ready
    • Modal\'s explicit product fit for "run untrusted code"
    • Simpler operational story: one adapter, no infrastructure footprint
"""

from __future__ import annotations

import contextlib
import hashlib
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from darwin.agenticcloud.hashing import content_hash
from darwin.agenticcloud.substrate.base import (
    EVIDENCE_REGISTRY,
    CostEstimate,
    EvidenceSchema,
    EvidenceSchemaError,
    PreflightRejected,
    RunResult,
    Substrate,
    SubstrateError,
    SubstrateExecutionError,
    SubstrateIdentitySigner,
)
from darwin.agenticcloud.substrate.identity import resolve_identity_signer
from darwin.agenticcloud.types import WorkloadSpec

# ============================================================================
# Constants
# ============================================================================

#: Substrate id. Modal is region-agnostic in v3.0.0.
SUBSTRATE_ID: str = "modal-v0"

#: Adapter version.
SUBSTRATE_VERSION: str = "0.1.0"

#: Evidence schema URI.
EVIDENCE_SCHEMA_ID: str = "darwin.cloud/evidence/modal/v1"

#: Required evidence fields. EVIDENCE_REGISTRY.validate() enforces these.
EVIDENCE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "sandbox_id",
        "task_id",
        "image_tag",
        "exit_code",
        "container_status",
        "wall_time_sec",
        "stdout_hash",
        "stderr_hash",
    }
)

#: Wholesale per-second cost. Modal\'s CPU-only sandbox pricing
#: (0.125 cores ~= $0.000131/s, public pricing as of v3.0.0).
WHOLESALE_COST_PER_WALL_SECOND_USD: float = 0.000131

#: Modal cold-start overhead in seconds. Surfaced in CostEstimate for
#: honest cost comparison across substrates.
ESTIMATED_COLD_START_SEC: float = 5.0

#: Max wall time. Mirrors AWS Lambda ceiling.
MAX_WALL_TIME_SEC: int = 900

#: Supported workload languages.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"python", "node"})

#: Default Modal images.
DEFAULT_PYTHON_IMAGE: str = "python:3.12-slim"
DEFAULT_NODE_IMAGE: str = "node:20-slim"


# ============================================================================
# Sandbox-factory protocol (for test injection)
# ============================================================================


class SandboxFactory(Protocol):
    """Callable that spawns a sandbox, runs the workload, returns the result.

    Production uses `_real_modal_sandbox_factory` which talks to the real
    Modal SDK. Tests inject fakes via the `sandbox_factory` constructor
    arg of `ModalSubstrate` so they don\'t need network or Modal credentials.

    The factory must return a dict with these keys:
        sandbox_id        str  (Modal sandbox object id)
        stdout            str
        stderr            str
        exit_code         int  (subprocess exit code, -1 if killed)
        container_status  str  ("ok" | "error" | "timeout" | "killed")
    """

    def __call__(
        self,
        *,
        image_tag: str,
        cpu_cores: float,
        memory_mb: int,
        language: str,
        code: str,
        timeout_sec: int,
    ) -> dict[str, Any]: ...


# ============================================================================
# Evidence validator
# ============================================================================


def _validate_evidence(evidence: Mapping[str, Any]) -> None:
    """Validate Modal-specific evidence fields beyond required-keys check."""
    exit_code = evidence.get("exit_code")
    if not isinstance(exit_code, int):
        raise EvidenceSchemaError(f"exit_code must be int, got {type(exit_code).__name__}")
    wall_time = evidence.get("wall_time_sec")
    if not isinstance(wall_time, (int, float)):
        raise EvidenceSchemaError(f"wall_time_sec must be a number, got {type(wall_time).__name__}")
    stdout_hash = evidence.get("stdout_hash", "")
    if not isinstance(stdout_hash, str) or not stdout_hash.startswith("sha256:"):
        raise EvidenceSchemaError(
            f"stdout_hash must be a sha256-prefixed string, got {stdout_hash!r}"
        )
    stderr_hash = evidence.get("stderr_hash", "")
    if not isinstance(stderr_hash, str) or not stderr_hash.startswith("sha256:"):
        raise EvidenceSchemaError(
            f"stderr_hash must be a sha256-prefixed string, got {stderr_hash!r}"
        )
    container_status = evidence.get("container_status")
    if container_status not in {"ok", "error", "timeout", "killed"}:
        raise EvidenceSchemaError(
            f"container_status must be ok|error|timeout|killed, got {container_status!r}"
        )


# Register at import time.
EVIDENCE_REGISTRY.register(
    EvidenceSchema(
        schema_id=EVIDENCE_SCHEMA_ID,
        required_fields=EVIDENCE_REQUIRED_FIELDS,
        validator=_validate_evidence,
    )
)


# ============================================================================
# Configuration
# ============================================================================


@dataclass(frozen=True, slots=True)
class ModalConfig:
    """Configuration for the Modal substrate adapter.

    Default constructor produces a config that uses MODAL_TOKEN_ID /
    MODAL_TOKEN_SECRET from the environment (or ~/.modal.toml).
    """

    image_python: str = DEFAULT_PYTHON_IMAGE
    image_node: str = DEFAULT_NODE_IMAGE
    cpu_cores: float = 0.125
    memory_mb: int = 512


# ============================================================================
# Substrate
# ============================================================================


class ModalSubstrate(Substrate):
    """Substrate backed by ephemeral Modal sandboxes."""

    SUBSTRATE_ID = SUBSTRATE_ID

    def __init__(
        self,
        *,
        config: ModalConfig | None = None,
        identity_signer: SubstrateIdentitySigner | None = None,
        sandbox_factory: SandboxFactory | None = None,
    ) -> None:
        self._config = config or ModalConfig()
        self._explicit_signer = identity_signer
        self._sandbox_factory = sandbox_factory

    # ----- ABC: required metadata -----

    @property
    def substrate_id(self) -> str:
        return SUBSTRATE_ID

    @property
    def substrate_version(self) -> str:
        return SUBSTRATE_VERSION

    @property
    def evidence_schema_id(self) -> str:
        return EVIDENCE_SCHEMA_ID

    # ----- ABC: identity -----

    def identity_signer(self) -> SubstrateIdentitySigner:
        if self._explicit_signer is not None:
            return self._explicit_signer
        return resolve_identity_signer(SUBSTRATE_ID)

    # ----- ABC: preflight -----

    def preflight(self, workload: WorkloadSpec) -> CostEstimate:
        """Validate inputs and return an upper-bound cost estimate.

        Raises PreflightRejected for unsupported languages or out-of-range
        timeouts.
        """
        if workload.language not in SUPPORTED_LANGUAGES:
            raise PreflightRejected(
                f"Modal substrate supports {sorted(SUPPORTED_LANGUAGES)}, got {workload.language!r}"
            )
        if workload.timeout_sec < 1 or workload.timeout_sec > MAX_WALL_TIME_SEC:
            raise PreflightRejected(
                f"timeout_sec must be 1..{MAX_WALL_TIME_SEC}, got {workload.timeout_sec}"
            )

        estimated_seconds = ESTIMATED_COLD_START_SEC + min(workload.timeout_sec, MAX_WALL_TIME_SEC)
        cost_usd_max = estimated_seconds * WHOLESALE_COST_PER_WALL_SECOND_USD

        return CostEstimate(
            cost_usd_max=cost_usd_max,
            cost_breakdown={
                "modal_sandbox_seconds": cost_usd_max,
                "modal_cold_start_sec": ESTIMATED_COLD_START_SEC,
            },
            notes=(
                f"Modal ephemeral sandbox; "
                f"{ESTIMATED_COLD_START_SEC}s cold-start included in estimate"
            ),
        )

    # ----- ABC: run -----

    def run(self, workload: WorkloadSpec) -> RunResult:
        """Execute the workload in a fresh Modal sandbox."""
        # Defensive: re-validate inputs in case run() is called directly.
        if workload.language not in SUPPORTED_LANGUAGES:
            raise PreflightRejected(
                f"Modal substrate supports {sorted(SUPPORTED_LANGUAGES)}, got {workload.language!r}"
            )

        sandbox_factory = self._sandbox_factory or _real_modal_sandbox_factory
        task_id = f"modal-task-{uuid.uuid4().hex[:12]}"
        image_tag = (
            self._config.image_python if workload.language == "python" else self._config.image_node
        )

        started_at = time.time()
        try:
            sandbox_result = sandbox_factory(
                image_tag=image_tag,
                cpu_cores=self._config.cpu_cores,
                memory_mb=self._config.memory_mb,
                language=workload.language,
                code=workload.code,
                timeout_sec=min(workload.timeout_sec, MAX_WALL_TIME_SEC),
            )
        except SubstrateError:
            raise
        except Exception:
            # Sandbox crashed in a way it can\'t describe; let it propagate
            # so the runtime can record an unrecoverable failure.
            raise
        ended_at = time.time()

        wall_time_sec = ended_at - started_at
        cost_usd = wall_time_sec * WHOLESALE_COST_PER_WALL_SECOND_USD

        stdout = sandbox_result["stdout"]
        stderr = sandbox_result["stderr"]
        exit_code = sandbox_result["exit_code"]
        container_status = sandbox_result["container_status"]
        sandbox_id = sandbox_result["sandbox_id"]

        stdout_hash_hex = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        stderr_hash_hex = hashlib.sha256(stderr.encode("utf-8")).hexdigest()
        output_hash = stdout_hash_hex

        evidence: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "task_id": task_id,
            "image_tag": image_tag,
            "exit_code": exit_code,
            "container_status": container_status,
            "wall_time_sec": wall_time_sec,
            "stdout_hash": f"sha256:{stdout_hash_hex}",
            "stderr_hash": f"sha256:{stderr_hash_hex}",
        }

        # Substrate-described failure: emit partial evidence via
        # SubstrateExecutionError so the runtime can still build a
        # forensic attestation.
        if container_status != "ok":
            raise SubstrateExecutionError(
                f"Modal sandbox status={container_status!r} exit_code={exit_code}",
                partial_evidence=evidence,
            )

        return RunResult(
            substrate_id=SUBSTRATE_ID,
            substrate_version=SUBSTRATE_VERSION,
            workload_spec_hash=content_hash(asdict(workload)),
            stdout=stdout,
            stderr=stderr,
            output_hash=output_hash,
            cost_usd=cost_usd,
            evidence_schema_id=EVIDENCE_SCHEMA_ID,
            evidence=evidence,
            extensions={},
            tee_required=False,
            issued_at="",
        )


# ============================================================================
# Real Modal SDK path
# ============================================================================


def _real_modal_sandbox_factory(
    *,
    image_tag: str,
    cpu_cores: float,
    memory_mb: int,
    language: str,
    code: str,
    timeout_sec: int,
) -> dict[str, Any]:
    """Production sandbox factory backed by the Modal SDK.

    Imported lazily so unit tests don\'t need the modal package installed.
    """
    try:
        import modal
    except ImportError as e:
        raise PreflightRejected(
            "modal package not installed. Install with: pip install modal"
        ) from e

    try:
        app = modal.App.lookup(
            "darwin-agentic-cloud-ephemeral",
            create_if_missing=True,
        )
        image = modal.Image.from_registry(image_tag)
    except Exception as e:
        raise PreflightRejected(f"Modal initialization failed: {type(e).__name__}: {e}") from e

    if language == "python":
        cmd = ["python3", "-c", code]
    else:
        cmd = ["node", "-e", code]

    sb = None
    try:
        sb = modal.Sandbox.create(
            *cmd,
            image=image,
            app=app,
            cpu=cpu_cores,
            memory=memory_mb,
            timeout=timeout_sec,
        )
        sandbox_id = getattr(sb, "object_id", "unknown")
        sb.wait()
        stdout_raw = sb.stdout.read() if sb.stdout is not None else b""
        stderr_raw = sb.stderr.read() if sb.stderr is not None else b""
        exit_code = sb.returncode if sb.returncode is not None else -1

        stdout = (
            stdout_raw
            if isinstance(stdout_raw, str)
            else stdout_raw.decode("utf-8", errors="replace")
        )
        stderr = (
            stderr_raw
            if isinstance(stderr_raw, str)
            else stderr_raw.decode("utf-8", errors="replace")
        )

        if exit_code == 0:
            container_status = "ok"
        elif exit_code < 0:
            container_status = "killed"
        else:
            container_status = "error"

        result = {
            "sandbox_id": sandbox_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "container_status": container_status,
        }
        with contextlib.suppress(Exception):
            sb.terminate()
        return result
    except Exception as e:
        if sb is not None:
            with contextlib.suppress(Exception):
                sb.terminate()
        # Wrap as SubstrateExecutionError with minimal partial evidence
        # so the runtime can attest the failure.
        raise SubstrateExecutionError(
            f"Modal sandbox failure: {type(e).__name__}: {e}",
            partial_evidence={
                "sandbox_id": "unknown",
                "task_id": "unknown",
                "image_tag": image_tag,
                "exit_code": -1,
                "container_status": "error",
                "wall_time_sec": 0.0,
                "stdout_hash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "stderr_hash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        ) from e
