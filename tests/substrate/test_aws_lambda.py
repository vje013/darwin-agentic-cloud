"""
tests.substrate.test_aws_lambda
===============================

Tests for `darwin.agenticcloud.substrate.aws_lambda`.

Fully mocked. Zero AWS calls.

Coverage:
- ABC conformance: substrate_id format with region, version semver,
  evidence schema registration, Substrate subclass check.
- Pricing: hardcoded rate card, fallback indicator, region-not-supported
  rejection, cost-for math.
- Preflight: budget enforcement using pricing client, language allowlist,
  oversize code rejection, region routing.
- Run happy path: invocation reaches the mocked Lambda client with the
  right function name and event payload, response parsing, evidence
  shape, log parsing for billed duration and memory used.
- Failure modes: LambdaUnreachable on invoke error, LambdaInvocationFailed
  on FunctionError, runner-status surfaces as SubstrateExecutionError
  with partial evidence.
- Credential provider: env-var resolution, session-kwarg construction.
- Event schema: round-trip, validation rejections.
- Identity signer wiring: explicit override path + resolve fallback.
- End-to-end: substrate -> attestation -> identity-signature verifies.
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from darwin.agenticcloud.hashing import canonical_json, content_hash
from darwin.agenticcloud.signing import verify_signature
from darwin.agenticcloud.substrate.aws_lambda import (
    EVIDENCE_REQUIRED_FIELDS,
    EVIDENCE_SCHEMA_ID,
    LAMBDA_PRICE_PER_GB_SECOND_FALLBACK,
    LAMBDA_REQUEST_PRICE,
    SUBSTRATE_VERSION,
    SUPPORTED_REGIONS,
    AwsCredentialProvider,
    AwsCredentials,
    LambdaInvocationFailed,
    LambdaPricing,
    LambdaPricingClient,
    LambdaSubstrate,
    LambdaSubstrateError,
    LambdaUnreachable,
)
from darwin.agenticcloud.substrate.aws_lambda_event import (
    EVENT_SCHEMA_URI,
    MAX_CODE_BYTES,
    RunnerEvent,
    RunnerEventError,
    RunnerResponse,
    validate_event_dict,
)
from darwin.agenticcloud.substrate.base import (
    EVIDENCE_REGISTRY,
    EvidenceSchemaError,
    PreflightRejected,
    RunResult,
    Substrate,
    SubstrateExecutionError,
    build_attestation_dict,
    build_identity_payload,
    iso8601_now,
    sign_identity,
)
from darwin.agenticcloud.substrate.identity import (
    ED25519_SIGNATURE_BYTES,
    OperatorFallbackSigner,
)
from darwin.agenticcloud.types import WorkloadSpec

# ============================================================================
# Helpers
# ============================================================================


def _make_workload(**overrides) -> WorkloadSpec:
    defaults = {
        "code": "print('hi')",
        "language": "python",
        "inputs": {},
        "cost_cap_usd": 1.0,
        "timeout_sec": 30,
        "memory_mb": 512,
    }
    defaults.update(overrides)
    return WorkloadSpec(**defaults)


def _build_log_result(billed_ms: int = 423, max_mem_mb: int = 87) -> str:
    """Base64-encode a realistic Lambda REPORT log line."""
    log_text = (
        "START RequestId: abc-123 Version: $LATEST\n"
        "END RequestId: abc-123\n"
        f"REPORT RequestId: abc-123\tDuration: 420.5 ms\t"
        f"Billed Duration: {billed_ms} ms\tMemory Size: 512 MB\t"
        f"Max Memory Used: {max_mem_mb} MB\t"
    )
    return base64.b64encode(log_text.encode("utf-8")).decode("ascii")


def _make_runner_response(
    *,
    status: str = "ok",
    stdout: str = "Hello, agent.\n",
    stderr: str = "",
    exit_code: int | None = 0,
    wall_time_sec: float = 0.42,
    error: str | None = None,
) -> RunnerResponse:
    from darwin.agenticcloud.hashing import sha256_hex

    return RunnerResponse(
        schema=EVENT_SCHEMA_URI,
        request_id="req-test",
        workload_id="wl-test",
        status=status,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        started_at=1716_000_000.0,
        ended_at=1716_000_000.0 + wall_time_sec,
        wall_time_sec=wall_time_sec,
        output_hash=sha256_hex(stdout.encode("utf-8")),
        stderr_hash=sha256_hex(stderr.encode("utf-8")),
        error=error,
    )


def _make_invoke_response(
    *,
    runner_resp: RunnerResponse | None = None,
    function_error: str | None = None,
    log_result_b64: str | None = None,
    executed_version: str = "$LATEST",
) -> dict:
    """Build what aiobotocore's lambda.invoke returns."""
    runner_resp = runner_resp or _make_runner_response()
    payload_bytes = json.dumps(runner_resp.to_dict()).encode("utf-8")
    payload_stream = AsyncMock()
    payload_stream.read = AsyncMock(return_value=payload_bytes)
    resp = {
        "Payload": payload_stream,
        "ExecutedVersion": executed_version,
        "LogResult": log_result_b64 or _build_log_result(),
        "ResponseMetadata": {"RequestId": "aws-req-12345"},
    }
    if function_error:
        resp["FunctionError"] = function_error
    return resp


