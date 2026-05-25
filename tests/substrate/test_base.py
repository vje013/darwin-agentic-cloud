"""
tests.substrate.test_base
=========================

Contract tests for `darwin.agenticcloud.substrate.base`.

Goals:
- Validate the evidence schema registry behavior (register, duplicate-detect, validate)
- Validate the substrate identity payload shape and signing flow with a
  fake signer that lets us inspect the bytes-to-sign
- Validate that `build_attestation_dict()` produces a structure that exactly
  matches Phase 1 spec Section 3.2 (v0.2 schema)
- Validate the substrate ABC: a concrete subclass with all abstracts
  implemented is instantiable; an incomplete one is not

These tests do NOT require Docker, AWS, Modal, or Akash. They run in the
default (non-`integration`) CI lane.

When AWS Lambda / Modal / Akash adapters land, each gets its OWN
`tests/substrate/test_<name>.py` that uses the conformance helpers below
to assert the adapter respects the contract.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

import pytest

from darwin.agenticcloud.substrate.base import (
    ATTESTATION_SCHEMA_URI,
    IDENTITY_DOMAIN_SEPARATOR,
    CostEstimate,
    EvidenceRegistry,
    EvidenceSchema,
    EvidenceSchemaError,
    RunResult,
    Substrate,
    SubstrateError,
    SubstrateExecutionError,
    SubstrateIdentity,
    SubstrateIdentitySigner,
    build_attestation_dict,
    build_identity_payload,
    iso8601_now,
    sign_identity,
)

# ============================================================================
# Helpers — fake signer, fake substrate, fake workload
# ============================================================================


class _FakeSigner:
    """Implements `SubstrateIdentitySigner` Protocol. Records the last
    payload signed so tests can verify the bytes-to-sign were JCS-canonical."""

    def __init__(
        self,
        signer_type: str = "darwin-class-key",
        signer_key_id: str = "dac-class-local-docker-v0-abc123",
    ):
        self._type = signer_type
        self._id = signer_key_id
        self.last_payload: bytes | None = None

    @property
    def signer_type(self) -> str:
        return self._type

    @property
    def signer_key_id(self) -> str:
        return self._id

    def sign(self, payload: bytes) -> bytes:
        self.last_payload = payload
        # Deterministic fake "signature" — the literal bytes the verifier
        # would never accept, but enough to test the wiring.
        return b"\x00" * 64


def _make_result(**overrides: Any) -> RunResult:
    """Build a RunResult with sensible defaults; override specific fields."""
    defaults: dict[str, Any] = {
        "substrate_id": "fake-substrate-v0",
        "substrate_version": "0.0.1",
        "workload_spec_hash": "sha256:" + "a" * 64,
        "stdout": "Hello, agent.\n",
        "stderr": "",
        "output_hash": "sha256:" + "b" * 64,
        "cost_usd": 0.000142,
        "evidence_schema_id": "darwin.cloud/evidence/fake-substrate/v1",
        "evidence": {"fake_request_id": "req_001", "fake_log_ref": "logref_001"},
        "extensions": {},
        "tee_required": False,
        "issued_at": "2026-06-15T12:00:00Z",
    }
    defaults.update(overrides)
    return RunResult(**defaults)


def _register_fake_evidence_schema(registry: EvidenceRegistry) -> EvidenceSchema:
    """Register the fake substrate's evidence schema in `registry`."""
    schema = EvidenceSchema(
        schema_id="darwin.cloud/evidence/fake-substrate/v1",
        required_fields=frozenset({"fake_request_id", "fake_log_ref"}),
        validator=lambda _evidence: None,
    )
    registry.register(schema)
    return schema


# ============================================================================
# EvidenceRegistry
# ============================================================================


