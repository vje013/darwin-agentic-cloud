"""
tests.substrate.test_identity
=============================

Tests for `darwin.agenticcloud.substrate.identity`.

Coverage:
- OperatorFallbackSigner: round-trip with the real Phase 1 Signer using a
  temp-dir key. Confirms signatures verify with the operator's public key.
- RemoteClassKeySigner: happy path with mocked urlopen; typed errors on
  every failure mode (unreachable, 4xx, 5xx, timeout, malformed response,
  wrong substrate, wrong signature length).
- resolve_identity_signer factory: env var routing.
- Protocol conformance: both signers satisfy SubstrateIdentitySigner.
- End-to-end: sign with OperatorFallbackSigner, embed in
  SubstrateIdentity, verify the signature with the operator's public key.

No network. No Docker. No new pytest deps beyond unittest.mock.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from typing import Any
from unittest.mock import patch

import pytest

from darwin.agenticcloud.hashing import canonical_json
from darwin.agenticcloud.signing import Signer, verify_signature
from darwin.agenticcloud.substrate.base import (
    IDENTITY_DOMAIN_SEPARATOR,
    build_identity_payload,
    iso8601_now,
    sign_identity,
)
from darwin.agenticcloud.substrate.identity import (
    DEFAULT_SIGNER_URL,
    ED25519_SIGNATURE_BYTES,
    MAX_PAYLOAD_BYTES,
    OperatorFallbackSigner,
    RemoteClassKeySigner,
    SubstrateSignerError,
    SubstrateSignerProtocolError,
    SubstrateSignerRejected,
    SubstrateSignerUnreachable,
    class_key_id,
    resolve_identity_signer,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_identity_payload_bytes(
    *,
    substrate_id: str = "fake-substrate-v0",
    output_hash: str = "sha256:" + "b" * 64,
) -> bytes:
    """Build a real JCS-canonical identity payload for signing tests."""
    payload = build_identity_payload(
        substrate_id=substrate_id,
        substrate_version="0.0.1",
        workload_spec_hash="sha256:" + "a" * 64,
        output_hash=output_hash,
        evidence_schema_id="darwin.cloud/evidence/fake-substrate/v1",
        issued_at="2026-06-15T12:00:00Z",
    )
    return canonical_json(payload)


def _mock_urlopen_response(
    *,
    status: int = 200,
    body: dict[str, Any] | bytes | None = None,
):
    """Build a context-manager-compatible mock that mimics urlopen's return."""

    class _MockResp:
        def __init__(self, status_code: int, payload: bytes):
            self.status = status_code
            self._payload = payload

        def read(self, _n: int | None = None) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    if body is None:
        payload = b""
    elif isinstance(body, bytes):
        payload = body
    else:
        payload = json.dumps(body).encode("utf-8")

    return _MockResp(status, payload)


# ============================================================================
# OperatorFallbackSigner
# ============================================================================