def _make_lambda_client_factory(invoke_response: dict):
    """Return a callable that yields an async context manager wrapping a
    mocked Lambda client."""
    client = MagicMock()
    client.invoke = AsyncMock(return_value=invoke_response)

    @asynccontextmanager
    async def _ctx():
        yield client

    def _factory():
        return _ctx()

    _factory.client = client  # expose for assertions
    return _factory


# ============================================================================
# ABC conformance
# ============================================================================


class TestABCConformance:
    def test_substrate_id_includes_region(self):
        sub = LambdaSubstrate(region="us-east-1")
        assert sub.substrate_id == "aws-lambda-us-east-1"

    def test_substrate_id_eu_west_1(self):
        sub = LambdaSubstrate(region="eu-west-1")
        assert sub.substrate_id == "aws-lambda-eu-west-1"

    def test_substrate_version_is_semver(self):
        sub = LambdaSubstrate(region="us-east-1")
        parts = sub.substrate_version.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)

    def test_evidence_schema_id(self):
        sub = LambdaSubstrate(region="us-east-1")
        assert sub.evidence_schema_id == "darwin.cloud/evidence/aws-lambda/v1"

    def test_evidence_schema_registered_at_import(self):
        assert EVIDENCE_SCHEMA_ID in EVIDENCE_REGISTRY.known_ids()

    def test_is_substrate_subclass(self):
        assert issubclass(LambdaSubstrate, Substrate)

    def test_rejects_unsupported_region(self):
        with pytest.raises(LambdaSubstrateError, match="not in supported set"):
            LambdaSubstrate(region="mars-1")


# ============================================================================
# Pricing
# ============================================================================


class TestPricing:
    def test_fallback_rates_present_for_all_supported_regions(self):
        for region in SUPPORTED_REGIONS:
            assert region in LAMBDA_PRICE_PER_GB_SECOND_FALLBACK

    def test_get_returns_fallback_when_no_prefetch(self):
        client = LambdaPricingClient()
        price = client.get("us-east-1")
        assert price.region == "us-east-1"
        assert price.source == "fallback"
        assert price.gb_second == LAMBDA_PRICE_PER_GB_SECOND_FALLBACK["us-east-1"]

    def test_cost_for_uses_lambda_billing_formula(self):
        price = LambdaPricing(
            region="us-east-1",
            gb_second=0.0000166667,
            request=LAMBDA_REQUEST_PRICE,
        )
        # 512 MB = 0.5 GB; 1000 ms = 1 s
        # cost = 0.5 * 1 * 0.0000166667 + 0.0000002
        expected = 0.5 * 1.0 * 0.0000166667 + 0.0000002
        actual = price.cost_for(memory_mb=512, billed_duration_ms=1000)
        assert actual == pytest.approx(expected, rel=1e-9)

    def test_get_unknown_region_raises(self):
        client = LambdaPricingClient()
        with pytest.raises(LambdaSubstrateError, match="No pricing data"):
            client.get("mars-1")

    def test_prefetched_takes_precedence(self):
        prefetched = {
            "us-east-1": LambdaPricing(
                region="us-east-1",
                gb_second=0.0000200000,  # different from fallback
                request=LAMBDA_REQUEST_PRICE,
                source="pricing-api",
            ),
        }
        client = LambdaPricingClient(prefetched=prefetched)
        price = client.get("us-east-1")
        assert price.source == "pricing-api"
        assert price.gb_second == 0.0000200000

    def test_caches_after_first_get(self):
        client = LambdaPricingClient()
        first = client.get("us-east-1")
        second = client.get("us-east-1")
        assert first is second  # same instance, cached


