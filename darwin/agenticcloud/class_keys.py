"""
darwin.agenticcloud.class_keys
==============================

Server-side class signing key management.

The class signing keys are the keys that sign `substrate.identity_signature`
in v0.2 attestations. They live on the Darwin hosted signer (Fly) and
never leave it. Clients call `/v0/sign-substrate-identity` with a payload
and receive a signature back.

Each substrate has exactly one active class key. Old keys remain in the
keylist for verification of historical attestations after rotation.

Key files on disk:

    {keys_dir}/
        {substrate_id}.pem            # active private key
        {substrate_id}.pem.rotated/   # historical keys, kept for verification
            2026-05-25T18-00-00Z.pem
            ...

Public keylist (served at `.well-known/substrate-keys.json`):

    {
        "schema": "darwin.cloud/agenticcloud/substrate-keys/v1",
        "issued_at": "2026-05-25T18:00:00Z",
        "keys": [
            {
                "substrate_id": "local-docker-v0",
                "signer_key_id": "dac-class-local-docker-v0-abc123...",
                "public_key_b64": "...",
                "status": "active",
                "created_at": "2026-05-25T18:00:00Z"
            },
            {
                "substrate_id": "local-docker-v0",
                "signer_key_id": "dac-class-local-docker-v0-def456...",
                "public_key_b64": "...",
                "status": "rotated",
                "created_at": "2026-04-01T12:00:00Z",
                "rotated_at": "2026-05-25T18:00:00Z"
            }
        ]
    }

Verifier flow:
    1. Read attestation, find `substrate.identity_signer_key_id`
    2. Fetch keylist from `darwin.cloud/.well-known/substrate-keys.json`
    3. Look up matching key, verify signature
    4. If key status is `rotated`, signature is still valid for attestations
       issued before `rotated_at`
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from darwin.agenticcloud.substrate.identity import class_key_id

# ============================================================================
# Constants
# ============================================================================

#: Default directory for class signing keys on the server. Override with
#: the `DARWIN_CLASS_KEYS_DIR` env var. Fly deployment uses `/data/class-keys`
#: which is mounted on a persistent volume.
DEFAULT_KEYS_DIR_PATH = Path("/data/class-keys")

#: Schema URI for the public keylist served at `.well-known/`.
KEYLIST_SCHEMA_URI = "darwin.cloud/agenticcloud/substrate-keys/v1"

#: Phase 2 v3.0.0 substrate allowlist. The server refuses to sign for any
#: substrate not in this list. Update this when shipping a new substrate.
ALLOWED_SUBSTRATES: frozenset[str] = frozenset(
    {
        "local-docker-v0",
    }
)


# ============================================================================
# Errors
# ============================================================================


class ClassKeyError(Exception):
    """Base class for class-key errors."""


class ClassKeyNotFound(ClassKeyError):
    """No active class key exists for the requested substrate."""


class SubstrateNotAllowed(ClassKeyError):
    """The requested substrate is not in the server's allowlist."""


# ============================================================================
# Data classes
# ============================================================================


@dataclass(frozen=True)
class ClassKeyEntry:
    """One entry in the public keylist."""

    substrate_id: str
    signer_key_id: str
    public_key_b64: str
    status: str  # "active" | "rotated"
    created_at: str  # ISO 8601
    rotated_at: str | None = None  # ISO 8601, only for rotated keys

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "substrate_id": self.substrate_id,
            "signer_key_id": self.signer_key_id,
            "public_key_b64": self.public_key_b64,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.rotated_at is not None:
            d["rotated_at"] = self.rotated_at
        return d


# ============================================================================
# ClassKeyStore
# ============================================================================


def _default_keys_dir() -> Path:
    """Resolve the keys directory. Honors `DARWIN_CLASS_KEYS_DIR`."""
    val = os.environ.get("DARWIN_CLASS_KEYS_DIR")
    if val:
        return Path(val)
    return DEFAULT_KEYS_DIR_PATH


