"""
tests.test_server_signing
=========================

Tests for the Phase 2 substrate-identity signing endpoints on the FastAPI
server:

- POST /v0/sign-substrate-identity
- GET  /.well-known/substrate-keys.json

End-to-end coverage:
- Generate class keys in a tmp dir.
- Spin up the FastAPI app with an explicit ClassKeyStore pointing at the
  tmp dir.
- Hit the endpoints via TestClient.
- Verify signatures with the public key from the keylist.
- Confirm every validation gate (domain separator, substrate match,
  allowlist, size cap, base64 sanity) rejects malformed inputs.
- Confirm the keylist shape matches the spec.

No network. No Fly. No new deps.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from darwin.agenticcloud.class_keys import (
    ALLOWED_SUBSTRATES,
    ClassKeyStore,
    generate_class_key,
)
from darwin.agenticcloud.hashing import canonical_json
from darwin.agenticcloud.server import create_app
from darwin.agenticcloud.signing import verify_signature
from darwin.agenticcloud.substrate.base import (
    IDENTITY_DOMAIN_SEPARATOR,
    build_identity_payload,
)
from darwin.agenticcloud.substrate.identity import (
    ED25519_SIGNATURE_BYTES,
    MAX_PAYLOAD_BYTES,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def keys_dir(tmp_path) -> Path:
    """Empty class-keys directory."""
    d = tmp_path / "class-keys"
    d.mkdir()
    return d


@pytest.fixture
def keys_dir_with_local_docker(keys_dir) -> Path:
    """Class-keys dir with an active local-docker-v0 key generated."""
    generate_class_key(keys_dir, "local-docker-v0")
    return keys_dir


@pytest.fixture
def client(keys_dir_with_local_docker, tmp_path, monkeypatch) -> TestClient:
    """FastAPI TestClient with a class-key store pointing at our tmp dir.
    Also isolates the runtime's state dir to avoid touching ~/.darwin."""
    monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path / "state"))
    cks = ClassKeyStore(keys_dir=keys_dir_with_local_docker)
    app = create_app(class_key_store=cks)
    return TestClient(app)


@pytest.fixture
def client_no_keys(keys_dir, tmp_path, monkeypatch) -> TestClient:
    """FastAPI TestClient with an empty class-keys dir (no active keys)."""
    monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path / "state"))
    cks = ClassKeyStore(keys_dir=keys_dir)
    app = create_app(class_key_store=cks)
    return TestClient(app)


def _valid_payload_bytes(
    substrate_id: str = "local-docker-v0",
    output_hash: str = "sha256:" + "b" * 64,
) -> bytes:
    """Build a real JCS-canonical identity payload."""
    payload = build_identity_payload(
        substrate_id=substrate_id,
        substrate_version="0.1.0",
        workload_spec_hash="sha256:" + "a" * 64,
        output_hash=output_hash,
        evidence_schema_id="darwin.cloud/evidence/local-docker/v1",
        issued_at="2026-06-15T12:00:00Z",
    )
    return canonical_json(payload)


# ============================================================================
# POST /v0/sign-substrate-identity — happy path
# ============================================================================


class TestSignSubstrateIdentityHappyPath:
    def test_returns_200_with_valid_payload(self, client):
        payload = _valid_payload_bytes()
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        assert resp.status_code == 200, resp.text

    def test_response_shape(self, client):
        payload = _valid_payload_bytes()
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        data = resp.json()
        assert set(data.keys()) == {"substrate_id", "signer_key_id", "signature_b64"}
        assert data["substrate_id"] == "local-docker-v0"
        assert data["signer_key_id"].startswith("dac-class-local-docker-v0-")
        # 64-byte Ed25519 sig -> 88 base64 chars (with padding).
        sig_bytes = base64.b64decode(data["signature_b64"])
        assert len(sig_bytes) == ED25519_SIGNATURE_BYTES

    def test_signature_verifies_against_keylist_public_key(self, client):
        """End-to-end: sign via the endpoint, fetch the public key from
        .well-known, verify the signature. This is exactly what a verifier
        does in production."""
        payload = _valid_payload_bytes()
        sign_resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        sign_data = sign_resp.json()
        keylist_resp = client.get("/.well-known/substrate-keys.json")
        keylist = keylist_resp.json()

        # Find the active key for local-docker-v0.
        matching = [k for k in keylist["keys"] if k["signer_key_id"] == sign_data["signer_key_id"]]
        assert len(matching) == 1, "signer_key_id must appear exactly once in keylist"
        pub_b64 = matching[0]["public_key_b64"]

        # Verify the signature against the canonical payload.
        assert verify_signature(payload, sign_data["signature_b64"], pub_b64) is True


# ============================================================================
# POST /v0/sign-substrate-identity — validation failures
# ============================================================================