# ============================================================================
# Preflight
# ============================================================================


class TestPreflight:
    def test_accepts_workload_within_budget(self):
        sub = LambdaSubstrate(region="us-east-1")
        # 30s * 0.5 GB * $0.0000166667/GB-s + $0.0000002 ~= $0.00025
        wl = _make_workload(timeout_sec=30, memory_mb=512, cost_cap_usd=0.01)
        est = sub.preflight(wl)
        assert est.cost_usd_max < 0.01
        assert "gb_seconds_max" in est.cost_breakdown
        assert est.cost_breakdown["gb_seconds_max"] == pytest.approx(15.0, abs=0.01)

    def test_rejects_workload_over_budget(self):
        sub = LambdaSubstrate(region="us-east-1")
        # Tiny cap, large memory, long timeout: cost > cap
        wl = _make_workload(
            timeout_sec=900,
            memory_mb=10240,
            cost_cap_usd=0.0001,
        )
        with pytest.raises(PreflightRejected, match="exceeds cap"):
            sub.preflight(wl)

    def test_rejects_unsupported_language(self):
        sub = LambdaSubstrate(region="us-east-1")
        wl = _make_workload(language="rust")
        with pytest.raises(PreflightRejected, match="not supported"):
            sub.preflight(wl)

    def test_rejects_oversize_code(self):
        sub = LambdaSubstrate(region="us-east-1")
        too_big = "x" * (MAX_CODE_BYTES + 1)
        wl = _make_workload(code=too_big)
        with pytest.raises(PreflightRejected, match="too large"):
            sub.preflight(wl)

    def test_cost_breakdown_includes_pricing_source(self):
        sub = LambdaSubstrate(region="us-east-1")
        est = sub.preflight(_make_workload())
        assert est.cost_breakdown["pricing_source"] == "fallback"

    def test_eu_west_1_costs_more_than_us_east_1(self):
        """Sanity check that region-specific rates flow through."""
        sub_us = LambdaSubstrate(region="us-east-1")
        sub_eu = LambdaSubstrate(region="eu-west-1")
        wl = _make_workload(timeout_sec=60, memory_mb=1024)
        est_us = sub_us.preflight(wl)
        est_eu = sub_eu.preflight(wl)
        assert est_eu.cost_usd_max > est_us.cost_usd_max


# ============================================================================
# Run happy path
# ============================================================================