class TestEvidenceRegistry:
    def test_register_and_get(self):
        reg = EvidenceRegistry()
        schema = EvidenceSchema(
            schema_id="darwin.cloud/evidence/test/v1",
            required_fields=frozenset({"foo"}),
            validator=lambda _: None,
        )
        reg.register(schema)
        assert reg.get("darwin.cloud/evidence/test/v1") is schema

    def test_register_idempotent_same_instance(self):
        reg = EvidenceRegistry()
        schema = EvidenceSchema(
            schema_id="darwin.cloud/evidence/test/v1",
            required_fields=frozenset({"foo"}),
            validator=lambda _: None,
        )
        reg.register(schema)
        reg.register(schema)  # Same instance — must not raise

    def test_register_duplicate_id_different_instance_raises(self):
        reg = EvidenceRegistry()
        reg.register(
            EvidenceSchema(
                schema_id="darwin.cloud/evidence/test/v1",
                required_fields=frozenset({"foo"}),
                validator=lambda _: None,
            )
        )
        with pytest.raises(EvidenceSchemaError, match="already registered"):
            reg.register(
                EvidenceSchema(
                    schema_id="darwin.cloud/evidence/test/v1",
                    required_fields=frozenset({"bar"}),  # different required set
                    validator=lambda _: None,
                )
            )

    def test_get_unknown_raises(self):
        reg = EvidenceRegistry()
        with pytest.raises(EvidenceSchemaError, match="Unknown evidence schema id"):
            reg.get("darwin.cloud/evidence/missing/v1")

    def test_validate_missing_required_field_raises(self):
        reg = EvidenceRegistry()
        reg.register(
            EvidenceSchema(
                schema_id="darwin.cloud/evidence/test/v1",
                required_fields=frozenset({"foo", "bar"}),
                validator=lambda _: None,
            )
        )
        with pytest.raises(EvidenceSchemaError, match=r"missing required fields.*'bar'"):
            reg.validate("darwin.cloud/evidence/test/v1", {"foo": 1})

    def test_validate_calls_custom_validator(self):
        called: list[Mapping[str, Any]] = []

        def custom(evidence: Mapping[str, Any]) -> None:
            called.append(evidence)
            if evidence["foo"] < 0:
                raise EvidenceSchemaError("foo must be non-negative")

        reg = EvidenceRegistry()
        reg.register(
            EvidenceSchema(
                schema_id="darwin.cloud/evidence/test/v1",
                required_fields=frozenset({"foo"}),
                validator=custom,
            )
        )
        reg.validate("darwin.cloud/evidence/test/v1", {"foo": 1})
        assert called == [{"foo": 1}]

        with pytest.raises(EvidenceSchemaError, match="non-negative"):
            reg.validate("darwin.cloud/evidence/test/v1", {"foo": -1})

    def test_known_ids_returns_frozenset(self):
        reg = EvidenceRegistry()
        reg.register(
            EvidenceSchema(
                schema_id="darwin.cloud/evidence/a/v1",
                required_fields=frozenset(),
                validator=lambda _: None,
            )
        )
        reg.register(
            EvidenceSchema(
                schema_id="darwin.cloud/evidence/b/v1",
                required_fields=frozenset(),
                validator=lambda _: None,
            )
        )
        ids = reg.known_ids()
        assert isinstance(ids, frozenset)
        assert ids == frozenset(
            {
                "darwin.cloud/evidence/a/v1",
                "darwin.cloud/evidence/b/v1",
            }
        )


# ============================================================================
# Identity payload + signing
# ============================================================================


class TestIdentityPayload:
    def test_payload_includes_domain_separator(self):
        payload = build_identity_payload(
            substrate_id="local-docker-v0",
            substrate_version="0.1.0",
            workload_spec_hash="sha256:" + "a" * 64,
            output_hash="sha256:" + "b" * 64,
            evidence_schema_id="darwin.cloud/evidence/local-docker/v1",
            issued_at="2026-06-15T12:00:00Z",
        )
        assert payload["domain"] == IDENTITY_DOMAIN_SEPARATOR

    def test_payload_field_set_is_exact(self):
        """If this test fails, RFC-0003 needs an update — the identity
        payload shape is a public contract."""
        payload = build_identity_payload(
            substrate_id="x",
            substrate_version="y",
            workload_spec_hash="z",
            output_hash="w",
            evidence_schema_id="u",
            issued_at="t",
        )
        assert set(payload.keys()) == {
            "domain",
            "substrate_id",
            "substrate_version",
            "workload_spec_hash",
            "output_hash",
            "evidence_schema_id",
            "issued_at",
        }


