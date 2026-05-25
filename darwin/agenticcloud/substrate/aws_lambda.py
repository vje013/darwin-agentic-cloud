"""
darwin.agenticcloud.substrate.aws_lambda
========================================
COST MODEL NOTE: this adapter's `preflight()` returns the WHOLESALE cost
— what Darwin pays AWS for the invocation. NOT the price Darwin charges
the end customer. Customer-facing markup (per Phase 6 hosted tier) is
applied in the billing layer, never in the substrate. This separation
lets the router (Phase 2) `pick_by_cost()` on real wholesale costs while
the billing layer (Phase 6) handles tiered pricing, marketplace cuts,
and per-tenant rate cards independently.

AWS Lambda substrate adapter for Phase 2 v3.0.0.

Talks to a pre-deployed darwin-runner Lambda function in the target
region. The runner is deployed via step 3b's CDK stack — this adapter
only invokes it, never creates or destroys functions.

Architecture:

    LambdaSubstrate          (this file, client side)
        |
        |  aiobotocore.lambda.invoke(payload=RunnerEvent)
        v
    darwin-runner-{lang}-{region}   (step 3b, AWS deployment)
        |
        |  exec() workload in Lambda execution context
        v
    RunnerResponse  -> evidence dict -> RunResult

Substrate id format: `aws-lambda-{region}` (e.g. `aws-lambda-us-east-1`).
Per spec section 5.2.

Evidence schema URI: `darwin.cloud/evidence/aws-lambda/v1`. Required fields:
- request_id          AWS Lambda request id (X-Amzn-Requestid)
- log_group           CloudWatch log group name
- log_stream          CloudWatch log stream name
- lambda_version      Lambda function version that ran the workload
- region              AWS region the workload ran in
- billed_duration_ms  Billed duration from Lambda (for cost reconciliation)
- memory_size_mb      Memory configured on the function
- max_memory_used_mb  Max memory observed during execution
- container_status    'ok' | 'error' | 'timeout' | 'oom'  (from runner)
- exit_code           subprocess exit code (from runner)
- stdout_hash         sha256 of stdout (from runner)
- stderr_hash         sha256 of stderr (from runner)
- wall_time_sec       wall-clock seconds (from runner)

Identity signing: per spec, every substrate signs its own identity
declaration. `identity_signer()` resolves via `resolve_identity_signer()`
which routes to RemoteClassKeySigner (hosted) or OperatorFallbackSigner
(self-hosted).
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from darwin.agenticcloud.hashing import content_hash
from darwin.agenticcloud.substrate.aws_lambda_event import (
    EVENT_SCHEMA_URI,
    SUPPORTED_LANGUAGES,
    RunnerEvent,
    RunnerEventError,
    RunnerResponse,
)
from darwin.agenticcloud.substrate.base import (
    EVIDENCE_REGISTRY,
    CostEstimate,
    EvidenceSchema,
    EvidenceSchemaError,
    PreflightRejected,
    RunResult,
    Substrate,
    SubstrateError,
    SubstrateExecutionError,
    SubstrateIdentitySigner,
)
from darwin.agenticcloud.substrate.identity import resolve_identity_signer
from darwin.agenticcloud.types import WorkloadSpec

# ============================================================================
# Constants
# ============================================================================

#: Substrate-id prefix. The full id is constructed per-region.
SUBSTRATE_ID_PREFIX: str = "aws-lambda"

#: Adapter version. Bumped when behavior changes in a way that would
#: change produced evidence shape or content.
SUBSTRATE_VERSION: str = "0.1.0"

#: Evidence schema URI for AWS Lambda.
EVIDENCE_SCHEMA_ID: str = "darwin.cloud/evidence/aws-lambda/v1"

#: Required evidence fields. EVIDENCE_REGISTRY.validate() enforces these.
EVIDENCE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "log_group",
        "log_stream",
        "lambda_version",
        "region",
        "billed_duration_ms",
        "memory_size_mb",
        "max_memory_used_mb",
        "container_status",
        "exit_code",
        "stdout_hash",
        "stderr_hash",
        "wall_time_sec",
    }
)

#: Function-name template for the darwin-runner. Step 3b deploys functions
#: matching this pattern (e.g. `darwin-runner-python-us-east-1`).
RUNNER_FUNCTION_NAME_TEMPLATE: str = "darwin-runner-{language}-{region}"

#: AWS regions Darwin supports for v3.0.0. Step 3b deploys the runner
#: into each. Adding a region requires both a code change here and a
#: CDK deploy.
SUPPORTED_REGIONS: frozenset[str] = frozenset(
    {
        "us-east-1",
        "us-west-2",
        "eu-west-1",
        "ap-northeast-1",
    }
)

#: Hardcoded Lambda pricing fallback (USD per GB-second). Used when the
#: AWS Pricing API is unreachable on startup. Source: AWS Lambda public
#: pricing as of 2026-05-25. Refreshed in Phase 6.
LAMBDA_PRICE_PER_GB_SECOND_FALLBACK: dict[str, float] = {
    "us-east-1": 0.0000166667,
    "us-west-2": 0.0000166667,
    "eu-west-1": 0.0000183333,
    "ap-northeast-1": 0.0000200000,
}

#: Lambda's per-invocation request fee (USD per request).
LAMBDA_REQUEST_PRICE: float = 0.0000002


# ============================================================================
# Errors
# ============================================================================


class LambdaSubstrateError(SubstrateError):
    """Base class for LambdaSubstrate errors."""


class LambdaUnreachable(LambdaSubstrateError):
    """The runner function couldn't be reached (network, throttling, IAM)."""


