"""
tests.substrate.test_local_docker
=================================

Tests for `darwin.agenticcloud.substrate.local_docker.LocalDockerSubstrate`.

Two layers:

- Unit tests (no Docker): inject a fake DockerSandbox into the substrate
  and exercise the adapter's translation logic — evidence shape,
  preflight, cost math, error surfacing, identity signer resolution.
- Integration test (marked `integration`): runs a real container via
  the actual DockerSandbox to confirm the full path works against the
  real daemon. Skipped in default CI lane.

Conformance assertions reuse `assert_substrate_conforms_to_v02()` from
tests.substrate.test_base so future adapters can copy the same shape.
"""

from __future__ import annotations

import base64
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from darwin.agenticcloud.hashing import content_hash, sha256_hex
from darwin.agenticcloud.sandbox import SandboxResult
from darwin.agenticcloud.substrate.base import (
    EVIDENCE_REGISTRY,
    EvidenceSchemaError,
    PreflightRejected,
    RunResult,
    Substrate,
    SubstrateExecutionError,
    build_attestation_dict,
    iso8601_now,
    sign_identity,
)
from darwin.agenticcloud.substrate.identity import (
    ED25519_SIGNATURE_BYTES,
    OperatorFallbackSigner,
)
from darwin.agenticcloud.substrate.local_docker import (
    EVIDENCE_SCHEMA_ID,
    SUBSTRATE_ID,
    SUBSTRATE_VERSION,
    LocalDockerSubstrate,
)
from darwin.agenticcloud.types import WorkloadSpec
from tests.substrate.test_base import assert_substrate_conforms_to_v02

# ============================================================================
# Helpers
# ============================================================================


def _make_sandbox_result(
    *,
    status: str = "ok",
    stdout: str = "Hello, agent.\n",
    stderr: str = "",
    exit_code: int | None = 0,
    wall_time_sec: float = 0.5,
    error: str | None = None,
) -> SandboxResult:
    """Build a SandboxResult mimicking what the real DockerSandbox returns."""
    return SandboxResult(
        status=status,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        started_at=1716_000_000.0,
        ended_at=1716_000_000.0 + wall_time_sec,
        wall_time_sec=wall_time_sec,
        substrate_id=SUBSTRATE_ID,
        output_hash=sha256_hex(stdout.encode("utf-8")),
        error=error,
    )


def _make_workload(**overrides) -> WorkloadSpec:
    defaults = {
        "code": "print('hi')",
        "language": "python",
        "inputs": {},
        "cost_cap_usd": 0.01,
        "timeout_sec": 30,
        "memory_mb": 512,
    }
    defaults.update(overrides)
    return WorkloadSpec(**defaults)


def _make_substrate_with_fake_sandbox(
    sandbox_result: SandboxResult,
    *,
    identity_signer=None,
) -> LocalDockerSubstrate:
    """Build a substrate with a stubbed sandbox that returns `sandbox_result`."""
    fake_sandbox = MagicMock()
    fake_sandbox.execute.return_value = sandbox_result
    return LocalDockerSubstrate(
        sandbox=fake_sandbox,
        identity_signer=identity_signer,
    )


# ============================================================================
# ABC conformance
# ============================================================================


