"""
tests.test_class_keys
=====================

Unit tests for `darwin.agenticcloud.class_keys`.

Server endpoint tests (`tests/test_server_signing.py`) cover the happy
path through the HTTP boundary. This file covers behaviors only
exercisable at the store layer:

- generate_class_key + rotate_class_key flows
- ClassKeyStore.sign with allowlist enforcement (server reaches same conclusion)
- Keylist publication with mixed active + rotated keys
- Refusal to load rogue PEMs for non-allowlisted substrates
- Defensive: refusing to overwrite an existing active key

No HTTP, no FastAPI, no Docker.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from darwin.agenticcloud.class_keys import (
    ALLOWED_SUBSTRATES,
    KEYLIST_SCHEMA_URI,
    ClassKeyError,
    ClassKeyNotFound,
    ClassKeyStore,
    SubstrateNotAllowed,
    generate_class_key,
    rotate_class_key,
)

# ============================================================================
# Helpers
# ============================================================================


def _write_rogue_pem(keys_dir: Path, substrate_id: str) -> Path:
    """Write a valid Ed25519 PEM for a substrate (allowlisted or not).
    Used to test defense-in-depth — the store must refuse to load rogue
    PEMs even if they're cryptographically valid."""
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path = keys_dir / f"{substrate_id}.pem"
    pem_path.write_bytes(pem)
    return pem_path


# ============================================================================
# generate_class_key
# ============================================================================


class TestGenerateClassKey:
    def test_generates_pem_at_expected_path(self, tmp_path):
        keys_dir = tmp_path / "keys"
        pem_path, _signer_key_id = generate_class_key(keys_dir, "local-docker-v0")
        assert pem_path == keys_dir / "local-docker-v0.pem"
        assert pem_path.exists()

    def test_returned_signer_key_id_format(self, tmp_path):
        keys_dir = tmp_path / "keys"
        _, signer_key_id = generate_class_key(keys_dir, "local-docker-v0")
        assert signer_key_id.startswith("dac-class-local-docker-v0-")
        assert len(signer_key_id) - len("dac-class-local-docker-v0-") == 16

    def test_pem_is_pkcs8_ed25519(self, tmp_path):
        keys_dir = tmp_path / "keys"
        pem_path, _ = generate_class_key(keys_dir, "local-docker-v0")
        with pem_path.open("rb") as f:
            loaded = serialization.load_pem_private_key(f.read(), password=None)
        assert isinstance(loaded, Ed25519PrivateKey)

    def test_file_mode_is_owner_only(self, tmp_path):
        keys_dir = tmp_path / "keys"
        pem_path, _ = generate_class_key(keys_dir, "local-docker-v0")
        # 0o600 = owner read+write only. Anything broader is a leak risk.
        mode = pem_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_refuses_to_overwrite_existing_active_key(self, tmp_path):
        """Generating over an existing key would silently invalidate every
        attestation signed by the old key. We refuse and require an
        explicit rotation."""
        keys_dir = tmp_path / "keys"
        generate_class_key(keys_dir, "local-docker-v0")
        with pytest.raises(ClassKeyError, match="already exists"):
            generate_class_key(keys_dir, "local-docker-v0")

    def test_refuses_non_allowlisted_substrate(self, tmp_path):
        keys_dir = tmp_path / "keys"
        with pytest.raises(SubstrateNotAllowed):
            generate_class_key(keys_dir, "aws-lambda-us-east-1")

    def test_creates_keys_dir_if_missing(self, tmp_path):
        keys_dir = tmp_path / "does-not-exist-yet"
        generate_class_key(keys_dir, "local-docker-v0")
        assert keys_dir.exists()


# ============================================================================
# rotate_class_key
# ============================================================================