class ClassKeyStore:
    """Loads class signing keys from disk and produces the public keylist.

    Keys are loaded lazily on first access and cached for the lifetime of
    the store. To pick up new keys, restart the process. (Class keys are
    high-trust, low-frequency; a hot reload mechanism is not worth the
    bug surface in v3.0.0.)
    """

    def __init__(self, keys_dir: Path | None = None) -> None:
        self._keys_dir = keys_dir or _default_keys_dir()
        # substrate_id -> (private_key, signer_key_id, public_key_b64)
        self._active: dict[str, tuple[Ed25519PrivateKey, str, str]] | None = None
        # All keys ever seen (active + rotated) for keylist publication.
        self._all_entries: list[ClassKeyEntry] | None = None

    @property
    def keys_dir(self) -> Path:
        return self._keys_dir

    def has_active_key(self, substrate_id: str) -> bool:
        return substrate_id in self._load_active()

    def sign(self, substrate_id: str, payload: bytes) -> tuple[bytes, str]:
        """Sign `payload` with the active key for `substrate_id`.

        Returns (signature_bytes, signer_key_id).

        Raises:
            SubstrateNotAllowed: substrate not in ALLOWED_SUBSTRATES.
            ClassKeyNotFound: no active key on disk for this substrate.
        """
        if substrate_id not in ALLOWED_SUBSTRATES:
            raise SubstrateNotAllowed(
                f"Substrate {substrate_id!r} is not in the server allowlist. "
                f"Allowed: {sorted(ALLOWED_SUBSTRATES)}"
            )
        active = self._load_active()
        if substrate_id not in active:
            raise ClassKeyNotFound(
                f"No active class key for substrate {substrate_id!r}. Looked in: {self._keys_dir}"
            )
        priv, signer_key_id, _pub_b64 = active[substrate_id]
        sig = priv.sign(payload)
        return sig, signer_key_id

    def get_active_signer_key_id(self, substrate_id: str) -> str:
        """Return the signer_key_id of the active key for `substrate_id`."""
        active = self._load_active()
        if substrate_id not in active:
            raise ClassKeyNotFound(f"No active class key for substrate {substrate_id!r}")
        return active[substrate_id][1]

    def keylist(self) -> dict[str, Any]:
        """Return the full keylist as a JSON-serializable dict.

        This is what gets served at `.well-known/substrate-keys.json`.
        """
        entries = self._load_all_entries()
        return {
            "schema": KEYLIST_SCHEMA_URI,
            "issued_at": _iso8601_now(),
            "keys": [e.to_dict() for e in entries],
        }

    def reload(self) -> None:
        """Force a reload from disk. Mostly for tests."""
        self._active = None
        self._all_entries = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_active(self) -> dict[str, tuple[Ed25519PrivateKey, str, str]]:
        if self._active is not None:
            return self._active
        result: dict[str, tuple[Ed25519PrivateKey, str, str]] = {}
        if not self._keys_dir.exists():
            # No keys directory means no active keys. That's a valid state
            # for a freshly-deployed server before bootstrap. Endpoint will
            # respond with ClassKeyNotFound for any sign request.
            self._active = result
            return result
        for path in sorted(self._keys_dir.glob("*.pem")):
            substrate_id = path.stem
            if substrate_id not in ALLOWED_SUBSTRATES:
                # Refuse to load keys for substrates not in the allowlist.
                # This is defense-in-depth: even if an operator drops a
                # rogue PEM in the keys dir, the server won't sign with it.
                continue
            priv = _load_private_key(path)
            pub_raw = _public_key_raw(priv)
            signer_key_id = class_key_id(substrate_id, pub_raw)
            from darwin.agenticcloud.signing import base64 as _b64  # noqa  # pragma: no cover
            import base64 as _b64_local

            pub_b64 = _b64_local.b64encode(pub_raw).decode("ascii")
            result[substrate_id] = (priv, signer_key_id, pub_b64)
        self._active = result
        return result

    def _load_all_entries(self) -> list[ClassKeyEntry]:
        if self._all_entries is not None:
            return self._all_entries
        entries: list[ClassKeyEntry] = []
        active = self._load_active()

        # Active entries first.
        for substrate_id, (_priv, signer_key_id, pub_b64) in active.items():
            pem_path = self._keys_dir / f"{substrate_id}.pem"
            try:
                created_at = _iso8601_from_mtime(pem_path)
            except OSError:
                created_at = _iso8601_now()
            entries.append(
                ClassKeyEntry(
                    substrate_id=substrate_id,
                    signer_key_id=signer_key_id,
                    public_key_b64=pub_b64,
                    status="active",
                    created_at=created_at,
                )
            )

        # Rotated entries from {substrate_id}.pem.rotated/*.pem
        if self._keys_dir.exists():
            for rotated_dir in sorted(self._keys_dir.glob("*.pem.rotated")):
                substrate_id = rotated_dir.name.removesuffix(".pem.rotated")
                if substrate_id not in ALLOWED_SUBSTRATES:
                    continue
                for old_pem in sorted(rotated_dir.glob("*.pem")):
                    try:
                        priv = _load_private_key(old_pem)
                    except Exception:
                        # Malformed historical key; skip rather than fail
                        # keylist publication.
                        continue
                    pub_raw = _public_key_raw(priv)
                    import base64 as _b64_local

                    pub_b64 = _b64_local.b64encode(pub_raw).decode("ascii")
                    signer_key_id = class_key_id(substrate_id, pub_raw)
                    # File name = ISO 8601 rotation timestamp, stem only.
                    rotated_at = old_pem.stem.replace("-", ":").replace("T", "T", 1)
                    # Reconstruct ISO 8601: filenames use "T" and "-" only.
                    rotated_at = _filename_to_iso8601(old_pem.stem)
                    try:
                        created_at = _iso8601_from_mtime(old_pem)
                    except OSError:
                        created_at = rotated_at
                    entries.append(
                        ClassKeyEntry(
                            substrate_id=substrate_id,
                            signer_key_id=signer_key_id,
                            public_key_b64=pub_b64,
                            status="rotated",
                            created_at=created_at,
                            rotated_at=rotated_at,
                        )
                    )

        self._all_entries = entries
        return entries