class TestRunHappyPath:
    def test_returns_runresult(self):
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        result = sub.run(_make_workload())
        assert isinstance(result, RunResult)

    def test_invokes_function_with_correct_name(self):
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-west-2",
            lambda_client_factory=factory,
        )
        sub.run(_make_workload(language="python"))
        call_kwargs = factory.client.invoke.call_args.kwargs
        assert call_kwargs["FunctionName"] == "darwin-runner-python-us-west-2"

    def test_invokes_with_node_function_name(self):
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        sub.run(_make_workload(language="node", code="console.log('hi')"))
        call_kwargs = factory.client.invoke.call_args.kwargs
        assert call_kwargs["FunctionName"] == "darwin-runner-node-us-east-1"

    def test_invoke_payload_is_validated_runner_event(self):
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        sub.run(_make_workload())
        call_kwargs = factory.client.invoke.call_args.kwargs
        payload = json.loads(call_kwargs["Payload"])
        validate_event_dict(payload)
        assert payload["schema"] == EVENT_SCHEMA_URI
        assert payload["language"] == "python"

    def test_substrate_metadata_in_result(self):
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        result = sub.run(_make_workload())
        assert result.substrate_id == "aws-lambda-us-east-1"
        assert result.substrate_version == SUBSTRATE_VERSION
        assert result.evidence_schema_id == EVIDENCE_SCHEMA_ID

    def test_workload_spec_hash_matches_phase1_helper(self):
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        wl = _make_workload()
        result = sub.run(wl)
        assert result.workload_spec_hash == content_hash(asdict(wl))

    def test_cost_uses_billed_duration_from_logs(self):
        """billed_duration_ms is extracted from the REPORT log line and
        used for cost calculation."""
        log = _build_log_result(billed_ms=500, max_mem_mb=100)
        factory = _make_lambda_client_factory(_make_invoke_response(log_result_b64=log))
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        result = sub.run(_make_workload(memory_mb=512))
        # 0.5 GB * 0.5 s * $0.0000166667 + $0.0000002
        expected = 0.5 * 0.5 * 0.0000166667 + 0.0000002
        assert result.cost_usd == pytest.approx(expected, rel=1e-9)

    def test_issued_at_blank(self):
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        result = sub.run(_make_workload())
        assert result.issued_at == ""


# ============================================================================
# Evidence shape
# ============================================================================


class TestEvidenceShape:
    @pytest.fixture
    def result(self) -> RunResult:
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        return sub.run(_make_workload(memory_mb=512))

    def test_all_required_fields_present(self, result):
        assert set(result.evidence.keys()) >= EVIDENCE_REQUIRED_FIELDS

    def test_region_in_evidence(self, result):
        assert result.evidence["region"] == "us-east-1"

    def test_log_group_format(self, result):
        assert result.evidence["log_group"].startswith("/aws/lambda/")

    def test_billed_duration_parsed_from_log(self, result):
        assert result.evidence["billed_duration_ms"] == 423

    def test_max_memory_parsed_from_log(self, result):
        assert result.evidence["max_memory_used_mb"] == 87

    def test_memory_size_matches_workload(self, result):
        assert result.evidence["memory_size_mb"] == 512

    def test_container_status_ok(self, result):
        assert result.evidence["container_status"] == "ok"

    def test_registered_validator_accepts_evidence(self, result):
        # Should not raise.
        EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, result.evidence)


# ============================================================================
# Evidence validator
# ============================================================================


class TestEvidenceValidator:
    def _base_evidence(self) -> dict:
        return {
            "request_id": "r",
            "log_group": "/aws/lambda/x",
            "log_stream": "s",
            "lambda_version": "$LATEST",
            "region": "us-east-1",
            "billed_duration_ms": 100,
            "memory_size_mb": 512,
            "max_memory_used_mb": 80,
            "container_status": "ok",
            "exit_code": 0,
            "stdout_hash": "x",
            "stderr_hash": "y",
            "wall_time_sec": 0.1,
        }

    def test_accepts_valid_evidence(self):
        EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, self._base_evidence())

    def test_rejects_unknown_container_status(self):
        e = self._base_evidence()
        e["container_status"] = "exploded"
        with pytest.raises(EvidenceSchemaError, match="container_status"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, e)

    def test_rejects_unsupported_region(self):
        e = self._base_evidence()
        e["region"] = "mars-1"
        with pytest.raises(EvidenceSchemaError, match="region"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, e)

    def test_rejects_negative_billed_duration(self):
        e = self._base_evidence()
        e["billed_duration_ms"] = -1
        with pytest.raises(EvidenceSchemaError, match="billed_duration_ms"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, e)

    def test_rejects_negative_wall_time(self):
        e = self._base_evidence()
        e["wall_time_sec"] = -1.0
        with pytest.raises(EvidenceSchemaError, match="wall_time_sec"):
            EVIDENCE_REGISTRY.validate(EVIDENCE_SCHEMA_ID, e)


# ============================================================================
# Failure modes
# ============================================================================


