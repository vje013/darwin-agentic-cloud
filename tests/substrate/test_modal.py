"""
Tests for darwin.agenticcloud.substrate.modal.

Coverage:
- ABC conformance
- Pricing model (preflight, wholesale cost)
- Run happy path with injected fake sandbox factory
- Evidence shape + validator
- Failure modes (preflight rejection, substrate execution error,
  bare crash propagation)
- Identity signer wiring
- Evidence registry round-trip
"""

from __future__ import annotations

import hashlib

import pytest

from darwin.agenticcloud.substrate.base import (
    EVIDENCE_REGISTRY,
    CostEstimate,
    EvidenceSchemaError,
    PreflightRejected,
    RunResult,
    Substrate,
    SubstrateExecutionError,
    SubstrateIdentitySigner,
)
from darwin.agenticcloud.substrate.modal import (
    DEFAULT_NODE_IMAGE,
    DEFAULT_PYTHON_IMAGE,
    ESTIMATED_COLD_START_SEC,
    EVIDENCE_REQUIRED_FIELDS,
    EVIDENCE_SCHEMA_ID,
    MAX_WALL_TIME_SEC,
    SUBSTRATE_ID,
    SUBSTRATE_VERSION,
    SUPPORTED_LANGUAGES,
    WHOLESALE_COST_PER_WALL_SECOND_USD,
    ModalConfig,
    ModalSubstrate,
)
from darwin.agenticcloud.types import WorkloadSpec

# ============================================================================
# Test fixtures
# ============================================================================


def _ok_factory(stdout: str = "ok\n", exit_code: int = 0):
    """Return a sandbox factory that always succeeds."""

    def factory(**kwargs):
        return {
            "sandbox_id": "sb-test-1234",
            "stdout": stdout,
            "stderr": "",
            "exit_code": exit_code,
            "container_status": "ok" if exit_code == 0 else "error",
        }

    return factory


def _failing_factory(container_status: str = "error", exit_code: int = 1):
    def factory(**kwargs):
        return {
            "sandbox_id": "sb-test-fail",
            "stdout": "",
            "stderr": "boom",
            "exit_code": exit_code,
            "container_status": container_status,
        }

    return factory


def _make_workload(
    *,
    code: str = "print('hello')",
    language: str = "python",
    timeout_sec: int = 30,
    memory_mb: int = 512,
    cost_cap_usd: float = 1.0,
) -> WorkloadSpec:
    return WorkloadSpec(
        code=code,
        language=language,
        timeout_sec=timeout_sec,
        memory_mb=memory_mb,
        cost_cap_usd=cost_cap_usd,
    )


@pytest.fixture
def stub_signer() -> SubstrateIdentitySigner:
    """Minimal stub identity signer."""

    class _StubSigner:
        @property
        def signer_type(self) -> str:
            return "operator-fallback"

        @property
        def signer_key_id(self) -> str:
            return "stub-key-id"

        def sign(self, payload: bytes) -> bytes:
            return b"\x00" * 64

    return _StubSigner()


# ============================================================================
# ABC conformance
# ============================================================================


class TestABCConformance:
    def test_is_substrate_subclass(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer)
        assert isinstance(s, Substrate)

    def test_required_metadata(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer)
        assert s.substrate_id == "modal-v0"
        assert s.substrate_version == SUBSTRATE_VERSION
        assert s.evidence_schema_id == EVIDENCE_SCHEMA_ID

    def test_identity_signer_returns_explicit(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer)
        assert s.identity_signer() is stub_signer

    def test_constants_are_sane(self):
        assert SUBSTRATE_ID == "modal-v0"
        assert EVIDENCE_SCHEMA_ID == "darwin.cloud/evidence/modal/v1"
        assert frozenset({"python", "node"}) == SUPPORTED_LANGUAGES
        assert MAX_WALL_TIME_SEC == 900
        assert DEFAULT_PYTHON_IMAGE.startswith("python:")
        assert DEFAULT_NODE_IMAGE.startswith("node:")


# ============================================================================
# Pricing
# ============================================================================


class TestPricing:
    def test_wholesale_per_second_is_positive(self):
        assert WHOLESALE_COST_PER_WALL_SECOND_USD > 0
        assert WHOLESALE_COST_PER_WALL_SECOND_USD < 0.01

    def test_cold_start_in_estimate(self):
        assert ESTIMATED_COLD_START_SEC > 0
        assert ESTIMATED_COLD_START_SEC < 60


