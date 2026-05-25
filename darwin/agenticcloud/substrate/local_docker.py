"""
darwin.agenticcloud.substrate.local_docker
==========================================

Local Docker substrate adapter for Phase 2 v3.0.0.

Wraps the existing Phase 1 `DockerSandbox` (which is battle-tested across
55 tests) and exposes it through the new `Substrate` ABC. Behavior is
preserved exactly:

- Same container isolation: memory + CPU limits, network disabled,
  read-only filesystem with tmpfs /tmp, dropped capabilities, no-root.
- Same cost model: `cost.cost_for_seconds(wall_time, 'local-docker-v0')`.
- Same pre-flight check: `cost.max_possible_cost()` vs `spec.cost_cap_usd`.
- Same status codes: 'ok', 'error', 'timeout', 'oom'.

What's new for Phase 2:

- Registers its evidence schema (`darwin.cloud/evidence/local-docker/v1`)
  with the global `EVIDENCE_REGISTRY` at module import.
- Implements the v0.2 attestation evidence shape with five fields:
  `container_status`, `exit_code`, `stdout_hash`, `stderr_hash`,
  `wall_time_sec`.
- Identity signer is resolved per-run via `resolve_identity_signer()`
  so hosted Darwin produces class-signed identities and self-hosted
  Darwin produces operator-fallback signed identities — both verifiable.

Phase 1's `sandbox.py` is NOT modified or removed. It continues to be
the implementation under the hood. The Phase 1 runtime keeps using it
directly until step 7 (schema v0.2 wired through runtime) flips the
runtime to call the substrate adapter instead.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from darwin.agenticcloud.cost import (
    BudgetExceeded,
    check_budget,
    cost_for_seconds,
    max_possible_cost,
)
from darwin.agenticcloud.hashing import content_hash, sha256_hex
from darwin.agenticcloud.sandbox import SUBSTRATE_ID as PHASE1_SUBSTRATE_ID
from darwin.agenticcloud.sandbox import DockerSandbox, SandboxResult
from darwin.agenticcloud.substrate.base import (
    EVIDENCE_REGISTRY,
    CostEstimate,
    EvidenceSchema,
    PreflightRejected,
    RunResult,
    Substrate,
    SubstrateExecutionError,
    SubstrateIdentitySigner,
)
from darwin.agenticcloud.substrate.identity import resolve_identity_signer
from darwin.agenticcloud.types import WorkloadSpec

# ============================================================================
# Substrate identity
# ============================================================================

#: Canonical substrate id. Matches Phase 1's SUBSTRATE_ID exactly so the
#: cost rate card and any audit logs that reference 'local-docker-v0'
#: continue to resolve correctly.
SUBSTRATE_ID: str = PHASE1_SUBSTRATE_ID

#: Adapter version (semver). Bumped when behavior changes in a way that
#: would change produced evidence shape or content.
SUBSTRATE_VERSION: str = "0.1.0"

#: Evidence schema URI for local-docker. Spec section 5.1 names exactly
#: these fields: container_id, exit_code, stdout_hash, stderr_hash.
#: We add wall_time_sec because verifiers want to recompute cost.
EVIDENCE_SCHEMA_ID: str = "darwin.cloud/evidence/local-docker/v1"

#: Required evidence fields. EVIDENCE_REGISTRY.validate() enforces these
#: before any attestation is built.
EVIDENCE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "container_status",
        "exit_code",
        "stdout_hash",
        "stderr_hash",
        "wall_time_sec",
    }
)


def _validate_evidence(evidence: Any) -> None:
    """Substrate-specific evidence validator.

    Required-fields presence is enforced by the registry. This function
    only checks types and value constraints the registry can't express.
    """
    cs = evidence.get("container_status")
    if cs not in {"ok", "error", "timeout", "oom"}:
        from darwin.agenticcloud.substrate.base import EvidenceSchemaError

        raise EvidenceSchemaError(
            f"local-docker evidence.container_status must be one of "
            f"'ok'/'error'/'timeout'/'oom', got {cs!r}"
        )
    wt = evidence.get("wall_time_sec")
    if not isinstance(wt, int | float) or wt < 0:
        from darwin.agenticcloud.substrate.base import EvidenceSchemaError

        raise EvidenceSchemaError(
            f"local-docker evidence.wall_time_sec must be a non-negative number, got {wt!r}"
        )


# Register at import. Idempotent — re-imports during tests are safe.
EVIDENCE_REGISTRY.register(
    EvidenceSchema(
        schema_id=EVIDENCE_SCHEMA_ID,
        required_fields=EVIDENCE_REQUIRED_FIELDS,
        validator=_validate_evidence,
    )
)


# ============================================================================
# LocalDockerSubstrate
# ============================================================================


class LocalDockerSubstrate(Substrate):
    """The Phase 1 DockerSandbox, adapted to the Phase 2 Substrate ABC.

    Holds a `DockerSandbox` instance internally. `preflight()` returns a
    cost estimate using the Phase 1 cost model. `run()` executes through
    the sandbox and translates the result into a `RunResult` with the
    v0.2 evidence shape.

    The identity signer is resolved per-instance at construction time
    (default) but can be overridden — tests pass an explicit signer.
    """

    def __init__(
        self,
        sandbox: DockerSandbox | None = None,
        identity_signer: SubstrateIdentitySigner | None = None,
    ) -> None:
        self._sandbox = sandbox or DockerSandbox()
        # Resolve the identity signer lazily so we can override in tests.
        # `_identity_signer` is None until first call to `identity_signer()`.
        self._explicit_signer = identity_signer

    # --- Substrate ABC: metadata --------------------------------------------

    @property
    def substrate_id(self) -> str:
        return SUBSTRATE_ID

    @property
    def substrate_version(self) -> str:
        return SUBSTRATE_VERSION

    @property
    def evidence_schema_id(self) -> str:
        return EVIDENCE_SCHEMA_ID

    # --- Substrate ABC: behavior --------------------------------------------

    def preflight(self, workload: WorkloadSpec) -> CostEstimate:
        """Pre-flight cost check using the Phase 1 cost model.

        Computes the maximum cost (full-timeout run) and confirms it
        does not exceed the workload's cost cap. Mirrors `cost.check_budget()`
        so a substrate-level preflight matches the runtime's existing
        budget enforcement exactly.
        """
        # Reuse the Phase 1 budget check so behavior is identical to what
        # `runtime.Runtime.run()` already does today.
        try:
            check_budget(workload, SUBSTRATE_ID)
        except BudgetExceeded as e:
            raise PreflightRejected(str(e)) from e

        projected = max_possible_cost(workload, SUBSTRATE_ID)
        return CostEstimate(
            cost_usd_max=projected,
            cost_breakdown={
                "wall_time_sec_max": float(workload.timeout_sec),
                "rate_usd_per_sec": projected / max(workload.timeout_sec, 1),
            },
            notes=f"Pre-flight estimate for {SUBSTRATE_ID}",
        )

    def run(self, workload: WorkloadSpec) -> RunResult:
        """Execute through the Phase 1 DockerSandbox and adapt the result.

        On non-`ok` sandbox status, raises `SubstrateExecutionError` with
        a partial evidence dict so the runtime can still attest to the
        failure.
        """
        sb_result: SandboxResult = self._sandbox.execute(
            code=workload.code,
            language=workload.language,
            timeout_sec=workload.timeout_sec,
            memory_mb=workload.memory_mb,
            # Phase 1 sandbox.execute takes cpu_quota as float; the
            # workload doesn't carry that yet so we accept the sandbox
            # default (1.0 nano-CPU).
        )

        actual_cost = cost_for_seconds(sb_result.wall_time_sec, SUBSTRATE_ID)
        evidence = self._build_evidence(sb_result)

        # On non-`ok` sandbox status we surface a SubstrateExecutionError
        # with the partial evidence. The caller (Phase 2 runtime) decides
        # whether to still produce an attestation. This matches the
        # Phase 1 behavior where every status — including 'error',
        # 'timeout', 'oom' — gets an attestation.
        if sb_result.status != "ok":
            raise SubstrateExecutionError(
                f"local-docker workload status={sb_result.status}: "
                f"{sb_result.error or 'see stderr'}",
                partial_evidence={
                    **evidence,
                    "_partial_run_result": self._build_partial_run_result_dict(
                        workload=workload,
                        sb_result=sb_result,
                        actual_cost=actual_cost,
                        evidence=evidence,
                    ),
                },
            )

        return RunResult(
            substrate_id=SUBSTRATE_ID,
            substrate_version=SUBSTRATE_VERSION,
            workload_spec_hash=content_hash(asdict(workload)),
            stdout=sb_result.stdout,
            stderr=sb_result.stderr,
            output_hash=sb_result.output_hash,
            cost_usd=actual_cost,
            evidence_schema_id=EVIDENCE_SCHEMA_ID,
            evidence=evidence,
            extensions={},
            tee_required=False,
            # issued_at filled by the runtime just before signing, so the
            # outer and substrate-identity signatures share one timestamp.
            issued_at="",
        )

    def identity_signer(self) -> SubstrateIdentitySigner:
        """Return the substrate-identity signer for this substrate.

        If a signer was passed at construction (test override), use it.
        Otherwise consult `resolve_identity_signer()` which reads
        `DARWIN_SIGNER_URL` to pick between RemoteClassKeySigner and
        OperatorFallbackSigner.
        """
        if self._explicit_signer is not None:
            return self._explicit_signer
        return resolve_identity_signer(SUBSTRATE_ID)

    # --- Helpers ------------------------------------------------------------

    @staticmethod
    def _build_evidence(sb_result: SandboxResult) -> dict[str, Any]:
        return {
            "container_status": sb_result.status,
            "exit_code": sb_result.exit_code,
            "stdout_hash": sb_result.output_hash,
            "stderr_hash": sha256_hex(sb_result.stderr.encode("utf-8")),
            "wall_time_sec": sb_result.wall_time_sec,
        }

    @staticmethod
    def _build_partial_run_result_dict(
        *,
        workload: WorkloadSpec,
        sb_result: SandboxResult,
        actual_cost: float,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Shape that a future runtime can use to build a 'failure'
        attestation. Kept here (in partial_evidence) so the runtime
        doesn't have to reconstruct it."""
        return {
            "substrate_id": SUBSTRATE_ID,
            "substrate_version": SUBSTRATE_VERSION,
            "workload_spec_hash": content_hash(asdict(workload)),
            "stdout": sb_result.stdout,
            "stderr": sb_result.stderr,
            "output_hash": sb_result.output_hash,
            "cost_usd": actual_cost,
            "evidence_schema_id": EVIDENCE_SCHEMA_ID,
            "evidence": evidence,
            "extensions": {},
            "tee_required": False,
        }


__all__ = [
    "EVIDENCE_REQUIRED_FIELDS",
    "EVIDENCE_SCHEMA_ID",
    "SUBSTRATE_ID",
    "SUBSTRATE_VERSION",
    "LocalDockerSubstrate",
]