class TestFailureModes:
    def test_invoke_raises_lambda_unreachable(self):
        client = MagicMock()
        client.invoke = AsyncMock(side_effect=RuntimeError("connection refused"))

        @asynccontextmanager
        async def _ctx():
            yield client

        def _factory():
            return _ctx()

        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=_factory,
        )
        with pytest.raises(LambdaUnreachable, match="invoke failed"):
            sub.run(_make_workload())

    def test_function_error_raises_invocation_failed(self):
        resp = _make_invoke_response()
        resp["FunctionError"] = "Unhandled"
        factory = _make_lambda_client_factory(resp)
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        with pytest.raises(LambdaInvocationFailed, match="FunctionError"):
            sub.run(_make_workload())

    def test_malformed_response_raises_invocation_failed(self):
        payload_stream = AsyncMock()
        payload_stream.read = AsyncMock(return_value=b"not json")
        resp = {
            "Payload": payload_stream,
            "ExecutedVersion": "$LATEST",
            "LogResult": _build_log_result(),
            "ResponseMetadata": {"RequestId": "x"},
        }
        factory = _make_lambda_client_factory(resp)
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        with pytest.raises(LambdaInvocationFailed):
            sub.run(_make_workload())

    @pytest.mark.parametrize("status", ["error", "timeout", "oom"])
    def test_runner_status_raises_substrate_execution_error(self, status):
        runner_resp = _make_runner_response(status=status, error="boom")
        factory = _make_lambda_client_factory(_make_invoke_response(runner_resp=runner_resp))
        sub = LambdaSubstrate(
            region="us-east-1",
            lambda_client_factory=factory,
        )
        with pytest.raises(SubstrateExecutionError) as exc_info:
            sub.run(_make_workload())
        pe = exc_info.value.partial_evidence
        assert pe["container_status"] == status
        assert "_partial_run_result" in pe
        assert pe["_partial_run_result"]["substrate_id"] == "aws-lambda-us-east-1"


# ============================================================================
# Credential provider
# ============================================================================


class TestCredentialProvider:
    def test_reads_darwin_aws_env_vars(self, monkeypatch):
        monkeypatch.setenv("DARWIN_AWS_ACCESS_KEY_ID", "AKIA-test")
        monkeypatch.setenv("DARWIN_AWS_SECRET_ACCESS_KEY", "secret-test")
        monkeypatch.setenv("DARWIN_AWS_SESSION_TOKEN", "token-test")
        provider = AwsCredentialProvider()
        creds = provider.resolve()
        assert creds.access_key_id == "AKIA-test"
        assert creds.secret_access_key == "secret-test"
        assert creds.session_token == "token-test"

    def test_returns_none_when_env_unset(self, monkeypatch):
        for v in (
            "DARWIN_AWS_ACCESS_KEY_ID",
            "DARWIN_AWS_SECRET_ACCESS_KEY",
            "DARWIN_AWS_SESSION_TOKEN",
        ):
            monkeypatch.delenv(v, raising=False)
        creds = AwsCredentialProvider().resolve()
        assert creds.access_key_id is None
        assert creds.secret_access_key is None
        assert creds.session_token is None

    def test_session_kwargs_omits_none(self, monkeypatch):
        for v in (
            "DARWIN_AWS_ACCESS_KEY_ID",
            "DARWIN_AWS_SECRET_ACCESS_KEY",
            "DARWIN_AWS_SESSION_TOKEN",
        ):
            monkeypatch.delenv(v, raising=False)
        creds = AwsCredentialProvider().resolve()
        assert creds.to_session_kwargs() == {}

    def test_session_kwargs_includes_set_values(self):
        creds = AwsCredentials(
            access_key_id="AKIA-x",
            secret_access_key="sec",
        )
        kwargs = creds.to_session_kwargs()
        assert kwargs["aws_access_key_id"] == "AKIA-x"
        assert kwargs["aws_secret_access_key"] == "sec"
        assert "aws_session_token" not in kwargs


# ============================================================================
# Event schema
# ============================================================================