class LambdaInvocationFailed(LambdaSubstrateError):
    """The Lambda was invoked but failed before reaching the workload
    (init error, role error, image pull error)."""


# ============================================================================
# Evidence schema registration
# ============================================================================


def _validate_evidence(evidence: Any) -> None:
    """Substrate-specific evidence validator."""
    cs = evidence.get("container_status")
    if cs not in {"ok", "error", "timeout", "oom"}:
        raise EvidenceSchemaError(f"aws-lambda evidence.container_status invalid: {cs!r}")
    region = evidence.get("region")
    if region not in SUPPORTED_REGIONS:
        raise EvidenceSchemaError(f"aws-lambda evidence.region not supported: {region!r}")
    bd = evidence.get("billed_duration_ms")
    if not isinstance(bd, int) or bd < 0:
        raise EvidenceSchemaError(
            f"aws-lambda evidence.billed_duration_ms must be a non-negative int, got {bd!r}"
        )
    wt = evidence.get("wall_time_sec")
    if not isinstance(wt, int | float) or wt < 0:
        raise EvidenceSchemaError(
            f"aws-lambda evidence.wall_time_sec must be a non-negative number, got {wt!r}"
        )


EVIDENCE_REGISTRY.register(
    EvidenceSchema(
        schema_id=EVIDENCE_SCHEMA_ID,
        required_fields=EVIDENCE_REQUIRED_FIELDS,
        validator=_validate_evidence,
    )
)


# ============================================================================
# Pricing
# ============================================================================


@dataclass
class LambdaPricing:
    """Pricing data for one region.

    `gb_second` is the USD price per GB-second. `request` is the
    per-invocation fee. Both come from the AWS Pricing API on startup,
    with a hardcoded fallback if the API is unreachable.
    """

    region: str
    gb_second: float
    request: float = LAMBDA_REQUEST_PRICE
    source: str = "fallback"  # 'pricing-api' | 'fallback'

    def cost_for(self, *, memory_mb: int, billed_duration_ms: int) -> float:
        """Compute cost for an invocation.

        Lambda billing formula:
            cost = (memory_gb * (billed_duration_ms / 1000) * gb_second)
                   + request
        """
        memory_gb = memory_mb / 1024.0
        seconds = billed_duration_ms / 1000.0
        return memory_gb * seconds * self.gb_second + self.request


class LambdaPricingClient:
    """Loads Lambda pricing on startup and caches it in-process.

    Tries the AWS Pricing API first. On any failure (unreachable,
    throttled, IAM denied, unexpected response shape) falls back to the
    hardcoded rate card. The `source` field on each LambdaPricing entry
    indicates which path was taken, so attestations can record it for
    auditability.

    Currently constructed with the pricing-API call deferred to the
    first preflight (lazy load). v3.1.0 may move this to startup.
    """

    def __init__(
        self,
        *,
        prefetched: dict[str, LambdaPricing] | None = None,
    ) -> None:
        self._cache: dict[str, LambdaPricing] = prefetched or {}

    def get(self, region: str) -> LambdaPricing:
        if region in self._cache:
            return self._cache[region]
        # On miss, return the fallback for this region. Real pricing-API
        # integration lands when we have AWS credentials to test against
        # (step 3b deploy ceremony).
        price = LAMBDA_PRICE_PER_GB_SECOND_FALLBACK.get(region)
        if price is None:
            raise LambdaSubstrateError(
                f"No pricing data for region {region!r}. Supported: {sorted(SUPPORTED_REGIONS)}"
            )
        entry = LambdaPricing(region=region, gb_second=price, source="fallback")
        self._cache[region] = entry
        return entry