class TestRotateClassKey:
    def test_rotation_moves_old_key_to_archive(self, tmp_path):
        keys_dir = tmp_path / "keys"
        old_path, _old_kid = generate_class_key(keys_dir, "local-docker-v0")
        old_pem_bytes = old_path.read_bytes()
        # Ensure rotation timestamp differs from creation.
        time.sleep(1.01)
        new_path, _new_kid = rotate_class_key(keys_dir, "local-docker-v0")
        # Archive directory exists with exactly one rotated PEM.
        archive_dir = keys_dir / "local-docker-v0.pem.rotated"
        assert archive_dir.exists()
        archived = list(archive_dir.glob("*.pem"))
        assert len(archived) == 1
        # The archived PEM contains the OLD key bytes.
        assert archived[0].read_bytes() == old_pem_bytes
        # New active key exists at the canonical path with new bytes.
        assert new_path == keys_dir / "local-docker-v0.pem"
        assert new_path.exists()
        assert new_path.read_bytes() != old_pem_bytes

    def test_rotation_produces_different_signer_key_id(self, tmp_path):
        keys_dir = tmp_path / "keys"
        _, old_kid = generate_class_key(keys_dir, "local-docker-v0")
        time.sleep(1.01)
        _, new_kid = rotate_class_key(keys_dir, "local-docker-v0")
        assert old_kid != new_kid

    def test_rotation_without_active_key_raises(self, tmp_path):
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        with pytest.raises(ClassKeyNotFound):
            rotate_class_key(keys_dir, "local-docker-v0")

    def test_rotation_refuses_non_allowlisted_substrate(self, tmp_path):
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        with pytest.raises(SubstrateNotAllowed):
            rotate_class_key(keys_dir, "aws-lambda-us-east-1")


# ============================================================================
# ClassKeyStore.sign
# ============================================================================


class TestClassKeyStoreSign:
    def test_signs_with_active_key(self, tmp_path):
        keys_dir = tmp_path / "keys"
        _, expected_kid = generate_class_key(keys_dir, "local-docker-v0")
        store = ClassKeyStore(keys_dir=keys_dir)
        sig, kid = store.sign("local-docker-v0", b"some payload")
        assert len(sig) == 64
        assert kid == expected_kid

    def test_sign_refuses_non_allowlisted_substrate(self, tmp_path):
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        store = ClassKeyStore(keys_dir=keys_dir)
        with pytest.raises(SubstrateNotAllowed, match="allowlist"):
            store.sign("aws-lambda-us-east-1", b"x")

    def test_sign_raises_when_no_active_key(self, tmp_path):
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        store = ClassKeyStore(keys_dir=keys_dir)
        with pytest.raises(ClassKeyNotFound):
            store.sign("local-docker-v0", b"x")

    def test_has_active_key_reports_correctly(self, tmp_path):
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        store = ClassKeyStore(keys_dir=keys_dir)
        assert store.has_active_key("local-docker-v0") is False
        generate_class_key(keys_dir, "local-docker-v0")
        store.reload()
        assert store.has_active_key("local-docker-v0") is True

    def test_caches_loaded_keys_until_reload(self, tmp_path):
        """First call to sign() loads from disk; subsequent calls don't
        re-read the file. After reload() the cache is dropped."""
        keys_dir = tmp_path / "keys"
        generate_class_key(keys_dir, "local-docker-v0")
        store = ClassKeyStore(keys_dir=keys_dir)
        sig1, _ = store.sign("local-docker-v0", b"x")
        # Delete the file on disk. If the store doesn't cache, this would
        # break the next sign call. If it caches, this still works.
        (keys_dir / "local-docker-v0.pem").unlink()
        sig2, _ = store.sign("local-docker-v0", b"x")
        # Both sigs over the same payload with the same key — Ed25519 is
        # deterministic, so they match exactly.
        assert sig1 == sig2
        # After reload, the missing file causes a ClassKeyNotFound.
        store.reload()
        with pytest.raises(ClassKeyNotFound):
            store.sign("local-docker-v0", b"x")


# ============================================================================
# Defense in depth: rogue PEMs
# ============================================================================