class TestOperatorFallbackSigner:
    @pytest.fixture
    def temp_signer(self, tmp_path, monkeypatch):
        """Real Phase 1 Signer with a brand-new key in a temp dir."""
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        return Signer()

    def test_signer_type_is_operator_fallback(self, temp_signer):
        s = OperatorFallbackSigner(temp_signer)
        assert s.signer_type == "operator-fallback"

    def test_signer_key_id_matches_phase1_format(self, temp_signer):
        s = OperatorFallbackSigner(temp_signer)
        assert s.signer_key_id.startswith("dac-local-")
        assert len(s.signer_key_id) == len("dac-local-") + 16

    def test_signer_key_id_matches_wrapped_signer(self, temp_signer):
        s = OperatorFallbackSigner(temp_signer)
        assert s.signer_key_id == temp_signer.key_id()

    def test_public_key_b64_exposes_underlying(self, temp_signer):
        s = OperatorFallbackSigner(temp_signer)
        assert s.public_key_b64 == temp_signer.public_key_b64()

    def test_sign_returns_64_bytes(self, temp_signer):
        s = OperatorFallbackSigner(temp_signer)
        sig = s.sign(b"hello world")
        assert isinstance(sig, bytes)
        assert len(sig) == ED25519_SIGNATURE_BYTES

    def test_sign_round_trips_with_phase1_verify(self, temp_signer):
        """Signature produced by OperatorFallbackSigner must verify with
        Phase 1's verify_signature() using the wrapped key. This is the
        contract self-hosted verifiers depend on."""
        s = OperatorFallbackSigner(temp_signer)
        payload = _make_identity_payload_bytes()
        sig_bytes = s.sign(payload)
        sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
        assert verify_signature(payload, sig_b64, s.public_key_b64) is True

    def test_sign_rejects_oversized_payload(self, temp_signer):
        s = OperatorFallbackSigner(temp_signer)
        too_big = b"x" * (MAX_PAYLOAD_BYTES + 1)
        with pytest.raises(SubstrateSignerRejected, match="exceeds max size"):
            s.sign(too_big)

    def test_default_construction_uses_real_signer(self, tmp_path, monkeypatch):
        """OperatorFallbackSigner() with no arg should construct a real Signer."""
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        s = OperatorFallbackSigner()
        assert s.signer_type == "operator-fallback"
        # Sign and verify to confirm the default-constructed Signer works.
        sig = s.sign(b"test")
        assert len(sig) == ED25519_SIGNATURE_BYTES


# ============================================================================
# RemoteClassKeySigner — construction and config
# ============================================================================


class TestRemoteClassKeySignerConstruction:
    def test_default_url_from_constant(self, monkeypatch):
        monkeypatch.delenv("DARWIN_SIGNER_URL", raising=False)
        s = RemoteClassKeySigner(substrate_id="fake-substrate-v0")
        # Internal but worth pinning to catch drift.
        assert s._url == DEFAULT_SIGNER_URL.rstrip("/")

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("DARWIN_SIGNER_URL", "https://staging.example.com/")
        s = RemoteClassKeySigner(substrate_id="fake-substrate-v0")
        assert s._url == "https://staging.example.com"

    def test_explicit_url_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DARWIN_SIGNER_URL", "https://env.example.com")
        s = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://explicit.example.com",
        )
        assert s._url == "https://explicit.example.com"

    def test_empty_env_var_raises(self, monkeypatch):
        """Empty DARWIN_SIGNER_URL is the documented `force operator
        fallback` sentinel. Constructing a RemoteClassKeySigner anyway
        must raise — that's what `resolve_identity_signer` is for."""
        monkeypatch.setenv("DARWIN_SIGNER_URL", "")
        with pytest.raises(SubstrateSignerError, match="requires a signer URL"):
            RemoteClassKeySigner(substrate_id="fake-substrate-v0")

    def test_url_without_scheme_raises(self, monkeypatch):
        monkeypatch.delenv("DARWIN_SIGNER_URL", raising=False)
        with pytest.raises(SubstrateSignerError, match="must include scheme"):
            RemoteClassKeySigner(
                substrate_id="fake-substrate-v0",
                signer_url="example.com",
            )

    def test_signer_type_is_class_key(self):
        s = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://example.com",
        )
        assert s.signer_type == "darwin-class-key"

    def test_signer_key_id_before_sign_is_unresolved(self):
        s = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://example.com",
        )
        assert s.signer_key_id == "dac-class-fake-substrate-v0-unresolved"


# ============================================================================
# RemoteClassKeySigner — happy path
# ============================================================================


