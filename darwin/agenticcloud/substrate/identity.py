"""
darwin.agenticcloud.substrate.identity
======================================

Substrate identity signers for Darwin Agentic Cloud.

Two implementations, one Protocol:

- `RemoteClassKeySigner` — hosted Darwin. HTTPS call to
  `/v0/sign-substrate-identity` on the Darwin signer service. The
  class-key private material never leaves the hosted infrastructure. The
  public key is published at `.well-known/substrate-keys.json` so any
  verifier on earth can confirm class signatures without trusting any
  single Darwin install. Produces `signer_type="darwin-class-key"`.

- `OperatorFallbackSigner` — self-hosted Darwin. Signs with the same
  per-deployment Ed25519 key the Phase 1 outer attestation uses (loaded
  via `darwin.agenticcloud.signing.Signer`). Produces
  `signer_type="operator-fallback"` so verifiers can tell self-signed
  attestations apart from class-signed ones.

The `resolve_identity_signer()` factory picks between them based on the
`DARWIN_SIGNER_URL` env var. Default is remote (prod). To force operator
fallback (CI, offline, hosted-tier opt-out), set `DARWIN_SIGNER_URL=""`.

Per our `no skim` policy (Apple-grade): when a caller requested the
remote signer and the remote signer is unreachable, we DO NOT silently
fall back to operator signing. Silent fallback would produce attestations
that look identical to API consumers but have very different verifiability
properties. Instead we raise a typed error so the caller can decide.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from darwin.agenticcloud.signing import Signer
from darwin.agenticcloud.substrate.base import (
    IDENTITY_DOMAIN_SEPARATOR,
    SubstrateError,
    SubstrateIdentitySigner,
)

# ============================================================================
# Constants
# ============================================================================

#: Default URL for the hosted Darwin signer service. Override with the
#: `DARWIN_SIGNER_URL` env var. Set to "" to force OperatorFallbackSigner.
DEFAULT_SIGNER_URL: str = "https://darwin-agentic-cloud.fly.dev"

#: Sign-substrate-identity endpoint path.
SIGN_ENDPOINT_PATH: str = "/v0/sign-substrate-identity"

#: Public keylist endpoint path. Verifiers fetch this to resolve
#: substrate-class signatures.
KEYLIST_PATH: str = "/.well-known/substrate-keys.json"

#: HTTP timeout in seconds. The hosted signer should respond in
#: milliseconds; a multi-second timeout means something is wrong.
HTTP_TIMEOUT_SEC: float = 5.0

#: Maximum signed payload size accepted by the remote signer. Identity
#: payloads are <500 bytes in practice; 4 KiB is a generous cap that
#: still prevents DoS via massive payloads.
MAX_PAYLOAD_BYTES: int = 4096

#: Ed25519 signature is always exactly 64 bytes. We refuse anything else.
ED25519_SIGNATURE_BYTES: int = 64


# ============================================================================
# Errors
# ============================================================================


class SubstrateSignerError(SubstrateError):
    """Base class for identity-signer errors."""


class SubstrateSignerUnreachable(SubstrateSignerError):
    """The hosted signer was unreachable (network, DNS, 5xx, timeout).

    Raised by `RemoteClassKeySigner`. Per our `no skim` policy we do not
    silently fall back to operator signing on this error. The caller can
    catch this and decide to retry, fall back manually, or surface to the
    user.
    """


class SubstrateSignerProtocolError(SubstrateSignerError):
    """The hosted signer responded with something that didn't match the
    expected protocol (wrong shape, wrong signature length, etc).

    Distinct from `SubstrateSignerUnreachable` — the server is up, but it
    spoke a language we don't understand. Either we're talking to the
    wrong server, or there's a version mismatch.
    """


class SubstrateSignerRejected(SubstrateSignerError):
    """The hosted signer refused to sign this payload.

    Examples: substrate_id not in the server's allowlist, payload missing
    domain separator, payload too large. The server's reason is
    propagated in the exception message.
    """


# ============================================================================
# OperatorFallbackSigner
# ============================================================================


class OperatorFallbackSigner:
    """Signs substrate identities with the operator's per-deployment key.

    Wraps the Phase 1 `darwin.agenticcloud.signing.Signer` so we reuse the
    existing key material at `~/.darwin/agenticcloud/keys/signing.pem`
    (or wherever `DARWIN_STATE_DIR` points).

    Conforms to the `SubstrateIdentitySigner` Protocol.

    `signer_key_id` follows the Phase 1 format (`dac-local-{hex16}`),
    which is fine for self-hosted deployments. Verifiers seeing
    `signer_type == "operator-fallback"` know they need the operator's
    public key, not the class keylist, to verify the signature.
    """

    def __init__(self, signer: Signer | None = None) -> None:
        self._signer = signer or Signer()

    @property
    def signer_type(self) -> str:
        return "operator-fallback"

    @property
    def signer_key_id(self) -> str:
        return self._signer.key_id()

    @property
    def public_key_b64(self) -> str:
        """Operator's Ed25519 public key, base64-encoded.

        Verifiers need this to check operator-fallback signatures. The
        attestation builder embeds it (or a reference to it) so verifiers
        can look up the key.
        """
        return self._signer.public_key_b64()

    def sign(self, payload: bytes) -> bytes:
        """Sign `payload` and return raw Ed25519 signature bytes.

        The Phase 1 `Signer.sign()` returns base64-encoded `str`. We
        decode once here so the Protocol stays consistent across both
        signer implementations. No double-encoding.
        """
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise SubstrateSignerRejected(
                f"Payload exceeds max size: {len(payload)} > {MAX_PAYLOAD_BYTES}"
            )
        sig_b64 = self._signer.sign(payload)
        sig_bytes = base64.b64decode(sig_b64)
        if len(sig_bytes) != ED25519_SIGNATURE_BYTES:
            # Defensive: should never happen, but if it does we want a
            # clear error not a downstream cryptographic mystery.
            raise SubstrateSignerProtocolError(
                f"Operator Signer produced wrong signature length: "
                f"{len(sig_bytes)} bytes (expected {ED25519_SIGNATURE_BYTES})"
            )
        return sig_bytes


# ============================================================================
# RemoteClassKeySigner
# ============================================================================


@dataclass(frozen=True)
class _RemoteSignResponse:
    """Parsed response from the remote signer."""

    signature: bytes
    signer_key_id: str
    substrate_id: str


class RemoteClassKeySigner:
    """Signs substrate identities by calling the hosted Darwin signer.

    Conforms to the `SubstrateIdentitySigner` Protocol.

    The class-key private material lives only on Fly. This signer sends
    the to-be-signed payload over HTTPS, the server verifies the payload
    matches the expected format (domain separator, substrate_id
    allowlist, JCS canonicality), signs with the class key for that
    substrate, and returns the raw 64-byte signature.

    Errors do NOT silently fall back. Callers receive a typed exception
    so they can decide what to do.

    Network calls use stdlib `urllib.request`. We deliberately avoid
    adding `httpx` or `requests` as a runtime dep so `pip install
    darwin-agentic-cloud` stays light.
    """

    def __init__(
        self,
        substrate_id: str,
        *,
        signer_url: str | None = None,
        timeout_sec: float = HTTP_TIMEOUT_SEC,
    ) -> None:
        self._substrate_id = substrate_id
        url = signer_url if signer_url is not None else _signer_url_from_env()
        if not url:
            raise SubstrateSignerError(
                "RemoteClassKeySigner requires a signer URL. Set "
                "DARWIN_SIGNER_URL or pass signer_url explicitly."
            )
        # Normalize: strip trailing slash, validate scheme.
        url = url.rstrip("/")
        if not url.startswith(("https://", "http://")):
            raise SubstrateSignerError(f"DARWIN_SIGNER_URL must include scheme. Got: {url!r}")
        self._url = url
        self._timeout = timeout_sec
        # Lazily resolved after first signing call.
        self._resolved_signer_key_id: str | None = None

    @property
    def signer_type(self) -> str:
        return "darwin-class-key"

    @property
    def signer_key_id(self) -> str:
        """Stable identifier for the class key signing this substrate.

        Format: `dac-class-{substrate_id}-{hex16}`. The hex16 portion is
        returned by the server on each sign call. Before the first call
        we use the format prefix only with a placeholder, which is fine
        because `sign_identity()` always calls `sign()` before reading
        `signer_key_id`.
        """
        if self._resolved_signer_key_id is None:
            return f"dac-class-{self._substrate_id}-unresolved"
        return self._resolved_signer_key_id

    def sign(self, payload: bytes) -> bytes:
        """Send `payload` to the hosted signer, return raw signature.

        Raises:
            SubstrateSignerRejected: server refused to sign (bad
              substrate_id, missing domain separator, etc).
            SubstrateSignerUnreachable: network or 5xx.
            SubstrateSignerProtocolError: response shape was wrong.
        """
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise SubstrateSignerRejected(
                f"Payload exceeds max size: {len(payload)} > {MAX_PAYLOAD_BYTES}"
            )
        # Quick sanity check: payload must be JCS-canonical JSON containing
        # our domain separator. The server re-validates this, but checking
        # client-side gives a faster failure for malformed inputs.
        self._client_side_validate(payload)

        body = json.dumps(
            {
                "substrate_id": self._substrate_id,
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            }
        ).encode("utf-8")

        url = self._url + SIGN_ENDPOINT_PATH
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "darwin-agentic-cloud/identity-signer",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise SubstrateSignerUnreachable(
                        f"Signer returned HTTP {resp.status} from {url}"
                    )
                raw_response = resp.read(8192)  # bounded read
        except urllib.error.HTTPError as e:
            # Server reachable but returned 4xx/5xx.
            try:
                detail = e.read(2048).decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - defensive
                detail = "<no body>"
            if 400 <= e.code < 500:
                raise SubstrateSignerRejected(
                    f"Signer rejected request ({e.code}): {detail}"
                ) from e
            raise SubstrateSignerUnreachable(f"Signer returned HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise SubstrateSignerUnreachable(f"Could not reach signer at {url}: {e.reason}") from e
        except TimeoutError as e:
            raise SubstrateSignerUnreachable(
                f"Signer at {url} timed out after {self._timeout}s"
            ) from e

        parsed = self._parse_response(raw_response)
        self._resolved_signer_key_id = parsed.signer_key_id
        if parsed.substrate_id != self._substrate_id:
            raise SubstrateSignerProtocolError(
                f"Signer signed for wrong substrate: requested "
                f"{self._substrate_id!r}, got {parsed.substrate_id!r}"
            )
        return parsed.signature

    def _client_side_validate(self, payload: bytes) -> None:
        """Fast pre-flight check before we waste an HTTP roundtrip."""
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SubstrateSignerRejected(f"Payload is not valid UTF-8 JSON: {e}") from e
        if not isinstance(decoded, dict):
            raise SubstrateSignerRejected("Payload must be a JSON object")
        if decoded.get("domain") != IDENTITY_DOMAIN_SEPARATOR:
            raise SubstrateSignerRejected(
                f"Payload missing or wrong domain separator. Expected "
                f"{IDENTITY_DOMAIN_SEPARATOR!r}, got {decoded.get('domain')!r}"
            )
        if decoded.get("substrate_id") != self._substrate_id:
            raise SubstrateSignerRejected(
                f"Payload substrate_id mismatch: signer is for "
                f"{self._substrate_id!r}, payload claims "
                f"{decoded.get('substrate_id')!r}"
            )

    def _parse_response(self, raw: bytes) -> _RemoteSignResponse:
        """Parse and validate the server's response shape."""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SubstrateSignerProtocolError(
                f"Signer response is not valid UTF-8 JSON: {e}"
            ) from e
        if not isinstance(data, dict):
            raise SubstrateSignerProtocolError("Signer response must be a JSON object")
        required = {"signature_b64", "signer_key_id", "substrate_id"}
        missing = required - data.keys()
        if missing:
            raise SubstrateSignerProtocolError(
                f"Signer response missing required fields: {sorted(missing)}"
            )
        try:
            sig_bytes = base64.b64decode(data["signature_b64"], validate=True)
        except (ValueError, TypeError) as e:
            raise SubstrateSignerProtocolError(
                f"Signer response signature_b64 is not valid base64: {e}"
            ) from e
        if len(sig_bytes) != ED25519_SIGNATURE_BYTES:
            raise SubstrateSignerProtocolError(
                f"Signer response signature wrong length: "
                f"{len(sig_bytes)} bytes (expected {ED25519_SIGNATURE_BYTES})"
            )
        return _RemoteSignResponse(
            signature=sig_bytes,
            signer_key_id=str(data["signer_key_id"]),
            substrate_id=str(data["substrate_id"]),
        )


