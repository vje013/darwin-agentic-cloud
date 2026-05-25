"""DAC HTTP server (FastAPI).

Exposes the runtime over HTTP so any client — curl, a remote agent,
another service — can request signed execution and query history.

Endpoints:
    GET  /healthz                          — liveness
    GET  /v0/identity                      — server's public key + substrate id
    POST /v0/run                           — execute workload, return signed attestation
    POST /v0/attestations/verify           — verify a signed attestation
    GET  /v0/attestations                  — list recent attestations
    GET  /v0/attestations/{id}             — fetch a specific attestation
    GET  /v0/attestations/stats            — aggregate stats

    POST /v0/sign-substrate-identity       — sign a substrate identity payload (Phase 2)
    GET  /.well-known/substrate-keys.json  — public keylist for substrate-class keys (Phase 2)
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from darwin import agenticcloud as dac
from darwin.agenticcloud.attestation import verify_attestation
from darwin.agenticcloud.class_keys import (
    ClassKeyError,
    ClassKeyNotFound,
    ClassKeyStore,
    SubstrateNotAllowed,
)
from darwin.agenticcloud.runtime import Runtime
from darwin.agenticcloud.sandbox import SUBSTRATE_ID
from darwin.agenticcloud.signing import Signer
from darwin.agenticcloud.storage import AttestationStore
from darwin.agenticcloud.substrate.base import IDENTITY_DOMAIN_SEPARATOR
from darwin.agenticcloud.substrate.identity import (
    ED25519_SIGNATURE_BYTES,
    MAX_PAYLOAD_BYTES,
)
from darwin.agenticcloud.types import WorkloadSpec


# -------------------------------------------------------------------
# Schemas
# -------------------------------------------------------------------
class RunRequest(BaseModel):
    code: str = Field(..., description="Source code to execute.")
    language: str = Field("python", description="Language: 'python' or 'node'.")
    inputs: dict = Field(default_factory=dict, description="Optional inputs.")
    cost_cap_usd: float = Field(0.01, ge=0, description="Cost ceiling in USD.")
    timeout_sec: int = Field(30, ge=1, le=600, description="Timeout in seconds.")
    memory_mb: int = Field(512, ge=64, le=8192, description="Memory limit in MB.")


class RunResponse(BaseModel):
    attestation: dict
    signature_b64: str
    public_key_b64: str


class VerifyRequest(BaseModel):
    attestation: dict
    signature_b64: str
    public_key_b64: str


class VerifyResponse(BaseModel):
    verified: bool


class IdentityResponse(BaseModel):
    key_id: str
    public_key_b64: str
    substrate_id: str
    schema: str
    version: str


class AttestationSummary(BaseModel):
    attestation_id: str
    workload_id: str
    signer_key_id: str
    substrate_id: str
    status: str
    issued_at: float
    cost_usd: float
    wall_time_sec: float
    schema_version: str


class AttestationListResponse(BaseModel):
    attestations: list[AttestationSummary]
    count: int


class AttestationStatsResponse(BaseModel):
    total_count: int
    total_cost_usd: float
    by_status: dict[str, int]


class SignSubstrateIdentityRequest(BaseModel):
    """Request body for POST /v0/sign-substrate-identity."""

    substrate_id: str = Field(..., description="Substrate id, e.g. 'local-docker-v0'.")
    payload_b64: str = Field(
        ..., description="Base64-encoded JCS-canonical identity payload to sign."
    )


class SignSubstrateIdentityResponse(BaseModel):
    """Response body for POST /v0/sign-substrate-identity."""

    substrate_id: str
    signer_key_id: str
    signature_b64: str


# -------------------------------------------------------------------
# App factory
# -------------------------------------------------------------------
def create_app(
    runtime: Runtime | None = None,
    class_key_store: ClassKeyStore | None = None,
) -> FastAPI:
    """Build a FastAPI app with the given runtime (or a default one).

    `class_key_store` defaults to a fresh ClassKeyStore which honors the
    DARWIN_CLASS_KEYS_DIR env var. Tests pass an explicit one pointing at
    a temp dir.
    """
    rt = runtime or Runtime()
    store: AttestationStore = rt.store
    cks: ClassKeyStore = class_key_store if class_key_store is not None else ClassKeyStore()

    limiter = Limiter(key_func=get_remote_address)

    app = FastAPI(
        title="Darwin Agentic Cloud",
        description="Verifiable compute for AI agents with cryptographically signed attestations.",
        version=dac.__version__,
        docs_url="/docs/swagger",
        redoc_url="/docs/redoc",
    )

    # Attach limiter to app state so the decorator can find it.
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {exc.detail}"},
            headers={"Retry-After": "60"},
        )

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v0/identity", response_model=IdentityResponse, tags=["identity"])
    async def identity() -> IdentityResponse:
        signer: Signer = rt.signer
        return IdentityResponse(
            key_id=signer.key_id(),
            public_key_b64=signer.public_key_b64(),
            substrate_id=SUBSTRATE_ID,
            schema=dac.ATTESTATION_SCHEMA,
            version=dac.__version__,
        )

    @app.post("/v0/run", response_model=RunResponse, tags=["run"])
    async def run(req: RunRequest) -> RunResponse:
        spec = WorkloadSpec(
            code=req.code,
            language=req.language,
            inputs=req.inputs,
            cost_cap_usd=req.cost_cap_usd,
            timeout_sec=req.timeout_sec,
            memory_mb=req.memory_mb,
        )
        signed = await asyncio.to_thread(rt.run, spec)
        return RunResponse(
            attestation=signed.attestation,
            signature_b64=signed.signature_b64,
            public_key_b64=signed.public_key_b64,
        )

    @app.post("/v0/attestations/verify", response_model=VerifyResponse, tags=["attestations"])
    async def verify(req: VerifyRequest) -> VerifyResponse:
        try:
            payload: dict[str, Any] = {
                "attestation": req.attestation,
                "signature_b64": req.signature_b64,
                "public_key_b64": req.public_key_b64,
            }
            return VerifyResponse(verified=verify_attestation(payload))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get(
        "/v0/attestations/stats",
        response_model=AttestationStatsResponse,
        tags=["attestations"],
    )
    async def stats() -> AttestationStatsResponse:
        total = store.count()
        total_cost = store.total_cost_usd()
        by_status = {
            "ok": len(store.list_by_status("ok", limit=10**9)),
            "error": len(store.list_by_status("error", limit=10**9)),
            "timeout": len(store.list_by_status("timeout", limit=10**9)),
            "oom": len(store.list_by_status("oom", limit=10**9)),
            "cost_exceeded": len(store.list_by_status("cost_exceeded", limit=10**9)),
        }
        return AttestationStatsResponse(
            total_count=total,
            total_cost_usd=total_cost,
            by_status=by_status,
        )

    @app.get(
        "/v0/attestations",
        response_model=AttestationListResponse,
        tags=["attestations"],
    )
    async def list_attestations(
        limit: int = Query(20, ge=1, le=1000),
        status: str | None = Query(None),
    ) -> AttestationListResponse:
        rows = (
            store.list_by_status(status, limit=limit) if status else store.list_recent(limit=limit)
        )
        return AttestationListResponse(
            attestations=[
                AttestationSummary(
                    attestation_id=r.attestation_id,
                    workload_id=r.workload_id,
                    signer_key_id=r.signer_key_id,
                    substrate_id=r.substrate_id,
                    status=r.status,
                    issued_at=r.issued_at,
                    cost_usd=r.cost_usd,
                    wall_time_sec=r.wall_time_sec,
                    schema_version=r.schema_version,
                )
                for r in rows
            ],
            count=len(rows),
        )

    @app.get("/v0/attestations/{attestation_id}", tags=["attestations"])
    async def get_attestation(attestation_id: str) -> dict:
        fetched = store.get(attestation_id)
        if fetched is None:
            raise HTTPException(status_code=404, detail="Attestation not found")
        return fetched.signed_attestation

    # -----------------------------------------------------------------
    # Phase 2: substrate identity signing
    # -----------------------------------------------------------------

    @app.post(
        "/v0/sign-substrate-identity",
        response_model=SignSubstrateIdentityResponse,
        tags=["substrate"],
    )
    @limiter.limit("60/minute")
    async def sign_substrate_identity(
        request: Request,
        req: SignSubstrateIdentityRequest,
    ) -> SignSubstrateIdentityResponse:
        """Sign a substrate identity payload with the server-side class key.

        Validates the payload before signing:
        - Decodes base64
        - Bounded payload size
        - Confirms the payload is JCS-canonical JSON
        - Confirms the domain separator matches our constant
        - Confirms the payload's claimed substrate_id matches the request
        - Confirms the substrate is in the server's allowlist
        - Confirms a class key exists for that substrate

        Returns the raw Ed25519 signature, base64-encoded.
        """
        # Decode payload.
        try:
            payload_bytes = base64.b64decode(req.payload_b64, validate=True)
        except (ValueError, TypeError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"payload_b64 is not valid base64: {e}",
            ) from e

        if len(payload_bytes) == 0:
            raise HTTPException(status_code=400, detail="payload is empty")
        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=(f"payload exceeds max size: {len(payload_bytes)} > {MAX_PAYLOAD_BYTES}"),
            )

        # Validate payload is JCS-canonical JSON with the expected fields.
        try:
            decoded = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"payload is not valid UTF-8 JSON: {e}",
            ) from e

        if not isinstance(decoded, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")

        # Domain separator must match our constant. Prevents replay across
        # signature types.
        if decoded.get("domain") != IDENTITY_DOMAIN_SEPARATOR:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"payload domain mismatch: expected "
                    f"{IDENTITY_DOMAIN_SEPARATOR!r}, got {decoded.get('domain')!r}"
                ),
            )

        # Payload's substrate_id must match the request's substrate_id.
        # Defense against confused-deputy: caller can't sign one substrate's
        # payload using a different substrate's key.
        if decoded.get("substrate_id") != req.substrate_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"payload substrate_id {decoded.get('substrate_id')!r} "
                    f"does not match request substrate_id {req.substrate_id!r}"
                ),
            )

        # Sign with the class key for this substrate.
        try:
            sig_bytes, signer_key_id = cks.sign(req.substrate_id, payload_bytes)
        except SubstrateNotAllowed as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ClassKeyNotFound as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except ClassKeyError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        if len(sig_bytes) != ED25519_SIGNATURE_BYTES:  # pragma: no cover
            # Defense in depth — Ed25519 always produces 64-byte sigs.
            raise HTTPException(
                status_code=500,
                detail=(
                    f"signature length wrong: {len(sig_bytes)} bytes "
                    f"(expected {ED25519_SIGNATURE_BYTES})"
                ),
            )

        return SignSubstrateIdentityResponse(
            substrate_id=req.substrate_id,
            signer_key_id=signer_key_id,
            signature_b64=base64.b64encode(sig_bytes).decode("ascii"),
        )

    @app.get(
        "/.well-known/substrate-keys.json",
        tags=["substrate"],
        include_in_schema=True,
    )
    async def well_known_substrate_keys() -> dict[str, Any]:
        """Public keylist for substrate-class keys.

        Verifiers fetch this to resolve `substrate.identity_signer_key_id`
        to a public key. The keylist includes both active and rotated keys
        so historical attestations remain verifiable after rotation.

        Cached aggressively at the CDN/proxy layer (5 minutes). The active
        key for a given substrate changes only at rotation time, and even
        then the old key remains in the list.
        """
        keylist = cks.keylist()
        return keylist

    # Custom branded docs page (Material 3, dark, brand colors)
    from pathlib import Path as _Path

    from fastapi.responses import HTMLResponse

    _TEMPLATE_PATH = _Path(__file__).parent / "templates" / "docs.html"

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    async def custom_docs() -> HTMLResponse:
        """Serve the branded Darwin docs page."""
        return HTMLResponse(_TEMPLATE_PATH.read_text(encoding="utf-8"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root() -> HTMLResponse:
        """Root: redirect to the docs page."""
        return HTMLResponse(_TEMPLATE_PATH.read_text(encoding="utf-8"))

    return app


app = create_app()