class TestRemoteClassKeySignerHappyPath:
    def test_sign_returns_signature_from_server(self):
        s = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://signer.example.com",
        )
        payload = _make_identity_payload_bytes()
        fake_sig = b"\xab" * ED25519_SIGNATURE_BYTES
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            return_value=_mock_urlopen_response(
                body={
                    "signature_b64": base64.b64encode(fake_sig).decode("ascii"),
                    "signer_key_id": "dac-class-fake-substrate-v0-abc123def4567890",
                    "substrate_id": "fake-substrate-v0",
                }
            ),
        ):
            sig = s.sign(payload)
        assert sig == fake_sig

    def test_sign_resolves_signer_key_id_from_response(self):
        s = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://signer.example.com",
        )
        payload = _make_identity_payload_bytes()
        fake_sig = b"\xab" * ED25519_SIGNATURE_BYTES
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            return_value=_mock_urlopen_response(
                body={
                    "signature_b64": base64.b64encode(fake_sig).decode("ascii"),
                    "signer_key_id": "dac-class-fake-substrate-v0-aaaabbbbccccdddd",
                    "substrate_id": "fake-substrate-v0",
                }
            ),
        ):
            s.sign(payload)
        # After signing, signer_key_id reflects what the server returned.
        assert s.signer_key_id == "dac-class-fake-substrate-v0-aaaabbbbccccdddd"

    def test_request_body_includes_substrate_id_and_payload(self):
        s = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://signer.example.com",
        )
        payload = _make_identity_payload_bytes()
        fake_sig = b"\xab" * ED25519_SIGNATURE_BYTES
        captured = {}

        def _capture(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["method"] = req.get_method()
            return _mock_urlopen_response(
                body={
                    "signature_b64": base64.b64encode(fake_sig).decode("ascii"),
                    "signer_key_id": "dac-class-fake-substrate-v0-1234567890abcdef",
                    "substrate_id": "fake-substrate-v0",
                }
            )

        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            side_effect=_capture,
        ):
            s.sign(payload)

        assert captured["url"] == "https://signer.example.com/v0/sign-substrate-identity"
        assert captured["method"] == "POST"
        body = json.loads(captured["body"])
        assert body["substrate_id"] == "fake-substrate-v0"
        # The payload roundtrips through base64.
        assert base64.b64decode(body["payload_b64"]) == payload


# ============================================================================
# RemoteClassKeySigner — failure modes
# ============================================================================