class TestSignIdentity:
    def test_sign_identity_happy_path(self):
        signer = _FakeSigner()
        result = _make_result()
        identity = sign_identity(result=result, signer=signer)
        assert isinstance(identity, SubstrateIdentity)
        assert identity.substrate_id == "fake-substrate-v0"
        assert identity.signer_type == "darwin-class-key"
        assert identity.signer_key_id == "dac-class-local-docker-v0-abc123"
        # base64-encoded 64-byte signature
        assert len(base64.b64decode(identity.identity_signature)) == 64

    def test_sign_identity_invokes_signer_on_jcs_canonical_payload(self):
        signer = _FakeSigner()
        result = _make_result()
        sign_identity(result=result, signer=signer)
        # The signer was called with the JCS-canonical encoding of the
        # identity payload. Decode and verify keys round-trip.
        assert signer.last_payload is not None
        decoded = json.loads(signer.last_payload.decode("utf-8"))
        assert decoded["domain"] == IDENTITY_DOMAIN_SEPARATOR
        assert decoded["substrate_id"] == "fake-substrate-v0"
        assert decoded["output_hash"] == "sha256:" + "b" * 64

    def test_sign_identity_requires_issued_at(self):
        signer = _FakeSigner()
        result = _make_result(issued_at="")
        with pytest.raises(SubstrateError, match="issued_at must be set"):
            sign_identity(result=result, signer=signer)

    def test_operator_fallback_signer_type_propagates(self):
        signer = _FakeSigner(
            signer_type="operator-fallback",
            signer_key_id="dac-local-d1bf7cad25875cee",
        )
        result = _make_result()
        identity = sign_identity(result=result, signer=signer)
        assert identity.signer_type == "operator-fallback"
        assert identity.signer_key_id == "dac-local-d1bf7cad25875cee"


# ============================================================================
# Attestation dict (spec section 3.2 conformance)
# ============================================================================


