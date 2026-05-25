"""
darwin.agenticcloud.substrate.base
==================================

Substrate abstraction for Darwin Agentic Cloud (Phase 2, v3.0.0).

This module defines the contract every compute backend must satisfy and the
attestation envelope it must produce. Local Docker, AWS Lambda, Modal/E2B,
Akash, and (in Phase 7) TEE-capable hardware all inherit from `Substrate`.

The design is shaped by three principles, locked in across the Phase 1 spec
and the Phase 2 architecture decisions:

1. Evidence is an open dict keyed by `schema_id`. New substrates register
   their evidence schemas at import time. Adding a TEE or GPU substrate in
   Phase 7-8 does not require modifying this file.

2. Substrate identity uses a per-substrate-class key. Hosted Darwin signs
   with the canonical class key (published in
   `darwin.cloud/.well-known/substrate-keys.json`). Self-hosted Darwin falls
   back to the operator's per-deployment key with `signer_type:
   "operator-fallback"`, so verifiers can tell the two apart.

3. Two signatures live in every attestation, with two purposes:
     - `signature` (top-level)            — operator vouches for the attestation
     - `substrate.identity_signature`     — substrate vouches it ran the workload

Schema: darwin.cloud/agenticcloud/attestation/v0.2
Spec:   docs/spec/Darwin_Agentic_Cloud_Build_Spec.md, sections 3.2 and 5

This file is RFC-0003 in proto form. Phase 3 will publish it as a public spec.
"""

from __future__ import annotations

import abc
import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# --- Imports from the rest of the Darwin codebase ---------------------------
# These imports name the Phase 1 modules. If a name has drifted, adjust the
# import only — the contract defined below does not depend on the internals.
from darwin.agenticcloud import hashing as _hashing  # sha256 hex helpers
from darwin.agenticcloud.types import WorkloadSpec  # workload spec dataclass

# ============================================================================
# Constants
# ============================================================================

#: Attestation schema URI produced by every substrate in v3.0.0.
ATTESTATION_SCHEMA_URI: str = "darwin.cloud/agenticcloud/attestation/v0.2"

#: Public location of the substrate-class keylist. Verifiers fetch this to
#: resolve substrate-class signatures without trusting any single Darwin
#: install. Format is documented in RFC-0003 (Phase 3).
SUBSTRATE_KEYLIST_URL: str = "https://darwin.cloud/.well-known/substrate-keys.json"

#: Identity declaration domain separator. Substrate identity payloads are
#: prefixed with this label before signing so that an identity signature
#: cannot be confused with any other Darwin signature artifact.
IDENTITY_DOMAIN_SEPARATOR: str = "darwin.cloud/substrate-identity/v1"


# ============================================================================
# Errors
# ============================================================================


class SubstrateError(Exception):
    """Base class for all substrate-layer errors."""


class PreflightRejected(SubstrateError):
    """Raised when the workload fails the substrate's pre-flight checks.

    Examples: cost cap exceeded, image not pullable, region unavailable,
    wallet underfunded, IAM role missing, GPU quota exhausted.
    """