class TestABCConformance:
    def test_substrate_id(self):
        sub = LocalDockerSubstrate(sandbox=MagicMock())
        assert sub.substrate_id == "local-docker-v0"

    def test_substrate_version_is_semver(self):
        sub = LocalDockerSubstrate(sandbox=MagicMock())
        parts = sub.substrate_version.split(".")
        assert len(parts) == 3
        for p in parts:
            assert p.isdigit(), f"version part {p!r} is not a digit"

    def test_evidence_schema_id(self):
        sub = LocalDockerSubstrate(sandbox=MagicMock())
        assert sub.evidence_schema_id == "darwin.cloud/evidence/local-docker/v1"

    def test_evidence_schema_registered_at_import(self):
        assert EVIDENCE_SCHEMA_ID in EVIDENCE_REGISTRY.known_ids()

    def test_conformance_helper(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        sub = LocalDockerSubstrate(
            sandbox=MagicMock(),
            identity_signer=OperatorFallbackSigner(),
        )
        assert_substrate_conforms_to_v02(
            sub,
            expected_substrate_id="local-docker-v0",
            expected_evidence_schema_id="darwin.cloud/evidence/local-docker/v1",
        )

    def test_is_substrate_subclass(self):
        assert issubclass(LocalDockerSubstrate, Substrate)


# ============================================================================
# Preflight
# ============================================================================


class TestPreflight:
    def test_accepts_workload_within_budget(self):
        sub = LocalDockerSubstrate(sandbox=MagicMock())
        # 30s * $0.0001/s = $0.003 << $0.01 cap
        wl = _make_workload(timeout_sec=30, cost_cap_usd=0.01)
        est = sub.preflight(wl)
        assert est.cost_usd_max == pytest.approx(0.003, abs=1e-6)
        assert "wall_time_sec_max" in est.cost_breakdown
        assert est.cost_breakdown["wall_time_sec_max"] == 30.0

    def test_rejects_workload_over_budget(self):
        sub = LocalDockerSubstrate(sandbox=MagicMock())
        # 600s * $0.0001/s = $0.06 > $0.01 cap
        wl = _make_workload(timeout_sec=600, cost_cap_usd=0.01)
        with pytest.raises(PreflightRejected):
            sub.preflight(wl)

    def test_rejects_zero_cost_cap(self):
        sub = LocalDockerSubstrate(sandbox=MagicMock())
        wl = _make_workload(cost_cap_usd=0.0)
        with pytest.raises(PreflightRejected):
            sub.preflight(wl)

    def test_preflight_does_not_call_sandbox(self):
        """Preflight is cheap — must NEVER touch the Docker daemon."""
        fake_sandbox = MagicMock()
        sub = LocalDockerSubstrate(sandbox=fake_sandbox)
        wl = _make_workload()
        sub.preflight(wl)
        fake_sandbox.execute.assert_not_called()


# ============================================================================
# Run — happy path
# ============================================================================


class TestRunHappyPath:
    def test_returns_runresult(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        wl = _make_workload()
        result = sub.run(wl)
        assert isinstance(result, RunResult)

    def test_substrate_metadata_in_result(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        wl = _make_workload()
        result = sub.run(wl)
        assert result.substrate_id == "local-docker-v0"
        assert result.substrate_version == SUBSTRATE_VERSION
        assert result.evidence_schema_id == EVIDENCE_SCHEMA_ID

    def test_workload_spec_hash_matches_phase1_helper(self):
        """Adapter must use the same content_hash() Phase 1 uses, so the
        workload_spec_hash field stays stable across the v0.1 -> v0.2
        transition."""
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        wl = _make_workload()
        result = sub.run(wl)
        assert result.workload_spec_hash == content_hash(asdict(wl))

    def test_stdout_and_output_hash_propagated(self):
        sb_result = _make_sandbox_result(stdout="custom output\n")
        sub = _make_substrate_with_fake_sandbox(sb_result)
        result = sub.run(_make_workload())
        assert result.stdout == "custom output\n"
        assert result.output_hash == sb_result.output_hash

    def test_cost_uses_phase1_rate_card(self):
        # 0.5s * $0.0001/s = $0.00005
        sb_result = _make_sandbox_result(wall_time_sec=0.5)
        sub = _make_substrate_with_fake_sandbox(sb_result)
        result = sub.run(_make_workload())
        assert result.cost_usd == pytest.approx(0.00005, abs=1e-8)

    def test_extensions_empty(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        result = sub.run(_make_workload())
        assert result.extensions == {}

    def test_tee_required_false(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        result = sub.run(_make_workload())
        assert result.tee_required is False

    def test_issued_at_blank_at_run_time(self):
        """`issued_at` is set by the runtime just before signing, not
        by the substrate. Substrate returns blank so the runtime owns
        the timestamp shared between outer and substrate-identity sigs."""
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        result = sub.run(_make_workload())
        assert result.issued_at == ""


# ============================================================================
# Evidence shape
# ============================================================================


class TestEvidenceShape:
    def test_required_fields_present(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        result = sub.run(_make_workload())
        assert set(result.evidence.keys()) >= {
            "container_status",
            "exit_code",
            "stdout_hash",
            "stderr_hash",
            "wall_time_sec",
        }

    def test_container_status_ok(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result(status="ok"))
        result = sub.run(_make_workload())
        assert result.evidence["container_status"] == "ok"

    def test_exit_code_propagated(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result(exit_code=0))
        result = sub.run(_make_workload())
        assert result.evidence["exit_code"] == 0

    def test_stderr_hash_computed_correctly(self):
        sb_result = _make_sandbox_result(stderr="warning: foo\n")
        sub = _make_substrate_with_fake_sandbox(sb_result)
        result = sub.run(_make_workload())
        expected = sha256_hex(b"warning: foo\n")
        assert result.evidence["stderr_hash"] == expected

    def test_stderr_hash_for_empty_stderr(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result(stderr=""))
        result = sub.run(_make_workload())
        # Hash of empty bytes is well-known.
        assert result.evidence["stderr_hash"] == sha256_hex(b"")

    def test_wall_time_propagated(self):
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result(wall_time_sec=2.71828))
        result = sub.run(_make_workload())
        assert result.evidence["wall_time_sec"] == pytest.approx(2.71828)

    def test_evidence_passes_registered_validator(self):
        """Round trip through build_attestation_dict to confirm the
        evidence registry validator accepts what this substrate produces."""
        sub = _make_substrate_with_fake_sandbox(_make_sandbox_result())
        result = sub.run(_make_workload())
        # Need an issued_at and an identity sig to build the attestation.
        result_for_attest = RunResult(
            substrate_id=result.substrate_id,
            substrate_version=result.substrate_version,
            workload_spec_hash=result.workload_spec_hash,
            stdout=result.stdout,
            stderr=result.stderr,
            output_hash=result.output_hash,
            cost_usd=result.cost_usd,
            evidence_schema_id=result.evidence_schema_id,
            evidence=result.evidence,
            extensions=result.extensions,
            tee_required=result.tee_required,
            issued_at=iso8601_now(),
        )
        # Use a fake signer for identity.
        from tests.substrate.test_base import _FakeSigner  # type: ignore

        identity = sign_identity(
            result=result_for_attest,
            signer=_FakeSigner(),
        )
        att = build_attestation_dict(
            attestation_id="att_test_local_docker",
            result=result_for_attest,
            identity=identity,
        )
        sub_block = att["execution_result"]["substrate"]
        assert sub_block["evidence_schema_id"] == EVIDENCE_SCHEMA_ID
        assert sub_block["evidence"]["container_status"] == "ok"


# ============================================================================
# Run — failure modes
# ============================================================================


class TestRunFailureModes:
    @pytest.mark.parametrize("status", ["error", "timeout", "oom"])
    def test_non_ok_status_raises_with_partial_evidence(self, status):
        sb_result = _make_sandbox_result(
            status=status,
            stdout="" if status == "timeout" else "partial output",
            stderr="some error" if status == "error" else "",
            exit_code=None if status == "timeout" else 1,
            error="container failed" if status == "error" else None,
        )
        sub = _make_substrate_with_fake_sandbox(sb_result)
        with pytest.raises(SubstrateExecutionError) as exc_info:
            sub.run(_make_workload())
        # Partial evidence carries the same five fields.
        pe = exc_info.value.partial_evidence
        assert pe["container_status"] == status
        assert "stdout_hash" in pe
        assert "stderr_hash" in pe
        assert "wall_time_sec" in pe
        # Plus a structured _partial_run_result for the runtime.
        assert "_partial_run_result" in pe
        prr = pe["_partial_run_result"]
        assert prr["substrate_id"] == "local-docker-v0"
        assert prr["evidence_schema_id"] == EVIDENCE_SCHEMA_ID

    def test_timeout_partial_evidence_has_none_exit_code(self):
        sb_result = _make_sandbox_result(
            status="timeout",
            exit_code=None,
            wall_time_sec=30.0,
        )
        sub = _make_substrate_with_fake_sandbox(sb_result)
        with pytest.raises(SubstrateExecutionError) as exc_info:
            sub.run(_make_workload())
        assert exc_info.value.partial_evidence["exit_code"] is None


# ============================================================================
# Evidence validator catches malformed evidence
# ============================================================================


class TestEvidenceValidator:
    """Sanity checks that the substrate-specific validator we registered
    catches bad evidence shapes. Defends against future changes silently
    breaking the schema."""

    def test_rejects_unknown_container_status(self):
        bad_evidence = {
            "container_status": "exploded",  # invalid
            "exit_code": 0,
            "stdout_hash": "x",
            "stderr_hash": "x",
            "wall_time_sec": 0.1,
        }
        with pytest.raises(EvidenceSchemaError, match="container_status"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, bad_evidence)

    def test_rejects_negative_wall_time(self):
        bad_evidence = {
            "container_status": "ok",
            "exit_code": 0,
            "stdout_hash": "x",
            "stderr_hash": "x",
            "wall_time_sec": -1.0,
        }
        with pytest.raises(EvidenceSchemaError, match="wall_time_sec"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, bad_evidence)

    def test_rejects_non_numeric_wall_time(self):
        bad_evidence = {
            "container_status": "ok",
            "exit_code": 0,
            "stdout_hash": "x",
            "stderr_hash": "x",
            "wall_time_sec": "fast",
        }
        with pytest.raises(EvidenceSchemaError, match="wall_time_sec"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, bad_evidence)


# ============================================================================
# Identity signer wiring
# ============================================================================


class TestIdentitySignerWiring:
    def test_explicit_signer_used_when_provided(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        explicit = OperatorFallbackSigner()
        sub = LocalDockerSubstrate(
            sandbox=MagicMock(),
            identity_signer=explicit,
        )
        assert sub.identity_signer() is explicit

    def test_resolve_called_when_no_signer_provided(self, tmp_path, monkeypatch):
        """No explicit signer + DARWIN_SIGNER_URL='' => OperatorFallback."""
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("DARWIN_SIGNER_URL", "")
        sub = LocalDockerSubstrate(sandbox=MagicMock())
        signer = sub.identity_signer()
        assert signer.signer_type == "operator-fallback"


# ============================================================================
# End-to-end: substrate -> attestation -> verify
# ============================================================================


class TestEndToEnd:
    def test_full_attestation_flow_with_operator_fallback(self, tmp_path, monkeypatch):
        """Run through a fake sandbox, build a v0.2 attestation, verify
        the substrate-identity signature with the operator's public key.
        Mirrors what the real runtime will do in step 7."""
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        signer = OperatorFallbackSigner()

        sub = LocalDockerSubstrate(
            sandbox=MagicMock(),
            identity_signer=signer,
        )
        sub._sandbox.execute.return_value = _make_sandbox_result()

        # Preflight + run, mimicking what a Phase 2 runtime would do.
        wl = _make_workload()
        sub.preflight(wl)
        result = sub.run(wl)
        # Runtime stamps issued_at just before signing.
        result_final = RunResult(
            substrate_id=result.substrate_id,
            substrate_version=result.substrate_version,
            workload_spec_hash=result.workload_spec_hash,
            stdout=result.stdout,
            stderr=result.stderr,
            output_hash=result.output_hash,
            cost_usd=result.cost_usd,
            evidence_schema_id=result.evidence_schema_id,
            evidence=result.evidence,
            extensions=result.extensions,
            tee_required=result.tee_required,
            issued_at=iso8601_now(),
        )
        identity = sign_identity(result=result_final, signer=signer)

        att = build_attestation_dict(
            attestation_id="att_e2e",
            result=result_final,
            identity=identity,
        )

        # Verify the substrate-identity signature against the operator's
        # public key (what the verifier will do for operator-fallback
        # attestations).
        from darwin.agenticcloud.hashing import canonical_json
        from darwin.agenticcloud.signing import verify_signature
        from darwin.agenticcloud.substrate.base import build_identity_payload

        payload = build_identity_payload(
            substrate_id=result_final.substrate_id,
            substrate_version=result_final.substrate_version,
            workload_spec_hash=result_final.workload_spec_hash,
            output_hash=result_final.output_hash,
            evidence_schema_id=result_final.evidence_schema_id,
            issued_at=result_final.issued_at,
        )
        canonical = canonical_json(payload)
        assert (
            verify_signature(
                canonical,
                att["execution_result"]["substrate"]["identity_signature"],
                signer.public_key_b64,
            )
            is True
        )


# ============================================================================
# Integration: real Docker
# ============================================================================


@pytest.mark.integration
class TestIntegrationRealDocker:
    """Smoke test against the real Docker daemon. Skipped in default CI.
    Runs in CI integration lane and locally with `-m integration`."""

    def test_real_hello_workload_produces_valid_attestation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        signer = OperatorFallbackSigner()
        sub = LocalDockerSubstrate(identity_signer=signer)

        wl = WorkloadSpec(
            code="print('Hello, agent.')",
            language="python",
            cost_cap_usd=0.01,
            timeout_sec=30,
            memory_mb=128,
        )
        sub.preflight(wl)
        result = sub.run(wl)
        assert "Hello, agent." in result.stdout
        assert result.evidence["container_status"] == "ok"
        assert result.evidence["exit_code"] == 0
        assert result.evidence["wall_time_sec"] > 0

        # Stamp issued_at and sign.
        result_final = RunResult(
            substrate_id=result.substrate_id,
            substrate_version=result.substrate_version,
            workload_spec_hash=result.workload_spec_hash,
            stdout=result.stdout,
            stderr=result.stderr,
            output_hash=result.output_hash,
            cost_usd=result.cost_usd,
            evidence_schema_id=result.evidence_schema_id,
            evidence=result.evidence,
            extensions=result.extensions,
            tee_required=result.tee_required,
            issued_at=iso8601_now(),
        )
        identity = sign_identity(result=result_final, signer=signer)
        att = build_attestation_dict(
            attestation_id="att_integration",
            result=result_final,
            identity=identity,
        )
        assert att["schema"] == "darwin.cloud/agenticcloud/attestation/v0.2"
        sig_bytes = base64.b64decode(identity.identity_signature)
        assert len(sig_bytes) == ED25519_SIGNATURE_BYTES