class TestBuildAttestationDict:
    @pytest.fixture
    def setup(self, monkeypatch):
        """Register the fake evidence schema on the process-global registry
        for the duration of the test, then clean up."""
        from darwin.agenticcloud.substrate.base import EVIDENCE_REGISTRY

        added = False
        schema_id = "darwin.cloud/evidence/fake-substrate/v1"
        if schema_id not in EVIDENCE_REGISTRY.known_ids():
            _register_fake_evidence_schema(EVIDENCE_REGISTRY)
            added = True
        yield
        # Best-effort cleanup. If two tests register concurrently this is fine
        # because of the idempotent-same-instance rule.
        if added:
            EVIDENCE_REGISTRY._schemas.pop(schema_id, None)  # type: ignore[attr-defined]

    def test_schema_uri_matches_spec(self, setup):
        result = _make_result()
        identity = sign_identity(result=result, signer=_FakeSigner())
        att = build_attestation_dict(
            attestation_id="att_01TESTTEST",
            result=result,
            identity=identity,
        )
        assert att["schema"] == ATTESTATION_SCHEMA_URI
        assert ATTESTATION_SCHEMA_URI == "darwin.cloud/agenticcloud/attestation/v0.2"

    def test_top_level_keys_match_spec(self, setup):
        """Spec Section 3.2 lists exactly these top-level keys in the
        to-be-signed attestation body."""
        result = _make_result()
        identity = sign_identity(result=result, signer=_FakeSigner())
        att = build_attestation_dict(
            attestation_id="att_01TESTTEST",
            result=result,
            identity=identity,
        )
        assert set(att.keys()) == {
            "attestation_id",
            "schema",
            "issued_at",
            "workload_spec_hash",
            "execution_result",
        }

    def test_execution_result_shape_matches_spec(self, setup):
        result = _make_result()
        identity = sign_identity(result=result, signer=_FakeSigner())
        att = build_attestation_dict(
            attestation_id="att_01TESTTEST",
            result=result,
            identity=identity,
        )
        er = att["execution_result"]
        assert set(er.keys()) == {
            "output_hash",
            "substrate",
            "cost_usd",
            "stdout",
            "stderr",
        }
        assert er["output_hash"] == result.output_hash
        assert er["cost_usd"] == result.cost_usd

    def test_substrate_block_includes_polymorphic_evidence(self, setup):
        result = _make_result()
        identity = sign_identity(result=result, signer=_FakeSigner())
        att = build_attestation_dict(
            attestation_id="att_01TESTTEST",
            result=result,
            identity=identity,
        )
        sub = att["execution_result"]["substrate"]
        assert sub["id"] == "fake-substrate-v0"
        assert sub["version"] == "0.0.1"
        assert sub["identity_signature"] == identity.identity_signature
        assert sub["identity_signer_type"] == "darwin-class-key"
        assert sub["evidence_schema_id"] == "darwin.cloud/evidence/fake-substrate/v1"
        assert sub["evidence"] == {
            "fake_request_id": "req_001",
            "fake_log_ref": "logref_001",
        }
        assert sub["extensions"] == {}
        assert sub["tee_required"] is False

    def test_extensions_map_round_trips(self, setup):
        """Phase 7 will write `extensions["tee.tdx.v1"]`. The base must not
        clobber unknown extension keys."""
        result = _make_result(
            extensions={"tee.tdx.v1": {"quote": "AAAA"}, "future.key": [1, 2, 3]},
        )
        identity = sign_identity(result=result, signer=_FakeSigner())
        att = build_attestation_dict(
            attestation_id="att_01TEST",
            result=result,
            identity=identity,
        )
        ext = att["execution_result"]["substrate"]["extensions"]
        assert ext["tee.tdx.v1"] == {"quote": "AAAA"}
        assert ext["future.key"] == [1, 2, 3]

    def test_evidence_must_satisfy_schema(self, setup):
        """If the substrate returns evidence missing required fields, the
        attestation builder refuses to embed it."""
        result = _make_result(evidence={"fake_request_id": "req_001"})  # missing log_ref
        identity = sign_identity(result=result, signer=_FakeSigner())
        with pytest.raises(EvidenceSchemaError, match="missing required fields"):
            build_attestation_dict(
                attestation_id="att_x",
                result=result,
                identity=identity,
            )

    def test_unknown_evidence_schema_raises(self, setup):
        result = _make_result(
            evidence_schema_id="darwin.cloud/evidence/never-registered/v1",
        )
        identity = sign_identity(result=result, signer=_FakeSigner())
        with pytest.raises(EvidenceSchemaError, match="Unknown evidence schema id"):
            build_attestation_dict(
                attestation_id="att_x",
                result=result,
                identity=identity,
            )

    def test_tee_required_flag_propagates(self, setup):
        result = _make_result(
            tee_required=True,
            extensions={"tee.tdx.v1": {"quote": "AAAA"}},
        )
        identity = sign_identity(result=result, signer=_FakeSigner())
        att = build_attestation_dict(
            attestation_id="att_tee",
            result=result,
            identity=identity,
        )
        assert att["execution_result"]["substrate"]["tee_required"] is True

    def test_evidence_and_extensions_are_copied_not_referenced(self, setup):
        """Mutating the input evidence dict after attestation construction
        must not change the attestation."""
        evidence = {"fake_request_id": "req_001", "fake_log_ref": "logref_001"}
        result = _make_result(evidence=evidence)
        identity = sign_identity(result=result, signer=_FakeSigner())
        att = build_attestation_dict(
            attestation_id="att_x",
            result=result,
            identity=identity,
        )
        evidence["fake_request_id"] = "MUTATED"
        assert att["execution_result"]["substrate"]["evidence"]["fake_request_id"] == "req_001"