class TestEventSchema:
    def test_roundtrip(self):
        ev = RunnerEvent(
            schema=EVENT_SCHEMA_URI,
            request_id="r",
            workload_id="w",
            language="python",
            code="x",
            timeout_sec=30,
            memory_mb=512,
            inputs={},
        )
        ev2 = RunnerEvent.from_dict(ev.to_dict())
        assert ev == ev2

    def test_rejects_unsupported_language(self):
        with pytest.raises(RunnerEventError, match="language"):
            RunnerEvent.from_dict(
                {
                    "schema": EVENT_SCHEMA_URI,
                    "request_id": "r",
                    "workload_id": "w",
                    "language": "rust",
                    "code": "x",
                    "timeout_sec": 30,
                    "memory_mb": 512,
                }
            )

    def test_rejects_missing_fields(self):
        with pytest.raises(RunnerEventError, match="missing"):
            RunnerEvent.from_dict({"schema": EVENT_SCHEMA_URI})

    def test_rejects_oversize_code(self):
        with pytest.raises(RunnerEventError, match="exceeds max size"):
            RunnerEvent.from_dict(
                {
                    "schema": EVENT_SCHEMA_URI,
                    "request_id": "r",
                    "workload_id": "w",
                    "language": "python",
                    "code": "x" * (MAX_CODE_BYTES + 1),
                    "timeout_sec": 30,
                    "memory_mb": 512,
                }
            )

    def test_rejects_wrong_schema(self):
        with pytest.raises(RunnerEventError, match="schema mismatch"):
            RunnerEvent.from_dict(
                {
                    "schema": "wrong-schema",
                    "request_id": "r",
                    "workload_id": "w",
                    "language": "python",
                    "code": "x",
                    "timeout_sec": 30,
                    "memory_mb": 512,
                }
            )


# ============================================================================
# Identity signer wiring
# ============================================================================


class TestIdentitySignerWiring:
    def test_explicit_signer_used_when_provided(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        explicit = OperatorFallbackSigner()
        sub = LambdaSubstrate(
            region="us-east-1",
            identity_signer=explicit,
        )
        assert sub.identity_signer() is explicit

    def test_resolve_falls_back_to_operator(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("DARWIN_SIGNER_URL", "")
        sub = LambdaSubstrate(region="us-east-1")
        assert sub.identity_signer().signer_type == "operator-fallback"


# ============================================================================
# End-to-end
# ============================================================================


class TestEndToEnd:
    def test_full_attestation_flow(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DARWIN_STATE_DIR", str(tmp_path))
        signer = OperatorFallbackSigner()
        factory = _make_lambda_client_factory(_make_invoke_response())
        sub = LambdaSubstrate(
            region="us-east-1",
            identity_signer=signer,
            lambda_client_factory=factory,
        )

        wl = _make_workload()
        sub.preflight(wl)
        result = sub.run(wl)

        # Stamp issued_at and sign.
        result_final = RunResult(
            substrate_id=result.substrate_id,
            substrate_version=result.substrate_version,
            workload_spec_hash=result.workload_spec_hash,
            stdout=result.stdout,
            stderr=result.stderr,
            output_hash=result.output_hash,
            cost_usd=result.cost_usd,
            evidence_schema_id=result.evidence_schema_id,
            evidence=result.evidence,
            extensions=result.extensions,
            tee_required=result.tee_required,
            issued_at=iso8601_now(),
        )
        identity = sign_identity(result=result_final, signer=signer)
        att = build_attestation_dict(
            attestation_id="att_e2e_lambda",
            result=result_final,
            identity=identity,
        )
        assert att["schema"] == "darwin.cloud/agenticcloud/attestation/v0.2"
        assert att["execution_result"]["substrate"]["id"] == "aws-lambda-us-east-1"
        sig_bytes = base64.b64decode(identity.identity_signature)
        assert len(sig_bytes) == ED25519_SIGNATURE_BYTES

        # Verify identity signature with operator's public key.
        payload = build_identity_payload(
            substrate_id=result_final.substrate_id,
            substrate_version=result_final.substrate_version,
            workload_spec_hash=result_final.workload_spec_hash,
            output_hash=result_final.output_hash,
            evidence_schema_id=result_final.evidence_schema_id,
            issued_at=result_final.issued_at,
        )
        canonical = canonical_json(payload)
        assert (
            verify_signature(
                canonical,
                att["execution_result"]["substrate"]["identity_signature"],
                signer.public_key_b64,
            )
            is True
        )