# ============================================================================
# Factory
# ============================================================================


def _signer_url_from_env() -> str:
    """Resolve the signer URL from the env. Empty string means
    `force operator fallback`. Unset means use the default prod URL."""
    val = os.environ.get("DARWIN_SIGNER_URL")
    if val is None:
        return DEFAULT_SIGNER_URL
    return val


def resolve_identity_signer(substrate_id: str) -> SubstrateIdentitySigner:
    """Factory: return the right signer for the current environment.

    - If `DARWIN_SIGNER_URL` is unset OR set to a non-empty URL:
      return `RemoteClassKeySigner`.
    - If `DARWIN_SIGNER_URL=""` (explicit empty):
      return `OperatorFallbackSigner`.

    Substrate adapters call this in their `identity_signer()` method:

        def identity_signer(self):
            return resolve_identity_signer(self.substrate_id)

    so the choice is made once per substrate run.
    """
    url = _signer_url_from_env()
    if url == "":
        return OperatorFallbackSigner()
    return RemoteClassKeySigner(substrate_id=substrate_id, signer_url=url)


# ============================================================================
# Bootstrap helper (server-side use)
# ============================================================================


def class_key_id(substrate_id: str, public_key_raw: bytes) -> str:
    """Compute the canonical key id for a substrate-class signing key.

    Format: `dac-class-{substrate_id}-{hex16}` where hex16 is the first
    16 hex chars of sha256(raw_ed25519_public_key).

    Used by the server-side `/v0/sign-substrate-identity` endpoint and
    the `.well-known/substrate-keys.json` keylist builder. Lives here so
    client (verifier) and server compute the same id from the same key.
    """
    from darwin.agenticcloud.hashing import sha256_hex

    return f"dac-class-{substrate_id}-{sha256_hex(public_key_raw)[:16]}"


__all__ = [
    "DEFAULT_SIGNER_URL",
    "ED25519_SIGNATURE_BYTES",
    "HTTP_TIMEOUT_SEC",
    "KEYLIST_PATH",
    "MAX_PAYLOAD_BYTES",
    "SIGN_ENDPOINT_PATH",
    "OperatorFallbackSigner",
    "RemoteClassKeySigner",
    "SubstrateSignerError",
    "SubstrateSignerProtocolError",
    "SubstrateSignerRejected",
    "SubstrateSignerUnreachable",
    "class_key_id",
    "resolve_identity_signer",
]