# ============================================================================
# Substrate ABC conformance
# ============================================================================


class _ConcreteSubstrate(Substrate):
    """Minimal concrete substrate used to test ABC instantiability."""

    @property
    def substrate_id(self) -> str:
        return "concrete-test-v0"

    @property
    def substrate_version(self) -> str:
        return "0.0.1"

    @property
    def evidence_schema_id(self) -> str:
        return "darwin.cloud/evidence/concrete-test/v1"

    def preflight(self, workload):
        return CostEstimate(cost_usd_max=0.0001)

    def run(self, workload):
        return _make_result(
            substrate_id="concrete-test-v0",
            evidence_schema_id="darwin.cloud/evidence/concrete-test/v1",
        )

    def identity_signer(self) -> SubstrateIdentitySigner:
        return _FakeSigner()


class _IncompleteSubstrate(Substrate):
    """Missing `run` — must not be instantiable."""

    @property
    def substrate_id(self) -> str:
        return "incomplete-v0"

    @property
    def substrate_version(self) -> str:
        return "0.0.1"

    @property
    def evidence_schema_id(self) -> str:
        return "x"

    def preflight(self, workload):
        return CostEstimate(cost_usd_max=0.0)

    def identity_signer(self):
        return _FakeSigner()


class TestSubstrateABC:
    def test_concrete_subclass_instantiable(self):
        s = _ConcreteSubstrate()
        assert s.substrate_id == "concrete-test-v0"
        assert s.substrate_version == "0.0.1"

    def test_incomplete_subclass_not_instantiable(self):
        with pytest.raises(TypeError, match="abstract"):
            _IncompleteSubstrate()  # type: ignore[abstract]


# ============================================================================
# Errors carry partial evidence
# ============================================================================


class TestSubstrateExecutionError:
    def test_carries_partial_evidence(self):
        err = SubstrateExecutionError(
            "container exited with code 137 (OOM)",
            partial_evidence={"container_id": "abc123", "exit_code": 137},
        )
        assert err.partial_evidence["container_id"] == "abc123"
        assert err.partial_evidence["exit_code"] == 137

    def test_default_partial_evidence_is_empty_dict(self):
        err = SubstrateExecutionError("failed somehow")
        assert err.partial_evidence == {}


# ============================================================================
# iso8601_now
# ============================================================================


class TestIso8601Now:
    def test_format(self):
        ts = iso8601_now()
        # YYYY-MM-DDTHH:MM:SSZ — exactly 20 characters
        assert len(ts) == 20
        assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T"
        assert ts[13] == ":" and ts[16] == ":" and ts[19] == "Z"


# ============================================================================
# Conformance helper for downstream test files
# ============================================================================


def assert_substrate_conforms_to_v02(
    substrate: Substrate,
    *,
    expected_substrate_id: str,
    expected_evidence_schema_id: str,
) -> None:
    """Helper for `tests/substrate/test_<adapter>.py` files.

    Every concrete substrate adapter's tests should call this to confirm the
    adapter conforms to the v0.2 contract before getting into adapter-specific
    integration tests.
    """
    assert substrate.substrate_id == expected_substrate_id
    assert substrate.evidence_schema_id == expected_evidence_schema_id
    # The adapter's evidence schema must be in the global registry by the
    # time the substrate is instantiated.
    from darwin.agenticcloud.substrate.base import EVIDENCE_REGISTRY

    assert expected_evidence_schema_id in EVIDENCE_REGISTRY.known_ids(), (
        f"Substrate {expected_substrate_id} declares evidence schema "
        f"{expected_evidence_schema_id} but did not register it. "
        f"The adapter module must call EVIDENCE_REGISTRY.register() at import."
    )
    # Identity signer must conform to the Protocol.
    signer = substrate.identity_signer()
    assert hasattr(signer, "signer_type")
    assert hasattr(signer, "signer_key_id")
    assert hasattr(signer, "sign")
    assert signer.signer_type in {"darwin-class-key", "operator-fallback"}