class TestRemoteClassKeySignerFailures:
    @pytest.fixture
    def signer(self):
        return RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://signer.example.com",
        )

    @pytest.fixture
    def payload(self):
        return _make_identity_payload_bytes()

    def test_oversized_payload_raises_rejected(self, signer):
        with pytest.raises(SubstrateSignerRejected, match="exceeds max size"):
            signer.sign(b"x" * (MAX_PAYLOAD_BYTES + 1))

    def test_payload_without_domain_separator_raises_rejected(self, signer):
        bad = json.dumps({"substrate_id": "fake-substrate-v0"}).encode("utf-8")
        with pytest.raises(SubstrateSignerRejected, match="domain separator"):
            signer.sign(bad)

    def test_payload_with_wrong_substrate_id_raises_rejected(self, signer):
        bad = json.dumps(
            {
                "domain": IDENTITY_DOMAIN_SEPARATOR,
                "substrate_id": "other-substrate",
            }
        ).encode("utf-8")
        with pytest.raises(SubstrateSignerRejected, match="substrate_id mismatch"):
            signer.sign(bad)

    def test_non_json_payload_raises_rejected(self, signer):
        with pytest.raises(SubstrateSignerRejected, match="not valid UTF-8 JSON"):
            signer.sign(b"\xff\xfe not json")

    def test_server_503_raises_unreachable(self, signer, payload):
        err = urllib.error.HTTPError(
            url="https://signer.example.com/v0/sign-substrate-identity",
            code=503,
            msg="Service Unavailable",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"upstream broken"),
        )
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(SubstrateSignerUnreachable, match="HTTP 503"):
                signer.sign(payload)

    def test_server_400_raises_rejected(self, signer, payload):
        err = urllib.error.HTTPError(
            url="https://signer.example.com/v0/sign-substrate-identity",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"bad substrate_id"),
        )
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(SubstrateSignerRejected, match="400"):
                signer.sign(payload)

    def test_server_429_raises_rejected(self, signer, payload):
        """Rate limiting is a 4xx, so it surfaces as Rejected (not
        Unreachable). Callers can decide whether to retry."""
        err = urllib.error.HTTPError(
            url="https://signer.example.com/v0/sign-substrate-identity",
            code=429,
            msg="Too Many Requests",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"rate limited"),
        )
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(SubstrateSignerRejected, match="429"):
                signer.sign(payload)

    def test_url_error_raises_unreachable(self, signer, payload):
        err = urllib.error.URLError("nodename nor servname provided")
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(SubstrateSignerUnreachable, match="Could not reach"):
                signer.sign(payload)

    def test_timeout_raises_unreachable(self, signer, payload):
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with pytest.raises(SubstrateSignerUnreachable, match="timed out"):
                signer.sign(payload)

    def test_malformed_response_json_raises_protocol_error(self, signer, payload):
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            return_value=_mock_urlopen_response(body=b"not json"),
        ):
            with pytest.raises(SubstrateSignerProtocolError, match="not valid UTF-8 JSON"):
                signer.sign(payload)

    def test_response_missing_fields_raises_protocol_error(self, signer, payload):
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            return_value=_mock_urlopen_response(body={"signature_b64": "AAAA"}),
        ):
            with pytest.raises(SubstrateSignerProtocolError, match="missing required fields"):
                signer.sign(payload)

    def test_response_wrong_signature_length_raises_protocol_error(self, signer, payload):
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            return_value=_mock_urlopen_response(
                body={
                    "signature_b64": base64.b64encode(b"\x00" * 32).decode("ascii"),
                    "signer_key_id": "dac-class-fake-substrate-v0-abc123def4567890",
                    "substrate_id": "fake-substrate-v0",
                }
            ),
        ):
            with pytest.raises(SubstrateSignerProtocolError, match="wrong length"):
                signer.sign(payload)

    def test_response_wrong_substrate_id_raises_protocol_error(self, signer, payload):
        fake_sig = b"\xab" * ED25519_SIGNATURE_BYTES
        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            return_value=_mock_urlopen_response(
                body={
                    "signature_b64": base64.b64encode(fake_sig).decode("ascii"),
                    "signer_key_id": "dac-class-other-substrate-abc123def4567890",
                    "substrate_id": "other-substrate",
                }
            ),
        ):
            with pytest.raises(SubstrateSignerProtocolError, match="wrong substrate"):
                signer.sign(payload)


# ============================================================================
# resolve_identity_signer factory
# ============================================================================


class TestResolveIdentitySigner:
    def test_default_returns_remote(self, monkeypatch):
        monkeypatch.delenv("DARWIN_SIGNER_URL", raising=False)
        s = resolve_identity_signer("fake-substrate-v0")
        assert isinstance(s, RemoteClassKeySigner)
        assert s.signer_type == "darwin-class-key"

    def test_empty_env_returns_operator_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_SIGNER_URL", "")
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        s = resolve_identity_signer("fake-substrate-v0")
        assert isinstance(s, OperatorFallbackSigner)
        assert s.signer_type == "operator-fallback"

    def test_explicit_url_returns_remote(self, monkeypatch):
        monkeypatch.setenv("DARWIN_SIGNER_URL", "https://staging.example.com")
        s = resolve_identity_signer("fake-substrate-v0")
        assert isinstance(s, RemoteClassKeySigner)


# ============================================================================
# class_key_id helper
# ============================================================================