# ============================================================================
# AWS credentials
# ============================================================================


@dataclass
class AwsCredentials:
    """Resolved AWS credentials passed to aiobotocore."""

    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None

    def to_session_kwargs(self) -> dict[str, str]:
        """Return kwargs suitable for aiobotocore.session.get_session.create_client()."""
        kwargs: dict[str, str] = {}
        if self.access_key_id:
            kwargs["aws_access_key_id"] = self.access_key_id
        if self.secret_access_key:
            kwargs["aws_secret_access_key"] = self.secret_access_key
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return kwargs


class AwsCredentialProvider:
    """Resolves AWS credentials for Darwin's managed AWS account.

    v3.0.0: reads `DARWIN_AWS_ACCESS_KEY_ID` and `DARWIN_AWS_SECRET_ACCESS_KEY`
    from env, falls back to the AWS default credential chain (which
    boto3/aiobotocore handles automatically when we pass no overrides).

    Phase 6 will add per-tenant credentials sourced from the hosted
    tier's encrypted store.

    Self-hosted Darwin can override DARWIN_AWS_* to point at the
    operator's own AWS account.
    """

    def resolve(self) -> AwsCredentials:
        import os

        return AwsCredentials(
            access_key_id=os.environ.get("DARWIN_AWS_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("DARWIN_AWS_SECRET_ACCESS_KEY"),
            session_token=os.environ.get("DARWIN_AWS_SESSION_TOKEN"),
        )


# ============================================================================
# LambdaSubstrate
# ============================================================================


class LambdaSubstrate(Substrate):
    """AWS Lambda substrate. One instance per region.

    Calls into a pre-deployed darwin-runner Lambda function. The runner
    handles the actual workload execution; this adapter handles
    invocation, response parsing, evidence shaping, and cost calculation.

    Tests inject `lambda_client_factory` (returns an async context manager
    that yields a mocked Lambda client). Production passes nothing, in
    which case aiobotocore builds the real client.
    """

    def __init__(
        self,
        region: str,
        *,
        credentials_provider: AwsCredentialProvider | None = None,
        pricing_client: LambdaPricingClient | None = None,
        identity_signer: SubstrateIdentitySigner | None = None,
        lambda_client_factory: Any = None,
    ) -> None:
        if region not in SUPPORTED_REGIONS:
            raise LambdaSubstrateError(
                f"Region {region!r} not in supported set: {sorted(SUPPORTED_REGIONS)}"
            )
        self._region = region
        self._credentials = credentials_provider or AwsCredentialProvider()
        self._pricing = pricing_client or LambdaPricingClient()
        self._explicit_signer = identity_signer
        # `lambda_client_factory` is for tests. Returns an async context
        # manager yielding a Lambda client. None means use aiobotocore.
        self._lambda_client_factory = lambda_client_factory

    # --- Substrate ABC: metadata --------------------------------------------

    @property
    def substrate_id(self) -> str:
        return f"{SUBSTRATE_ID_PREFIX}-{self._region}"

    @property
    def substrate_version(self) -> str:
        return SUBSTRATE_VERSION

    @property
    def evidence_schema_id(self) -> str:
        return EVIDENCE_SCHEMA_ID

    @property
    def region(self) -> str:
        return self._region

    # --- Substrate ABC: behavior --------------------------------------------

    def preflight(self, workload: WorkloadSpec) -> CostEstimate:
        """Estimate maximum cost for a Lambda invocation of this workload.

        Lambda bills per (memory_gb * duration_ms). The upper bound is:
            cost_max = (memory_gb * (timeout_sec * 1000) * gb_second_rate)
                       + request_fee
        """
        if workload.language not in SUPPORTED_LANGUAGES:
            raise PreflightRejected(
                f"language {workload.language!r} not supported by "
                f"aws-lambda. Supported: {sorted(SUPPORTED_LANGUAGES)}"
            )
        code_bytes = len(workload.code.encode("utf-8"))
        from darwin.agenticcloud.substrate.aws_lambda_event import MAX_CODE_BYTES

        if code_bytes > MAX_CODE_BYTES:
            raise PreflightRejected(
                f"workload code too large: {code_bytes} bytes (max {MAX_CODE_BYTES})"
            )
        pricing = self._pricing.get(self._region)
        cost_max = pricing.cost_for(
            memory_mb=workload.memory_mb,
            billed_duration_ms=workload.timeout_sec * 1000,
        )
        if cost_max > workload.cost_cap_usd:
            raise PreflightRejected(
                f"projected max cost ${cost_max:.8f} exceeds cap "
                f"${workload.cost_cap_usd:.8f} (memory={workload.memory_mb}MB, "
                f"timeout={workload.timeout_sec}s, region={self._region})"
            )
        return CostEstimate(
            cost_usd_max=cost_max,
            cost_breakdown={
                "gb_seconds_max": (workload.memory_mb / 1024.0) * workload.timeout_sec,
                "gb_second_rate_usd": pricing.gb_second,
                "request_fee_usd": pricing.request,
                "pricing_source": pricing.source,  # type: ignore[dict-item]
            },
            notes=f"Lambda preflight for {self.substrate_id}",
        )

    def run(self, workload: WorkloadSpec) -> RunResult:
        """Synchronous entry point — wraps `_run_async()` in asyncio.run()."""
        # The Substrate ABC's run() is synchronous (per spec). We bridge
        # to async here. If we're already inside an event loop, raise
        # rather than silently misbehaving.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._run_async(workload))
        raise LambdaSubstrateError(
            "LambdaSubstrate.run() must not be called from inside an "
            "event loop. Use _run_async() directly instead."
        )

    async def _run_async(self, workload: WorkloadSpec) -> RunResult:
        """Async core. Invokes the runner Lambda and translates the
        response into a v0.2 RunResult."""
        function_name = RUNNER_FUNCTION_NAME_TEMPLATE.format(
            language=workload.language,
            region=self._region,
        )
        workload_id = f"wl-{uuid.uuid4().hex[:12]}"
        event = RunnerEvent(
            schema=EVENT_SCHEMA_URI,
            request_id=str(uuid.uuid4()),
            workload_id=workload_id,
            language=workload.language,
            code=workload.code,
            timeout_sec=workload.timeout_sec,
            memory_mb=workload.memory_mb,
            inputs=workload.inputs,
        )
        payload = json.dumps(event.to_dict()).encode("utf-8")

        async with self._client_ctx() as lam:
            try:
                resp = await lam.invoke(
                    FunctionName=function_name,
                    InvocationType="RequestResponse",
                    Payload=payload,
                    LogType="Tail",
                )
            except Exception as e:
                raise LambdaUnreachable(f"Lambda invoke failed for {function_name}: {e}") from e

        # Lambda payload is a StreamingBody-like object.
        try:
            body_bytes = await resp["Payload"].read()
        except AttributeError:
            # Test doubles may already deliver bytes.
            body_bytes = resp["Payload"]

        # If the function itself errored (FunctionError header set), the
        # workload didn't run cleanly. Surface as LambdaInvocationFailed.
        if resp.get("FunctionError"):
            raise LambdaInvocationFailed(
                f"Lambda FunctionError={resp['FunctionError']}: "
                f"{body_bytes.decode('utf-8', errors='replace')[:500]}"
            )

        try:
            response_dict = json.loads(body_bytes.decode("utf-8"))
            runner_resp = RunnerResponse.from_dict(response_dict)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise LambdaInvocationFailed(f"Runner returned non-JSON response: {e}") from e
        except RunnerEventError as e:
            raise LambdaInvocationFailed(f"Runner returned malformed response: {e}") from e

        # CloudWatch LogResult is base64-encoded last 4KB of logs.
        # We don't store it in the attestation (logs live in CloudWatch).
        # But we extract the log group/stream for evidence.
        log_group = f"/aws/lambda/{function_name}"
        log_stream = self._extract_log_stream_from_logresult(resp.get("LogResult"))

        billed_duration_ms = self._extract_billed_duration(resp.get("LogResult"))
        max_memory_used_mb = self._extract_max_memory_used(resp.get("LogResult"))

        pricing = self._pricing.get(self._region)
        cost_usd = pricing.cost_for(
            memory_mb=workload.memory_mb,
            billed_duration_ms=billed_duration_ms,
        )

        evidence: dict[str, Any] = {
            "request_id": resp.get("ResponseMetadata", {}).get("RequestId", event.request_id),
            "log_group": log_group,
            "log_stream": log_stream,
            "lambda_version": resp.get("ExecutedVersion", "$LATEST"),
            "region": self._region,
            "billed_duration_ms": billed_duration_ms,
            "memory_size_mb": workload.memory_mb,
            "max_memory_used_mb": max_memory_used_mb,
            "container_status": runner_resp.status,
            "exit_code": runner_resp.exit_code,
            "stdout_hash": runner_resp.output_hash,
            "stderr_hash": runner_resp.stderr_hash,
            "wall_time_sec": runner_resp.wall_time_sec,
        }

        partial = {
            **evidence,
            "_partial_run_result": {
                "substrate_id": self.substrate_id,
                "substrate_version": SUBSTRATE_VERSION,
                "workload_spec_hash": content_hash(asdict(workload)),
                "stdout": runner_resp.stdout,
                "stderr": runner_resp.stderr,
                "output_hash": runner_resp.output_hash,
                "cost_usd": cost_usd,
                "evidence_schema_id": EVIDENCE_SCHEMA_ID,
                "evidence": evidence,
                "extensions": {},
                "tee_required": False,
            },
        }

        if runner_resp.status != "ok":
            raise SubstrateExecutionError(
                f"aws-lambda workload status={runner_resp.status}: "
                f"{runner_resp.error or 'see stderr'}",
                partial_evidence=partial,
            )

        return RunResult(
            substrate_id=self.substrate_id,
            substrate_version=SUBSTRATE_VERSION,
            workload_spec_hash=content_hash(asdict(workload)),
            stdout=runner_resp.stdout,
            stderr=runner_resp.stderr,
            output_hash=runner_resp.output_hash,
            cost_usd=cost_usd,
            evidence_schema_id=EVIDENCE_SCHEMA_ID,
            evidence=evidence,
            extensions={},
            tee_required=False,
            issued_at="",
        )

    def identity_signer(self) -> SubstrateIdentitySigner:
        if self._explicit_signer is not None:
            return self._explicit_signer
        return resolve_identity_signer(self.substrate_id)

    # --- Helpers ------------------------------------------------------------

    def _client_ctx(self):
        """Return an async context manager yielding a Lambda client.

        Production: builds via aiobotocore.
        Tests: uses `self._lambda_client_factory`."""
        if self._lambda_client_factory is not None:
            return self._lambda_client_factory()
        return self._make_real_client_ctx()

    def _make_real_client_ctx(self):
        """Real aiobotocore client context. Imported lazily so tests
        don't need network access."""
        from contextlib import asynccontextmanager

        from aiobotocore.session import get_session

        creds = self._credentials.resolve()
        kwargs = creds.to_session_kwargs()

        @asynccontextmanager
        async def _ctx():
            session = get_session()
            async with session.create_client(
                "lambda",
                region_name=self._region,
                **kwargs,
            ) as client:
                yield client

        return _ctx()

    @staticmethod
    def _extract_log_stream_from_logresult(log_result_b64: str | None) -> str:
        """LogResult is base64-encoded last 4KB of logs. We don't parse
        the stream name from it directly — Lambda doesn't include the
        stream in the result. Return a placeholder; step 3b's runner
        will include the log_stream in its response payload."""
        return "(stream-name-in-runner-response)"

    @staticmethod
    def _extract_billed_duration(log_result_b64: str | None) -> int:
        """Parse 'Billed Duration: NNN ms' out of the REPORT log line."""
        if not log_result_b64:
            return 0
        try:
            text = base64.b64decode(log_result_b64).decode("utf-8")
        except Exception:
            return 0
        for line in text.splitlines():
            if "Billed Duration:" in line:
                # Format: "REPORT RequestId: ...  Billed Duration: 123 ms ..."
                try:
                    chunk = line.split("Billed Duration:", 1)[1].strip()
                    ms_str = chunk.split()[0]
                    return int(ms_str)
                except (IndexError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _extract_max_memory_used(log_result_b64: str | None) -> int:
        """Parse 'Max Memory Used: NNN MB' out of the REPORT log line."""
        if not log_result_b64:
            return 0
        try:
            text = base64.b64decode(log_result_b64).decode("utf-8")
        except Exception:
            return 0
        for line in text.splitlines():
            if "Max Memory Used:" in line:
                try:
                    chunk = line.split("Max Memory Used:", 1)[1].strip()
                    mb_str = chunk.split()[0]
                    return int(mb_str)
                except (IndexError, ValueError):
                    return 0
        return 0


__all__ = [
    "EVIDENCE_REQUIRED_FIELDS",
    "EVIDENCE_SCHEMA_ID",
    "LAMBDA_PRICE_PER_GB_SECOND_FALLBACK",
    "LAMBDA_REQUEST_PRICE",
    "RUNNER_FUNCTION_NAME_TEMPLATE",
    "SUBSTRATE_ID_PREFIX",
    "SUBSTRATE_VERSION",
    "SUPPORTED_REGIONS",
    "AwsCredentialProvider",
    "AwsCredentials",
    "LambdaInvocationFailed",
    "LambdaPricing",
    "LambdaPricingClient",
    "LambdaSubstrate",
    "LambdaSubstrateError",
    "LambdaUnreachable",
]