class TestRoguePemDefense:
    def test_rogue_pem_for_non_allowlisted_substrate_is_ignored(self, tmp_path):
        """If an attacker drops a valid PEM into the keys dir for a
        substrate that isn't in the allowlist, the store must NOT load
        it. Without this defense, the store would happily sign with the
        rogue key the moment the substrate gets added to the allowlist
        in a future release."""
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        _write_rogue_pem(keys_dir, "aws-lambda-us-east-1")
        # Also add a legitimate key so the store has SOMETHING to load.
        generate_class_key(keys_dir, "local-docker-v0")
        store = ClassKeyStore(keys_dir=keys_dir)
        # The rogue key is silently ignored.
        with pytest.raises(SubstrateNotAllowed):
            store.sign("aws-lambda-us-east-1", b"x")
        # The legitimate key still signs.
        sig, _ = store.sign("local-docker-v0", b"x")
        assert len(sig) == 64


# ============================================================================
# Keylist publication
# ============================================================================


class TestKeylist:
    def test_keylist_schema_uri(self, tmp_path):
        keys_dir = tmp_path / "keys"
        generate_class_key(keys_dir, "local-docker-v0")
        store = ClassKeyStore(keys_dir=keys_dir)
        keylist = store.keylist()
        assert keylist["schema"] == KEYLIST_SCHEMA_URI

    def test_keylist_includes_active_key(self, tmp_path):
        keys_dir = tmp_path / "keys"
        _, signer_key_id = generate_class_key(keys_dir, "local-docker-v0")
        store = ClassKeyStore(keys_dir=keys_dir)
        keylist = store.keylist()
        active = [
            k
            for k in keylist["keys"]
            if k["status"] == "active" and k["substrate_id"] == "local-docker-v0"
        ]
        assert len(active) == 1
        assert active[0]["signer_key_id"] == signer_key_id
        assert "public_key_b64" in active[0]
        assert "created_at" in active[0]

    def test_keylist_includes_rotated_keys(self, tmp_path):
        keys_dir = tmp_path / "keys"
        _, kid_old = generate_class_key(keys_dir, "local-docker-v0")
        time.sleep(1.01)
        _, kid_new = rotate_class_key(keys_dir, "local-docker-v0")
        store = ClassKeyStore(keys_dir=keys_dir)
        keylist = store.keylist()
        entries = {k["signer_key_id"]: k for k in keylist["keys"]}
        # Both keys present.
        assert kid_old in entries
        assert kid_new in entries
        # Statuses correct.
        assert entries[kid_new]["status"] == "active"
        assert entries[kid_old]["status"] == "rotated"
        assert "rotated_at" in entries[kid_old]
        assert "rotated_at" not in entries[kid_new]

    def test_keylist_empty_when_no_keys(self, tmp_path):
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        store = ClassKeyStore(keys_dir=keys_dir)
        keylist = store.keylist()
        assert keylist["keys"] == []

    def test_keylist_does_not_include_rogue_rotated_keys(self, tmp_path):
        """Rotated archive for a non-allowlisted substrate is ignored."""
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        rogue_archive = keys_dir / "aws-lambda-us-east-1.pem.rotated"
        rogue_archive.mkdir(parents=True)
        # Write a valid PEM into the rogue archive.
        priv = Ed25519PrivateKey.generate()
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        (rogue_archive / "2026-01-01T00-00-00Z.pem").write_bytes(pem)
        store = ClassKeyStore(keys_dir=keys_dir)
        keylist = store.keylist()
        # No entries for the non-allowlisted substrate.
        assert all(
            k["substrate_id"] == "local-docker-v0" or k["substrate_id"] in ALLOWED_SUBSTRATES
            for k in keylist["keys"]
        )
        rogue_entries = [k for k in keylist["keys"] if k["substrate_id"] == "aws-lambda-us-east-1"]
        assert rogue_entries == []


# ============================================================================
# Missing keys directory
# ============================================================================


class TestMissingKeysDir:
    def test_store_handles_missing_dir_gracefully(self, tmp_path):
        """A freshly-deployed server with no keys dir yet must not crash.
        It just has zero active keys until bootstrap runs."""
        keys_dir = tmp_path / "does-not-exist"
        store = ClassKeyStore(keys_dir=keys_dir)
        assert store.has_active_key("local-docker-v0") is False
        assert store.keylist()["keys"] == []
        with pytest.raises(ClassKeyNotFound):
            store.sign("local-docker-v0", b"x")