class TestSignSubstrateIdentityValidation:
    def test_rejects_invalid_base64(self, client):
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": "!!!not base64!!!",
            },
        )
        assert resp.status_code == 400
        assert "base64" in resp.json()["detail"].lower()

    def test_rejects_empty_payload(self, client):
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={"substrate_id": "local-docker-v0", "payload_b64": ""},
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_rejects_oversized_payload(self, client):
        big = b"x" * (MAX_PAYLOAD_BYTES + 1)
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(big).decode("ascii"),
            },
        )
        assert resp.status_code == 400
        assert "exceeds max size" in resp.json()["detail"]

    def test_rejects_non_json_payload(self, client):
        garbage = b"\xff\xfenot json"
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(garbage).decode("ascii"),
            },
        )
        assert resp.status_code == 400

    def test_rejects_non_object_payload(self, client):
        payload = json.dumps(["not", "an", "object"]).encode("utf-8")
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        assert resp.status_code == 400
        assert "JSON object" in resp.json()["detail"]

    def test_rejects_wrong_domain_separator(self, client):
        payload = json.dumps(
            {
                "domain": "evil.example.com/some-other-domain/v1",
                "substrate_id": "local-docker-v0",
            }
        ).encode("utf-8")
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        assert resp.status_code == 400
        assert "domain mismatch" in resp.json()["detail"]

    def test_rejects_substrate_id_mismatch(self, client):
        """Payload claims one substrate; request claims another. The
        endpoint refuses to sign — defends against confused-deputy."""
        payload = json.dumps(
            {
                "domain": IDENTITY_DOMAIN_SEPARATOR,
                "substrate_id": "some-other-substrate",
            }
        ).encode("utf-8")
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        assert resp.status_code == 400
        assert "does not match" in resp.json()["detail"]

    def test_rejects_non_allowlisted_substrate(self, client):
        """Substrate not in ALLOWED_SUBSTRATES → 400 with clear reason."""
        # Build a payload with a not-yet-built substrate.
        payload = _valid_payload_bytes(substrate_id="e2b-v0")
        resp = client.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "e2b-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        assert resp.status_code == 400
        assert "allowlist" in resp.json()["detail"]

    def test_503_when_no_active_key_for_substrate(self, client_no_keys):
        """Substrate in allowlist but no key on disk → 503 (server not
        provisioned for this substrate yet)."""
        payload = _valid_payload_bytes()
        resp = client_no_keys.post(
            "/v0/sign-substrate-identity",
            json={
                "substrate_id": "local-docker-v0",
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            },
        )
        assert resp.status_code == 503
        assert "No active class key" in resp.json()["detail"]


# ============================================================================
# GET /.well-known/substrate-keys.json
# ============================================================================


class TestWellKnownKeylist:
    def test_returns_200(self, client):
        resp = client.get("/.well-known/substrate-keys.json")
        assert resp.status_code == 200

    def test_schema_uri_is_correct(self, client):
        resp = client.get("/.well-known/substrate-keys.json")
        data = resp.json()
        assert data["schema"] == "darwin.cloud/agenticcloud/substrate-keys/v1"

    def test_top_level_shape(self, client):
        resp = client.get("/.well-known/substrate-keys.json")
        data = resp.json()
        assert set(data.keys()) == {"schema", "issued_at", "keys"}
        assert isinstance(data["keys"], list)

    def test_active_key_entry_shape(self, client):
        resp = client.get("/.well-known/substrate-keys.json")
        data = resp.json()
        entries = [k for k in data["keys"] if k["substrate_id"] == "local-docker-v0"]
        assert len(entries) == 1
        entry = entries[0]
        assert set(entry.keys()) >= {
            "substrate_id",
            "signer_key_id",
            "public_key_b64",
            "status",
            "created_at",
        }
        assert entry["status"] == "active"
        # Public key is 32 raw bytes -> 44 base64 chars with padding.
        pub_bytes = base64.b64decode(entry["public_key_b64"])
        assert len(pub_bytes) == 32

    def test_keylist_empty_when_no_keys_present(self, client_no_keys):
        resp = client_no_keys.get("/.well-known/substrate-keys.json")
        data = resp.json()
        assert data["keys"] == []

    def test_keylist_signer_key_id_format(self, client):
        resp = client.get("/.well-known/substrate-keys.json")
        data = resp.json()
        for entry in data["keys"]:
            assert entry["signer_key_id"].startswith(f"dac-class-{entry['substrate_id']}-")


# ============================================================================
# Pre-existing Phase 1 routes still work
# ============================================================================


class TestPhase1RoutesIntact:
    """Sanity: ensure the server patch didn't break existing routes."""

    def test_healthz_still_works(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_identity_still_works(self, client):
        resp = client.get("/v0/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert "key_id" in data
        assert "public_key_b64" in data
        assert "substrate_id" in data


# ============================================================================
# Substrate allowlist is enforced from class_keys, not the endpoint
# ============================================================================


class TestAllowlistConsistency:
    def test_allowlist_includes_v300_substrates(self):
        """Sanity: Phase 2 v3.0.0 allowlist covers local-docker-v0,
        the four aws-lambda regions, modal-v0, and akash-v0. When new
        substrates are added in v3.1+, this test gets updated alongside
        the allowlist."""
        assert frozenset({
            "local-docker-v0",
            "aws-lambda-us-east-1",
            "aws-lambda-us-west-2",
            "aws-lambda-eu-west-1",
            "aws-lambda-ap-northeast-1",
            "modal-v0",
            "akash-v0",
        }) == ALLOWED_SUBSTRATES
