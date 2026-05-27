"""
tests.test_admin_cli
====================

Tests for `darwin.agenticcloud.admin_cli` and the `docker-entrypoint.sh`
bootstrap script.

Two layers:

- Unit tests for the admin CLI using typer.testing.CliRunner. Tests
  the `generate`, `rotate`, and `verify` commands against temp dirs
  and a mocked urlopen.
- Integration test for `docker-entrypoint.sh` that spawns the script
  in a subprocess with synthetic env vars and confirms the PEM
  materializes correctly. Skipped on non-POSIX hosts.

No real Fly. No real network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from darwin.agenticcloud.admin_cli import admin_app
from darwin.agenticcloud.class_keys import (
    ClassKeyStore,
    generate_class_key,
)

runner = CliRunner()


# ============================================================================
# Helpers
# ============================================================================


def _make_pem_bytes() -> bytes:
    priv = Ed25519PrivateKey.generate()
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


# ============================================================================
# generate
# ============================================================================


class TestGenerate:
    def test_generates_pem_at_expected_path(self, tmp_path):
        result = runner.invoke(
            admin_app,
            ["class-keys", "generate", "local-docker-v0", "--out-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        pem = tmp_path / "local-docker-v0.pem"
        assert pem.exists()

    def test_prints_signer_key_id(self, tmp_path):
        result = runner.invoke(
            admin_app,
            ["class-keys", "generate", "local-docker-v0", "--out-dir", str(tmp_path)],
        )
        assert "dac-class-local-docker-v0-" in result.output

    def test_prints_env_var_hint(self, tmp_path):
        result = runner.invoke(
            admin_app,
            ["class-keys", "generate", "local-docker-v0", "--out-dir", str(tmp_path)],
        )
        assert "DARWIN_CLASS_KEY_LOCAL_DOCKER_V0" in result.output

    def test_refuses_non_allowlisted(self, tmp_path):
        result = runner.invoke(
            admin_app,
            ["class-keys", "generate", "e2b-v0", "--out-dir", str(tmp_path)],
        )
        assert result.exit_code == 2
        assert "non-allowlisted" in result.output

    def test_refuses_to_overwrite(self, tmp_path):
        runner.invoke(
            admin_app,
            ["class-keys", "generate", "local-docker-v0", "--out-dir", str(tmp_path)],
        )
        # Second time should fail.
        result = runner.invoke(
            admin_app,
            ["class-keys", "generate", "local-docker-v0", "--out-dir", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "already exists" in result.output


# ============================================================================
# rotate
# ============================================================================


class TestRotate:
    def test_rotates_existing_key(self, tmp_path):
        # Generate first
        runner.invoke(
            admin_app,
            ["class-keys", "generate", "local-docker-v0", "--out-dir", str(tmp_path)],
        )
        # Rotate
        import time

        time.sleep(1.01)  # rotation filename uses timestamp seconds
        result = runner.invoke(
            admin_app,
            ["class-keys", "rotate", "local-docker-v0", "--keys-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "rotated" in result.output
        # Archive should exist with one file
        archive = tmp_path / "local-docker-v0.pem.rotated"
        assert archive.exists()
        archived = list(archive.glob("*.pem"))
        assert len(archived) == 1

    def test_rotate_without_existing_key_fails(self, tmp_path):
        result = runner.invoke(
            admin_app,
            ["class-keys", "rotate", "local-docker-v0", "--keys-dir", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "No active class key" in result.output

    def test_rotate_non_allowlisted_substrate_fails(self, tmp_path):
        result = runner.invoke(
            admin_app,
            ["class-keys", "rotate", "e2b-v0", "--keys-dir", str(tmp_path)],
        )
        assert result.exit_code == 2


# ============================================================================
# verify
# ============================================================================


class TestVerify:
    def test_match_when_keylist_matches_local(self, tmp_path):
        # Set up local key
        _, expected_kid = generate_class_key(tmp_path, "local-docker-v0")
        store = ClassKeyStore(keys_dir=tmp_path)
        keylist = store.keylist()

        # Mock urlopen to return that keylist
        class _MockResp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

        keylist_bytes = json.dumps(keylist).encode("utf-8")
        with patch(
            "darwin.agenticcloud.admin_cli.urllib.request.urlopen",
            return_value=_MockResp(keylist_bytes),
        ):
            result = runner.invoke(
                admin_app,
                [
                    "class-keys",
                    "verify",
                    "local-docker-v0",
                    "https://fake.example.com/.well-known/substrate-keys.json",
                    "--keys-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "match" in result.output
        assert expected_kid in result.output

    def test_mismatch_when_keylist_has_different_key(self, tmp_path):
        # Local key
        generate_class_key(tmp_path, "local-docker-v0")
        # Keylist returned by "server" has a DIFFERENT key.
        fake_keylist = {
            "schema": "darwin.cloud/agenticcloud/substrate-keys/v1",
            "issued_at": "2026-05-25T18:00:00Z",
            "keys": [
                {
                    "substrate_id": "local-docker-v0",
                    "signer_key_id": "dac-class-local-docker-v0-different01",
                    "public_key_b64": "AAAA",
                    "status": "active",
                    "created_at": "2026-05-25T18:00:00Z",
                }
            ],
        }

        class _MockResp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

        with patch(
            "darwin.agenticcloud.admin_cli.urllib.request.urlopen",
            return_value=_MockResp(json.dumps(fake_keylist).encode("utf-8")),
        ):
            result = runner.invoke(
                admin_app,
                [
                    "class-keys",
                    "verify",
                    "local-docker-v0",
                    "https://fake.example.com/.well-known/substrate-keys.json",
                    "--keys-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 2
        assert "mismatch" in result.output

    def test_no_active_key_in_keylist_fails(self, tmp_path):
        # Local key exists
        generate_class_key(tmp_path, "local-docker-v0")
        # Keylist is empty
        fake_keylist = {
            "schema": "darwin.cloud/agenticcloud/substrate-keys/v1",
            "issued_at": "2026-05-25T18:00:00Z",
            "keys": [],
        }

        class _MockResp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

        with patch(
            "darwin.agenticcloud.admin_cli.urllib.request.urlopen",
            return_value=_MockResp(json.dumps(fake_keylist).encode("utf-8")),
        ):
            result = runner.invoke(
                admin_app,
                [
                    "class-keys",
                    "verify",
                    "local-docker-v0",
                    "https://fake.example.com/.well-known/substrate-keys.json",
                    "--keys-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 1
        assert "no active key" in result.output

    def test_unreachable_keylist_fails(self, tmp_path):
        generate_class_key(tmp_path, "local-docker-v0")
        with patch(
            "darwin.agenticcloud.admin_cli.urllib.request.urlopen",
            side_effect=ConnectionError("nope"),
        ):
            result = runner.invoke(
                admin_app,
                [
                    "class-keys",
                    "verify",
                    "local-docker-v0",
                    "https://fake.example.com/.well-known/substrate-keys.json",
                    "--keys-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 1
        assert "could not fetch" in result.output


# ============================================================================
# Entrypoint integration test
# ============================================================================


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only entrypoint")
class TestEntrypointScript:
    @pytest.fixture
    def script_path(self) -> Path:
        # The script lives at the repo root.
        here = Path(__file__).resolve().parent.parent
        return here / "docker-entrypoint.sh"

    def test_script_exists_and_is_executable(self, script_path):
        assert script_path.exists()
        assert os.access(script_path, os.X_OK)

    def test_materializes_key_when_env_set(self, script_path, tmp_path):
        pem = _make_pem_bytes()
        env = os.environ.copy()
        env["DARWIN_CLASS_KEYS_DIR"] = str(tmp_path)
        env["DARWIN_CLASS_KEY_LOCAL_DOCKER_V0"] = pem.decode("utf-8")

        result = subprocess.run(
            [str(script_path), "echo", "exec-reached"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr
        assert "materialized" in result.stdout
        assert "exec-reached" in result.stdout
        # PEM should be on disk with mode 0600.
        pem_path = tmp_path / "local-docker-v0.pem"
        assert pem_path.exists()
        mode = pem_path.stat().st_mode & 0o777
        assert mode == 0o600
        assert pem_path.read_bytes() == pem

    def test_skips_when_env_unset(self, script_path, tmp_path):
        env = os.environ.copy()
        env["DARWIN_CLASS_KEYS_DIR"] = str(tmp_path)
        env.pop("DARWIN_CLASS_KEY_LOCAL_DOCKER_V0", None)

        result = subprocess.run(
            [str(script_path), "echo", "exec-reached"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr
        assert "unset" in result.stdout
        assert "exec-reached" in result.stdout
        # No PEM should be on disk.
        pem_path = tmp_path / "local-docker-v0.pem"
        assert not pem_path.exists()

    def test_idempotent_when_key_already_on_disk(self, script_path, tmp_path):
        # Pre-write a key
        original = _make_pem_bytes()
        (tmp_path / "local-docker-v0.pem").write_bytes(original)

        # Run the entrypoint with a DIFFERENT key in the env.
        different = _make_pem_bytes()
        env = os.environ.copy()
        env["DARWIN_CLASS_KEYS_DIR"] = str(tmp_path)
        env["DARWIN_CLASS_KEY_LOCAL_DOCKER_V0"] = different.decode("utf-8")

        result = subprocess.run(
            [str(script_path), "echo", "ok"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "already-on-disk" in result.stdout
        # File should NOT have been overwritten.
        assert (tmp_path / "local-docker-v0.pem").read_bytes() == original

    def test_creates_keys_dir_if_missing(self, script_path, tmp_path):
        keys_dir = tmp_path / "does-not-exist-yet"
        env = os.environ.copy()
        env["DARWIN_CLASS_KEYS_DIR"] = str(keys_dir)
        env["DARWIN_CLASS_KEY_LOCAL_DOCKER_V0"] = _make_pem_bytes().decode("utf-8")

        result = subprocess.run(
            [str(script_path), "echo", "ok"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert keys_dir.exists()
        assert (keys_dir / "local-docker-v0.pem").exists()