# ============================================================================
# Preflight
# ============================================================================


class TestPreflight:
    def test_python_workload_returns_cost_estimate(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        est = s.preflight(_make_workload(timeout_sec=10))
        assert isinstance(est, CostEstimate)
        expected = (ESTIMATED_COLD_START_SEC + 10) * WHOLESALE_COST_PER_WALL_SECOND_USD
        assert est.cost_usd_max == pytest.approx(expected)

    def test_node_workload_returns_cost_estimate(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        est = s.preflight(_make_workload(language="node", timeout_sec=5))
        expected = (ESTIMATED_COLD_START_SEC + 5) * WHOLESALE_COST_PER_WALL_SECOND_USD
        assert est.cost_usd_max == pytest.approx(expected)

    def test_unsupported_language_rejected(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        with pytest.raises(PreflightRejected, match="supports"):
            s.preflight(_make_workload(language="ruby"))

    def test_timeout_too_low_rejected(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        with pytest.raises(PreflightRejected, match="timeout_sec"):
            s.preflight(_make_workload(timeout_sec=0))

    def test_timeout_too_high_rejected(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        with pytest.raises(PreflightRejected, match="timeout_sec"):
            s.preflight(_make_workload(timeout_sec=MAX_WALL_TIME_SEC + 1))

    def test_cost_breakdown_includes_cold_start(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        est = s.preflight(_make_workload(timeout_sec=30))
        assert "modal_sandbox_seconds" in est.cost_breakdown
        assert "modal_cold_start_sec" in est.cost_breakdown


# ============================================================================
# Run happy path
# ============================================================================


class TestRunHappyPath:
    def test_simple_python_workload(self, stub_signer):
        s = ModalSubstrate(
            identity_signer=stub_signer,
            sandbox_factory=_ok_factory(stdout="hi\n"),
        )
        r = s.run(_make_workload(code="print('hi')"))
        assert isinstance(r, RunResult)
        assert r.substrate_id == "modal-v0"
        assert r.substrate_version == SUBSTRATE_VERSION
        assert r.stdout == "hi\n"
        assert r.evidence_schema_id == EVIDENCE_SCHEMA_ID
        expected_hash = hashlib.sha256(b"hi\n").hexdigest()
        assert r.output_hash == expected_hash

    def test_workload_spec_hash_present(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        r = s.run(_make_workload())
        assert isinstance(r.workload_spec_hash, str)
        assert len(r.workload_spec_hash) == 64  # sha256 hex

    def test_cost_usd_reflects_wall_time(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        r = s.run(_make_workload())
        assert r.cost_usd >= 0
        # cost_usd is wall_time * rate, both small but consistent
        wt = r.evidence["wall_time_sec"]
        assert r.cost_usd == pytest.approx(wt * WHOLESALE_COST_PER_WALL_SECOND_USD)

    def test_python_uses_python_image(self, stub_signer):
        captured = {}

        def factory(**kw):
            captured.update(kw)
            return _ok_factory()(**kw)

        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=factory)
        s.run(_make_workload(language="python"))
        assert captured["image_tag"] == DEFAULT_PYTHON_IMAGE

    def test_node_uses_node_image(self, stub_signer):
        captured = {}

        def factory(**kw):
            captured.update(kw)
            return _ok_factory()(**kw)

        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=factory)
        s.run(_make_workload(language="node", code="console.log('hi')"))
        assert captured["image_tag"] == DEFAULT_NODE_IMAGE

    def test_custom_config_overrides_images(self, stub_signer):
        captured = {}

        def factory(**kw):
            captured.update(kw)
            return _ok_factory()(**kw)

        cfg = ModalConfig(
            image_python="python:3.11-bullseye",
            cpu_cores=0.5,
            memory_mb=1024,
        )
        s = ModalSubstrate(config=cfg, identity_signer=stub_signer, sandbox_factory=factory)
        s.run(_make_workload(language="python"))
        assert captured["image_tag"] == "python:3.11-bullseye"
        assert captured["cpu_cores"] == 0.5
        assert captured["memory_mb"] == 1024


# ============================================================================
# Evidence shape
# ============================================================================


class TestEvidenceShape:
    def test_evidence_has_all_required_fields(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        r = s.run(_make_workload())
        missing = EVIDENCE_REQUIRED_FIELDS - r.evidence.keys()
        assert missing == set(), f"missing: {missing}"

    def test_evidence_hashes_prefixed(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        r = s.run(_make_workload())
        assert r.evidence["stdout_hash"].startswith("sha256:")
        assert r.evidence["stderr_hash"].startswith("sha256:")

    def test_evidence_task_id_format(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        r = s.run(_make_workload())
        assert r.evidence["task_id"].startswith("modal-task-")
        # 12 hex chars after the prefix
        suffix = r.evidence["task_id"][len("modal-task-") :]
        assert len(suffix) == 12
        int(suffix, 16)  # parses as hex

    def test_evidence_registers_in_global_registry(self):
        # Importing the module already registered; smoke test the lookup.
        schema = EVIDENCE_REGISTRY.get(EVIDENCE_SCHEMA_ID)
        assert schema.schema_id == EVIDENCE_SCHEMA_ID
        assert schema.required_fields == EVIDENCE_REQUIRED_FIELDS


# ============================================================================
# Evidence validator
# ============================================================================


class TestEvidenceValidator:
    def _valid_evidence(self) -> dict:
        empty_sha = "sha256:" + "0" * 64
        return {
            "sandbox_id": "sb-xyz",
            "task_id": "modal-task-abcdef012345",
            "image_tag": "python:3.12-slim",
            "exit_code": 0,
            "container_status": "ok",
            "wall_time_sec": 1.5,
            "stdout_hash": empty_sha,
            "stderr_hash": empty_sha,
        }

    def test_valid_evidence_passes(self):
        EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, self._valid_evidence())

    def test_missing_required_field_rejected(self):
        ev = self._valid_evidence()
        del ev["sandbox_id"]
        with pytest.raises(EvidenceSchemaError, match="missing"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, ev)

    def test_non_int_exit_code_rejected(self):
        ev = self._valid_evidence()
        ev["exit_code"] = "0"  # string, not int
        with pytest.raises(EvidenceSchemaError, match="exit_code"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, ev)

    def test_unprefixed_stdout_hash_rejected(self):
        ev = self._valid_evidence()
        ev["stdout_hash"] = "0" * 64
        with pytest.raises(EvidenceSchemaError, match="stdout_hash"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, ev)

    def test_invalid_container_status_rejected(self):
        ev = self._valid_evidence()
        ev["container_status"] = "weird"
        with pytest.raises(EvidenceSchemaError, match="container_status"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, ev)


# ============================================================================
# Failure modes
# ============================================================================


class TestFailureModes:
    def test_sandbox_error_raises_substrate_execution_error(self, stub_signer):
        s = ModalSubstrate(
            identity_signer=stub_signer,
            sandbox_factory=_failing_factory(container_status="error", exit_code=1),
        )
        with pytest.raises(SubstrateExecutionError) as exc:
            s.run(_make_workload())
        assert exc.value.partial_evidence["container_status"] == "error"
        assert exc.value.partial_evidence["exit_code"] == 1

    def test_killed_sandbox_raises_substrate_execution_error(self, stub_signer):
        s = ModalSubstrate(
            identity_signer=stub_signer,
            sandbox_factory=_failing_factory(container_status="killed", exit_code=-1),
        )
        with pytest.raises(SubstrateExecutionError):
            s.run(_make_workload())

    def test_partial_evidence_is_complete(self, stub_signer):
        s = ModalSubstrate(
            identity_signer=stub_signer,
            sandbox_factory=_failing_factory(container_status="timeout"),
        )
        try:
            s.run(_make_workload())
        except SubstrateExecutionError as e:
            missing = EVIDENCE_REQUIRED_FIELDS - e.partial_evidence.keys()
            assert missing == set(), f"missing: {missing}"

    def test_unsupported_language_in_run_rejected(self, stub_signer):
        s = ModalSubstrate(identity_signer=stub_signer, sandbox_factory=_ok_factory())
        # run() defends against direct unsupported-language calls.
        with pytest.raises(PreflightRejected, match="supports"):
            s.run(_make_workload(language="ruby"))