class TestClassKeyId:
    def test_format(self):
        kid = class_key_id("local-docker-v0", b"\x00" * 32)
        assert kid.startswith("dac-class-local-docker-v0-")
        assert len(kid) - len("dac-class-local-docker-v0-") == 16

    def test_deterministic_for_same_key(self):
        pub = b"\x42" * 32
        assert class_key_id("x", pub) == class_key_id("x", pub)

    def test_different_keys_yield_different_ids(self):
        kid_a = class_key_id("x", b"\x00" * 32)
        kid_b = class_key_id("x", b"\xff" * 32)
        assert kid_a != kid_b

    def test_different_substrates_yield_different_ids(self):
        pub = b"\x42" * 32
        kid_a = class_key_id("x", pub)
        kid_b = class_key_id("y", pub)
        # Same hex suffix (same key), different substrate prefix.
        assert kid_a != kid_b
        assert kid_a.endswith(kid_b.split("-")[-1])


# ============================================================================
# Protocol conformance
# ============================================================================


class TestProtocolConformance:
    def test_operator_fallback_has_protocol_attributes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        s = OperatorFallbackSigner()
        assert hasattr(s, "signer_type")
        assert hasattr(s, "signer_key_id")
        assert hasattr(s, "sign")
        assert callable(s.sign)

    def test_remote_has_protocol_attributes(self):
        s = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://example.com",
        )
        assert hasattr(s, "signer_type")
        assert hasattr(s, "signer_key_id")
        assert hasattr(s, "sign")
        assert callable(s.sign)


# ============================================================================
# End-to-end: sign_identity + OperatorFallbackSigner + verify
# ============================================================================


class TestEndToEnd:
    def test_sign_identity_with_operator_fallback_then_verify(self, tmp_path, monkeypatch):
        """The complete self-hosted flow: build an identity payload, sign
        it with OperatorFallbackSigner via sign_identity(), then verify
        the resulting signature using the Phase 1 verify_signature()."""
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        signer = OperatorFallbackSigner()

        from darwin.agenticcloud.substrate.base import RunResult

        result = RunResult(
            substrate_id="fake-substrate-v0",
            substrate_version="0.0.1",
            workload_spec_hash="sha256:" + "a" * 64,
            stdout="hi\n",
            stderr="",
            output_hash="sha256:" + "b" * 64,
            cost_usd=0.0001,
            evidence_schema_id="darwin.cloud/evidence/fake-substrate/v1",
            evidence={},
            extensions={},
            tee_required=False,
            issued_at=iso8601_now(),
        )

        identity = sign_identity(result=result, signer=signer)
        # Reconstruct the canonical bytes a verifier would build.
        from darwin.agenticcloud.substrate.base import build_identity_payload

        payload = build_identity_payload(
            substrate_id=result.substrate_id,
            substrate_version=result.substrate_version,
            workload_spec_hash=result.workload_spec_hash,
            output_hash=result.output_hash,
            evidence_schema_id=result.evidence_schema_id,
            issued_at=result.issued_at,
        )
        canonical = canonical_json(payload)
        assert (
            verify_signature(
                canonical,
                identity.identity_signature,
                signer.public_key_b64,
            )
            is True
        )

    def test_sign_identity_unreachable_remote_propagates(self, monkeypatch):
        """When the remote is unreachable, sign_identity must NOT silently
        fall back. The typed error propagates to the caller. This is the
        load-bearing 'no skim' guarantee."""
        signer = RemoteClassKeySigner(
            substrate_id="fake-substrate-v0",
            signer_url="https://signer.example.com",
        )
        from darwin.agenticcloud.substrate.base import RunResult

        result = RunResult(
            substrate_id="fake-substrate-v0",
            substrate_version="0.0.1",
            workload_spec_hash="sha256:" + "a" * 64,
            stdout="hi\n",
            stderr="",
            output_hash="sha256:" + "b" * 64,
            cost_usd=0.0001,
            evidence_schema_id="darwin.cloud/evidence/fake-substrate/v1",
            evidence={},
            extensions={},
            tee_required=False,
            issued_at=iso8601_now(),
        )

        with patch(
            "darwin.agenticcloud.substrate.identity.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(SubstrateSignerUnreachable):
                sign_identity(result=result, signer=signer)