class SubstrateExecutionError(SubstrateError):
    """Raised when the substrate accepted the workload but execution failed
    in a way the substrate can describe (non-zero exit, timeout, OOM, etc).

    Distinct from `PreflightRejected` (we never started) and bare exceptions
    (the substrate crashed in a way it can't describe). Carrying a partial
    evidence dict so callers can still produce a forensic attestation.
    """

    def __init__(self, message: str, partial_evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.partial_evidence = partial_evidence or {}


class EvidenceSchemaError(SubstrateError):
    """Raised when evidence does not conform to its declared schema."""


# ============================================================================
# Evidence schema registry
# ============================================================================

#: Validator signature: takes the evidence dict, raises on invalid input.
EvidenceValidator = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class EvidenceSchema:
    """A registered evidence schema.

    `schema_id` is the canonical URI for this evidence shape — e.g.
    `darwin.cloud/evidence/local-docker/v1`. It is embedded in every
    attestation alongside the evidence dict so that verifiers know which
    schema to validate against.

    `required_fields` is the minimum set of keys the evidence dict MUST
    contain. The validator may enforce stronger constraints.
    """

    schema_id: str
    required_fields: frozenset[str]
    validator: EvidenceValidator


class EvidenceRegistry:
    """Process-local registry mapping `schema_id` -> `EvidenceSchema`.

    Substrate adapters call `register()` at import time. The router and
    attestation builder consult `get()` to validate evidence before
    embedding it in an attestation.
    """

    def __init__(self) -> None:
        self._schemas: dict[str, EvidenceSchema] = {}

    def register(self, schema: EvidenceSchema) -> None:
        if schema.schema_id in self._schemas:
            existing = self._schemas[schema.schema_id]
            # Idempotent re-registration of the same instance is fine
            # (happens in test re-imports). A *different* schema with the
            # same id is a bug.
            if existing is not schema:
                raise EvidenceSchemaError(
                    f"Evidence schema id already registered: {schema.schema_id}"
                )
            return
        self._schemas[schema.schema_id] = schema

    def get(self, schema_id: str) -> EvidenceSchema:
        try:
            return self._schemas[schema_id]
        except KeyError:
            raise EvidenceSchemaError(
                f"Unknown evidence schema id: {schema_id!r}. Substrate "
                f"adapter must call EvidenceRegistry.register() at import."
            ) from None

    def validate(self, schema_id: str, evidence: Mapping[str, Any]) -> None:
        schema = self.get(schema_id)
        missing = schema.required_fields - evidence.keys()
        if missing:
            raise EvidenceSchemaError(
                f"Evidence for {schema_id} missing required fields: {sorted(missing)}"
            )
        schema.validator(evidence)

    def known_ids(self) -> frozenset[str]:
        return frozenset(self._schemas.keys())


#: Process-global registry. Substrate adapters register at module import.
EVIDENCE_REGISTRY = EvidenceRegistry()


def _noop_validator(_: Mapping[str, Any]) -> None:
    """Validator that does nothing beyond the required-fields check."""
    return None


# ============================================================================
# Substrate identity
# ============================================================================


@dataclass(frozen=True)
class SubstrateIdentity:
    """The substrate's self-signed identity declaration.

    Embedded in every attestation under `execution_result.substrate`.
    A verifier can independently confirm:
      - that the substrate name and version are what they claim to be
      - that the signature matches the key published at
        `darwin.cloud/.well-known/substrate-keys.json` (hosted), or the
        operator's key (self-hosted with `signer_type: "operator-fallback"`)

    The signed payload (bytes_to_sign) is the JCS-canonical encoding of:
        {
            "domain": IDENTITY_DOMAIN_SEPARATOR,
            "substrate_id": <id>,
            "substrate_version": <version>,
            "workload_spec_hash": <sha256-hex>,
            "output_hash": <sha256-hex>,
            "evidence_schema_id": <uri>,
            "issued_at": <iso8601>,
        }

    Domain separation prevents a substrate-identity signature from being
    replayable as any other Darwin signature artifact.
    """

    substrate_id: str
    substrate_version: str
    signer_type: str  # "darwin-class-key" | "operator-fallback"
    signer_key_id: str
    identity_signature: str  # base64(Ed25519 sig over JCS payload)


class SubstrateIdentitySigner(Protocol):
    """Signs a substrate's identity declaration.

    Two implementations exist:

    1. `RemoteClassKeySigner` — hosted Darwin. Sends the to-be-signed payload
       to the Darwin signer service over HTTPS. The class private key never
       leaves the hosted infrastructure. Produces `signer_type="darwin-class-key"`.

    2. `OperatorFallbackSigner` — self-hosted Darwin. Signs with the same
       per-deployment key the runtime already uses for the outer attestation
       signature. Produces `signer_type="operator-fallback"`.

    Both implementations live in `substrate/identity.py` (next file).
    """

    @property
    def signer_type(self) -> str: ...

    @property
    def signer_key_id(self) -> str: ...

    def sign(self, payload: bytes) -> bytes:
        """Return raw Ed25519 signature bytes over `payload`."""


# ============================================================================
# Workload, costs, and results
# ============================================================================


@dataclass(frozen=True)
class CostEstimate:
    """Pre-flight cost estimate returned by `Substrate.preflight()`.

    `cost_usd_max` is the upper bound the substrate is willing to commit to.
    The runtime compares this against the workload's cost cap and refuses to
    run if `cost_usd_max > workload.cost_cap_usd`.
    """

    cost_usd_max: float
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class RunResult:
    """The full result of a substrate executing a workload.

    Carries everything the attestation builder needs to produce a v0.2
    attestation. Substrate-specific evidence rides in `evidence` and is
    validated against `evidence_schema_id` before being embedded.

    `extensions` is the namespaced map that future substrate features
    (TEE quotes in Phase 7, GPU attestations in Phase 8) write into without
    breaking v0.2 consumers.

    `tee_required` is `False` by default. A substrate that provides hardware
    attestation flips this to `True` and writes the quote into
    `extensions["tee.tdx.v1"]` (or equivalent). Phase 7.
    """

    substrate_id: str
    substrate_version: str
    workload_spec_hash: str
    stdout: str
    stderr: str
    output_hash: str
    cost_usd: float
    evidence_schema_id: str
    evidence: dict[str, Any]
    extensions: dict[str, Any] = field(default_factory=dict)
    tee_required: bool = False
    issued_at: str = ""  # set by attestation builder


# ============================================================================
# Substrate ABC
# ============================================================================


class Substrate(abc.ABC):
    """Abstract compute backend.

    Every Phase 2+ adapter inherits from this. The contract is intentionally
    narrow — three methods (`preflight`, `run`, `identity_signer`) plus three
    properties (`substrate_id`, `substrate_version`, `evidence_schema_id`).

    The runtime does not call substrate methods directly. The router selects
    a substrate and the attestation builder drives the lifecycle:

        substrate = router.pick(workload)
        cost_est = substrate.preflight(workload)
        if cost_est.cost_usd_max > workload.cost_cap_usd:
            raise PreflightRejected(...)
        result = substrate.run(workload)
        identity = build_identity(result, substrate.identity_signer())
        attestation = build_attestation(result, identity, operator_signer)
    """

    # --- Required class metadata --------------------------------------------

    @property
    @abc.abstractmethod
    def substrate_id(self) -> str:
        """Canonical id, e.g. `local-docker-v0`, `aws-lambda-us-east-1`,
        `akash-sandbox-v0`. Stable across patch versions of this adapter."""

    @property
    @abc.abstractmethod
    def substrate_version(self) -> str:
        """Adapter version (semver). Bumped when behavior changes in a way
        that would change the produced evidence shape or content."""

    @property
    @abc.abstractmethod
    def evidence_schema_id(self) -> str:
        """The evidence schema URI this substrate emits. Must be registered
        in `EVIDENCE_REGISTRY` at module import time."""

    # --- Required behavior --------------------------------------------------

    @abc.abstractmethod
    def preflight(self, workload: WorkloadSpec) -> CostEstimate:
        """Validate inputs and return an upper-bound cost estimate.

        Raise `PreflightRejected` if the substrate cannot run this workload
        (image not pullable, region unavailable, IAM missing, wallet empty,
        etc). Must not actually start the workload.
        """

    @abc.abstractmethod
    def run(self, workload: WorkloadSpec) -> RunResult:
        """Execute the workload and return the run result.

        On non-zero exit or substrate-described failure, raise
        `SubstrateExecutionError` with a partial evidence dict so the runtime
        can record what happened.

        On a substrate crash that can't be described, let the bare exception
        propagate — the runtime will log it as an unrecoverable failure.
        """

    @abc.abstractmethod
    def identity_signer(self) -> SubstrateIdentitySigner:
        """Return the signer used to sign this substrate's identity
        declaration. Hosted Darwin returns `RemoteClassKeySigner`, self-
        hosted returns `OperatorFallbackSigner`. Implementations live in
        `substrate/identity.py`."""


# ============================================================================
# Identity payload + signing helpers
# ============================================================================


def build_identity_payload(
    *,
    substrate_id: str,
    substrate_version: str,
    workload_spec_hash: str,
    output_hash: str,
    evidence_schema_id: str,
    issued_at: str,
) -> dict[str, str]:
    """Construct the dict that gets JCS-encoded and signed for substrate
    identity. Kept in one place so verifiers can reconstruct it deterministically.
    """
    return {
        "domain": IDENTITY_DOMAIN_SEPARATOR,
        "substrate_id": substrate_id,
        "substrate_version": substrate_version,
        "workload_spec_hash": workload_spec_hash,
        "output_hash": output_hash,
        "evidence_schema_id": evidence_schema_id,
        "issued_at": issued_at,
    }


def sign_identity(
    *,
    result: RunResult,
    signer: SubstrateIdentitySigner,
) -> SubstrateIdentity:
    """Build and sign a `SubstrateIdentity` for a completed run result.

    `result.issued_at` must already be set by the caller (the attestation
    builder sets it just before signing so the same timestamp appears in
    both signatures).
    """
    if not result.issued_at:
        raise SubstrateError("RunResult.issued_at must be set before signing identity")

    payload = build_identity_payload(
        substrate_id=result.substrate_id,
        substrate_version=result.substrate_version,
        workload_spec_hash=result.workload_spec_hash,
        output_hash=result.output_hash,
        evidence_schema_id=result.evidence_schema_id,
        issued_at=result.issued_at,
    )

    # JCS-canonical (RFC 8785) encoding ensures the bytes a verifier
    # reconstructs from the JSON attestation match what we signed.
    canonical = _hashing.canonical_json(payload)
    sig_bytes = signer.sign(canonical)
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")

    return SubstrateIdentity(
        substrate_id=result.substrate_id,
        substrate_version=result.substrate_version,
        signer_type=signer.signer_type,
        signer_key_id=signer.signer_key_id,
        identity_signature=sig_b64,
    )


# ============================================================================
# Attestation construction
# ============================================================================


def build_attestation_dict(
    *,
    attestation_id: str,
    result: RunResult,
    identity: SubstrateIdentity,
) -> dict[str, Any]:
    """Build the attestation dict in the exact shape of spec section 3.2.

    This is the **to-be-signed payload** for the outer (operator) signature.
    The runtime canonicalizes this with JCS, signs with the operator key,
    base64-encodes the signature, and appends `signer_key_id` and
    `signature` at the top level.

    Validates evidence against the registered schema. Raises
    `EvidenceSchemaError` if the substrate emitted nonconforming evidence.
    """
    EVIDENCE_REGISTRY.validate(result.evidence_schema_id, result.evidence)

    substrate_block: dict[str, Any] = {
        "id": result.substrate_id,
        "version": result.substrate_version,
        "identity_signature": identity.identity_signature,
        "identity_signer_type": identity.signer_type,
        "identity_signer_key_id": identity.signer_key_id,
        "evidence_schema_id": result.evidence_schema_id,
        "evidence": dict(result.evidence),
        "extensions": dict(result.extensions),
        "tee_required": result.tee_required,
    }

    return {
        "attestation_id": attestation_id,
        "schema": ATTESTATION_SCHEMA_URI,
        "issued_at": result.issued_at,
        "workload_spec_hash": result.workload_spec_hash,
        "execution_result": {
            "output_hash": result.output_hash,
            "substrate": substrate_block,
            "cost_usd": result.cost_usd,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    }


def iso8601_now() -> str:
    """RFC 3339 / ISO 8601 UTC timestamp with 'Z' suffix. Used by the
    runtime to stamp both signatures with the same `issued_at`."""
    # time.gmtime() + strftime gives a string we control to the second.
    # Subsecond precision is unnecessary for attestations and would only add
    # signature-input variance.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = [
    # Constants
    "ATTESTATION_SCHEMA_URI",
    "EVIDENCE_REGISTRY",
    "IDENTITY_DOMAIN_SEPARATOR",
    "SUBSTRATE_KEYLIST_URL",
    # Workload + result
    "CostEstimate",
    "EvidenceRegistry",
    # Evidence registry
    "EvidenceSchema",
    "EvidenceSchemaError",
    "EvidenceValidator",
    "PreflightRejected",
    "RunResult",
    # ABC
    "Substrate",
    # Errors
    "SubstrateError",
    "SubstrateExecutionError",
    # Identity
    "SubstrateIdentity",
    "SubstrateIdentitySigner",
    # Attestation
    "build_attestation_dict",
    "build_identity_payload",
    "iso8601_now",
    "sign_identity",
]