# ============================================================================
# Helpers
# ============================================================================


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    with path.open("rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ClassKeyError(f"Expected Ed25519 key at {path}, got {type(key).__name__}")
    return key


def _public_key_raw(priv: Ed25519PrivateKey) -> bytes:
    pub: Ed25519PublicKey = priv.public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _iso8601_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso8601_from_mtime(path: Path) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))


def _filename_to_iso8601(stem: str) -> str:
    """Rotated key filenames use `:` -> `-` for filesystem safety.
    Reverse that here. Example: `2026-05-25T18-00-00Z` -> `2026-05-25T18:00:00Z`."""
    if "T" not in stem:
        return stem
    date_part, time_part = stem.split("T", 1)
    if time_part.endswith("Z"):
        time_part = time_part[:-1].replace("-", ":") + "Z"
    else:
        time_part = time_part.replace("-", ":")
    return f"{date_part}T{time_part}"


# ============================================================================
# Key generation (used by the bootstrap CLI, not by the server)
# ============================================================================


def generate_class_key(keys_dir: Path, substrate_id: str) -> tuple[Path, str]:
    """Generate a new Ed25519 class key for `substrate_id` and write to disk.

    Refuses to overwrite an existing active key — operators must
    explicitly rotate (call `rotate_class_key()`) instead.

    Returns (pem_path, signer_key_id).
    """
    if substrate_id not in ALLOWED_SUBSTRATES:
        raise SubstrateNotAllowed(
            f"Cannot generate key for non-allowlisted substrate {substrate_id!r}"
        )
    keys_dir.mkdir(parents=True, exist_ok=True)
    pem_path = keys_dir / f"{substrate_id}.pem"
    if pem_path.exists():
        raise ClassKeyError(f"Active key already exists at {pem_path}. Rotate instead.")
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path.write_bytes(pem)
    pem_path.chmod(0o600)
    pub_raw = _public_key_raw(priv)
    return pem_path, class_key_id(substrate_id, pub_raw)


def rotate_class_key(keys_dir: Path, substrate_id: str) -> tuple[Path, str]:
    """Move the current active key into the rotated archive and generate
    a new active key.

    The rotated key is moved to:
        {keys_dir}/{substrate_id}.pem.rotated/{ISO8601}.pem

    Returns (new_pem_path, new_signer_key_id).
    """
    if substrate_id not in ALLOWED_SUBSTRATES:
        raise SubstrateNotAllowed(
            f"Cannot rotate key for non-allowlisted substrate {substrate_id!r}"
        )
    pem_path = keys_dir / f"{substrate_id}.pem"
    if not pem_path.exists():
        raise ClassKeyNotFound(f"No active key to rotate at {pem_path}")
    rotated_dir = keys_dir / f"{substrate_id}.pem.rotated"
    rotated_dir.mkdir(parents=True, exist_ok=True)
    # Filename uses `-` instead of `:` for filesystem portability.
    timestamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    archive_path = rotated_dir / f"{timestamp}.pem"
    pem_path.rename(archive_path)
    return generate_class_key(keys_dir, substrate_id)


__all__ = [
    "ALLOWED_SUBSTRATES",
    "KEYLIST_SCHEMA_URI",
    "ClassKeyEntry",
    "ClassKeyError",
    "ClassKeyNotFound",
    "ClassKeyStore",
    "SubstrateNotAllowed",
    "generate_class_key",
    "rotate_class_key",
]
