# Darwin Agentic Cloud — Build Specification (Phase 2, in progress)

Multi-substrate routing layer. The same Darwin code runs verifiably on local Docker, AWS Lambda (4 regions), Modal, and Akash. Every execution produces a v0.2 attestation with two cryptographic signatures (substrate identity + operator) and substrate-specific evidence. Tamper-evident, auditable, open-source.

---

## 1. The Phase 2 Premise

Phase 1 shipped Darwin as a *local Docker wrapper* with cryptographic receipts. Phase 2 makes Darwin a real **compute aggregator**: the agent doesn\'t care where the workload runs because the attestation proves what happened regardless of substrate. The router picks AWS over local Docker when the workload demands it, picks Akash over AWS when cost matters, picks a TEE substrate when the workload is regulated — and every execution produces the same shape of attestation.

The Phase 2 v3.0.0 release ships with:

- **5 concrete substrates**: local-docker-v0 + aws-lambda-{us-east-1, us-west-2, eu-west-1, ap-northeast-1} + modal-v0 + akash-v0
- **2-signature attestation model**: operator-level signature over the whole attestation, plus a substrate-level identity signature scoped to the substrate that ran the workload
- **Hosted signer service** publishing a `.well-known/substrate-keys.json` keylist anyone can fetch and verify against
- **Schema v0.2** with polymorphic evidence per substrate
- **Routed CLI**: `darwin run hello.py --substrate aws-lambda-us-east-1`
- **Live hosted demo** at `darwin-agentic-cloud.fly.dev` actually executing workloads via real substrates

---

## 2. Architecture Overview (Phase 2)
┌─────────────────────────────────────────────────────────────┐
│                       Agent (LangChain, CrewAI, etc.)            │
└────────────────────────────┬───────────────────────────────────┘
│
┌────────────────┴────────────────┐
│   Transport (CLI / HTTP / MCP)  │
└────────────────┬────────────────┘
│
┌────────────────▼────────────────┐
│   Runtime + Router (Phase 2)    │
│  • pick_by_cost() / explicit    │
│  • preflight → substrate.run    │
│  • stamp issued_at, dual sign   │
└────────────────┬────────────────┘
┌───────────────────────┼───────────────────────┐
│                       │                       │
5 substrates: local-docker, aws-lambda × 4 regions, modal, akash
│                       │                       │
└───────────────────────┼───────────────────────┘
│
┌────────────────▼────────────────┐
│   Two-signature attestation     │
│  • substrate.identity_signature │
│    (class key, from hosted signer)│
│  • outer signature              │
│    (operator / per-tenant key)   │
└────────────────┬────────────────┘
│
┌────────────────▼────────────────┐
│   Storage + verification         │
│  • SQLite audit trail           │
│  • /.well-known/substrate-keys  │
│  • /v0/attestations/verify      │
└──────────────────────────────────┘

Phase 2 keeps all Phase 1 abstractions (workload spec, substrate, attestation, runtime). It adds:

- **Substrate ABC** (`darwin.agenticcloud.substrate.base.Substrate`) — interface every concrete substrate conforms to
- **Evidence Registry** — shared registry of substrate-specific evidence schemas with built-in validators
- **Substrate Identity Signer** — Protocol implemented by `RemoteClassKeySigner` (hosted) and `OperatorFallbackSigner` (self-hosted)
- **Class Key Store** — per-substrate Ed25519 keys held on Fly, served as a JWKS-like keylist
- **Router** — substrate selection by cost / policy / region

---

## 3. Attestation Schema (v0.2 — shipped Phase 2)

```json
{
  "attestation_id": "att_demo_aws-lambda-us-east-1",
  "schema": "darwin.cloud/agenticcloud/attestation/v0.2",
  "issued_at": "2026-05-25T21:25:05Z",
  "workload_spec_hash": "sha256:2cfd9d8f...bf96",
  "execution_result": {
    "output_hash": "sha256:74c73881...696e",
    "stdout": "Hello, agent.\n",
    "stderr": "",
    "cost_usd": 0.00000372,
    "substrate": {
      "id": "aws-lambda-us-east-1",
      "version": "0.1.0",
      "identity_signer_type": "darwin-class-key",
      "identity_signer_key_id": "dac-class-aws-lambda-us-east-1-{hex16}",
      "identity_signature": "base64-ed25519-sig...",
      "evidence_schema_id": "darwin.cloud/evidence/aws-lambda/v1",
      "evidence": {
        "request_id": "aws-req-...",
        "log_group": "/aws/lambda/darwin-runner-python-us-east-1",
        "log_stream": "2026/05/25/[$LATEST]...",
        "lambda_version": "$LATEST",
        "region": "us-east-1",
        "billed_duration_ms": 423,
        "memory_size_mb": 512,
        "max_memory_used_mb": 87,
        "container_status": "ok",
        "exit_code": 0,
        "stdout_hash": "sha256:74c738815abf786c...",
        "stderr_hash": "sha256:e3b0c44298fc1c14...",
        "wall_time_sec": 0.42
      },
      "extensions": {},
      "tee_required": false
    }
  },
  "signer_key_id": "dac-local-{hex16}",
  "signature": "base64-ed25519-sig..."
}
```

**Connection to Phase 1 (v0.1)**: `substrate` becomes an object (was a string). `signer_key_id` and `signature` (the outer fields) remain identical to v0.1 — the operator still vouches for the whole attestation. The new `substrate.identity_signature` is *additive*: substrate itself signs its identity declaration, allowing a verifier to independently confirm which substrate ran the workload.

**Connection to Phase 7 (TEE)**: `substrate.tee_required` flag is already in v0.2 schema. When Phase 7 ships, the same schema accommodates Intel TDX / SGX attestation quotes inside `substrate.evidence` and `substrate.extensions`. No schema bump required.

**Connection to Phase 8 (GPU)**: same pattern — GPU evidence (CUDA version, NVSwitch topology, model digest) fits inside `evidence` with a `darwin.cloud/evidence/gpu-{provider}/v1` schema URI. v0.2 schema is forward-compatible.

---

## 4. The Eight-Phase Build Plan — Phase 2 Status

### Phase 1 — Build the working substrate ✅ DONE
55 tests, v2.0.0 on PyPI, hosted demo live, Material 3 docs. Single substrate (`local-docker-v0`). See Phase 1 spec.

### Phase 2 — Multi-substrate routing ⏰ IN PROGRESS

**Done so far:**

- [x] **Substrate ABC + evidence registry** (`substrate/base.py`) — PR #17. 27 tests. Polymorphic evidence, cryptographic substrate identity, `tee_required` flag, `extensions` map.
- [x] **Two-signature attestation model** (`substrate/identity.py`) — PR #22. `RemoteClassKeySigner` + `OperatorFallbackSigner` + `resolve_identity_signer()` factory. 42 tests.
- [x] **Hosted class-key signing endpoint** — same PR. `POST /v0/sign-substrate-identity` (rate-limited via slowapi), `GET /.well-known/substrate-keys.json`. Class keys live only on Fly. 44 tests.
- [x] **Local Docker substrate adapter** (`substrate/local_docker.py`) — PR #23. Wraps the Phase 1 `DockerSandbox` without modifying it. 36 tests (35 unit + 1 real Docker integration).
- [x] **AWS Lambda substrate adapter** (`substrate/aws_lambda.py` + `substrate/aws_lambda_event.py`) — PR #24. aiobotocore-based, multi-region from day one (`aws-lambda-{region}`), shared event schema with the runner. Real public AWS Lambda pricing as wholesale cost. 58 tests.
- [x] **v0.2 branded attestation panel** (`ui.py`) — same PR. Preserves the v2.0.0 visual style with a fourth Substrate block rendering the polymorphic evidence dict. `render_attestation_panel_auto()` dispatches v0.1 vs v0.2 by schema URI.
- [x] **`darwin substrates demo` CLI** — same PR. Renders mocked v0.2 attestations through the branded panel by default; `--json` opt-out.
- [x] **Class-key bootstrap tooling** (`docker-entrypoint.sh`, `admin_cli.py`) — PR #25. `darwin admin class-keys generate / rotate / verify`. Entrypoint materializes Fly secrets into PEM files at container start. 17 tests including subprocess tests of the shell script.
- [x] **Fly ceremony: hosted signer LIVE for `local-docker-v0`** — done as operator. Class key `dac-class-local-docker-v0-ca698355dcb631e3` published at `darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json`. End-to-end verified.

**In progress:**

- [ ] **AWS Lambda 4-region deploy ceremony** (3b-3) — darwin-runner Lambda functions deployed to us-east-1, us-west-2, eu-west-1, ap-northeast-1. Two functions per region (Python + Node), two ECR repos per region. Triggered tonight.
- [ ] **Class keys for the 4 aws-lambda substrates** uploaded to Fly + entrypoint updated to materialize them (3b-4 / 3b-5).

**Still ahead in Phase 2:**

- [ ] **Modal substrate** (`substrate/modal.py`) — second AI-engineering-trusted cloud, sandbox-as-a-service tier.
- [ ] **Akash substrate** (`substrate/akash.py`) via `provider-services` CLI subprocess — decentralized cheap-tier compute.
- [ ] **Router** (`runtime/router.py`) with `pick_by_cost()` v0 policy.
- [ ] **Runtime rewire** — `Runtime.run()` calls through the substrate ABC, produces v0.2 attestations end-to-end. Phase 1 path preserved for compatibility.
- [ ] **CLI `darwin run --substrate <id>` flag**.
- [ ] **MCP tools updated with substrate parameter**.
- [ ] **Hosted demo upgraded** to do real execution via at least one substrate.
- [ ] **Ship as v3.0.0** + launch post + PyPI release + Fly redeploy.

### Phase 3 — Spec and standards play (next, ~2 weeks)
Position Darwin as the standard-writer. RFC-0001 to OpenSSF / MCP working group on v0.2 attestation schema, RFC-0002 on capability tokens, RFC-0003 on substrate identity.

### Phase 4 — Open-source agent integrations
Default sandbox layer for LangChain / CrewAI / AutoGen / OpenClaw. Drop-in tool definitions.

### Phase 5 — Design partner / DoD path
NSF SBIR Phase I, ARL BAA, ONR. Pilot deployments with one regulated team using v3.0.0\'s aws-lambda + akash substrates as the operational proof.

### Phase 6 — Hosted tier launch
$99/$999/$5K-$50K SKUs. Per-tenant signing keys derived via HKDF from a Darwin root. Stripe billing. Markup on substrate wholesale cost happens here, never in the substrate code itself.

### Phase 7 — TEE substrate
Intel TDX / SGX / AWS Nitro Enclaves. `tee_required: true` workloads route to TEE-capable substrates. v0.2 schema already accommodates the TEE quote in `substrate.evidence`.

### Phase 8 — GPU + capability marketplace
Per-second GPU pricing passthrough. Capability marketplace with 15–30% Darwin take rate on invocations.

---

## 5. Substrate Catalog (Phase 2 v3.0.0)

Each substrate exposes:
- `substrate_id` (e.g. `aws-lambda-us-east-1`)
- `substrate_version` (semver)
- `evidence_schema_id` (e.g. `darwin.cloud/evidence/aws-lambda/v1`)
- `preflight(workload) -> CostEstimate` returning WHOLESALE cost
- `run(workload) -> RunResult` returning the unsigned execution evidence
- `identity_signer() -> SubstrateIdentitySigner` for the substrate-identity signature

### 5.1 local-docker-v0 (Phase 1 + ported in Phase 2)

| Field | Value |
|-------|-------|
| Substrate ID | `local-docker-v0` |
| Container runtime | Docker (lazy daemon connect) |
| Adapter | `darwin/agenticcloud/substrate/local_docker.py` |
| Wholesale cost model | $0.0001 per wall-second (Phase 1 placeholder) |
| Evidence schema | `darwin.cloud/evidence/local-docker/v1` |
| Required evidence fields | `container_status`, `exit_code`, `stdout_hash`, `stderr_hash`, `wall_time_sec` |
| Signing key | Class key `dac-class-local-docker-v0-ca698355dcb631e3` (hosted, on Fly) |
| Status | ✅ Adapter merged. Class key uploaded. Keylist published. End-to-end verified. |

### 5.2 aws-lambda-{us-east-1, us-west-2, eu-west-1, ap-northeast-1}

| Field | Value |
|-------|-------|
| Substrate ID format | `aws-lambda-{region}` |
| Container runtime | AWS Lambda container image (per language) |
| Adapter | `darwin/agenticcloud/substrate/aws_lambda.py` |
| Runner deployment | `infra/aws_runner/deploy.py` (orchestrates ECR + Lambda + IAM) |
| Event schema | `darwin.cloud/event/aws-lambda-runner/v1` (shared between substrate and runner) |
| Wholesale cost model | AWS Lambda public pricing (real, per region) |
| Evidence schema | `darwin.cloud/evidence/aws-lambda/v1` |
| Required evidence fields | `request_id`, `log_group`, `log_stream`, `lambda_version`, `region`, `billed_duration_ms`, `memory_size_mb`, `max_memory_used_mb`, `container_status`, `exit_code`, `stdout_hash`, `stderr_hash`, `wall_time_sec` |
| Function naming | `darwin-runner-{python\|node}-{region}` |
| Status | ✅ Adapter merged. ⏰ Runner deploy in progress to 4 regions tonight. Class keys per region pending after deploy. |

### 5.3 modal-v0 (Phase 2 in-flight)

| Field | Value |
|-------|-------|
| Substrate ID | `modal-v0` |
| Container runtime | Modal sandbox / app |
| Adapter | `darwin/agenticcloud/substrate/modal.py` (not yet written) |
| Wholesale cost model | Modal billed amount passthrough |
| Evidence schema | `darwin.cloud/evidence/modal/v1` |
| Status | ⏱ Up next after AWS deploy. |

### 5.4 akash-v0 (Phase 2 in-flight)

| Field | Value |
|-------|-------|
| Substrate ID | `akash-v0` |
| Container runtime | Akash provider lease |
| Adapter | `darwin/agenticcloud/substrate/akash.py` (not yet written) |
| Implementation | Subprocess shell to `provider-services` CLI |
| Wholesale cost model | tAKT/AKT passthrough from Akash billing |
| Evidence schema | `darwin.cloud/evidence/akash/v1` |
| Status | ⏱ Up next after Modal. |

### 5.5 tee-{intel-tdx, intel-sgx, aws-nitro-enclaves} (Phase 7)

Same ABC with `tee_required: true`. Evidence schema `darwin.cloud/evidence/tee-{platform}/v1` carries TEE attestation quotes. v0.2 schema already accommodates.

### 5.6 gpu-{nvidia-h100, nvidia-a100} (Phase 8)

Same ABC with `extensions.gpu_attestation` populated. Evidence schema `darwin.cloud/evidence/gpu-{provider}/v1`. Per-second GPU billing passthrough + Darwin margin (Phase 8 hosted tier).

---

## 6. Identifiers and Hosting (Phase 2 additions)

### 6.1 Code and Packaging

Inherits everything from Phase 1 spec section 6.1. Phase 2 adds:

| Resource | Value |
|----------|-------|
| Target version | v3.0.0 (ships at end of Phase 2) |
| New runtime deps | `slowapi`, `aiobotocore`, `boto3` |
| New deploy artifact | `infra/aws_runner/` (Lambda runner code + Dockerfiles + boto3 deploy orchestrator) |

### 6.2 Hosting (Phase 2)

Phase 1\'s Fly app remains the operator + hosted-signer surface. New AWS-side hosting:

| Resource | Value |
|----------|-------|
| Hosted signer + operator | `darwin-agentic-cloud.fly.dev` (Phase 1, now serving Phase 2 endpoints) |
| Substrate-keys keylist | `darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json` (live) |
| AWS account hosting darwin-runner | `529088294890` (Darwin-managed, multi-region) |
| ECR repos | `darwin-runner-python` + `darwin-runner-node` per region (4 regions × 2 = 8 repos) |
| Lambda functions | `darwin-runner-{python\|node}-{region}` (4 regions × 2 = 8 functions) |
| IAM role | `darwin-runner-execution-role` (one global, attached to all functions) |
| Modal account | TBD (Phase 2 in-flight) |
| Akash provider | TBD (Phase 2 in-flight) |

### 6.3 Cryptographic Primitives (Phase 2 additions)

| Primitive | Choice |
|-----------|--------|
| Schema URI (current) | `darwin.cloud/agenticcloud/attestation/v0.2` |
| Class-key signing key format | `dac-class-{substrate_id}-{hex16}` |
| Identity payload domain separator | `darwin.cloud/substrate-identity/v1` |
| Class-keys keylist schema | `darwin.cloud/agenticcloud/substrate-keys/v1` |
| Class-key rotation | `{keys_dir}/{substrate}.pem.rotated/{ISO8601}.pem` archive; rotated keys remain in keylist |
| Operator fallback marker | `signer_type: "operator-fallback"` in attestation — verifiers can distinguish self-hosted from hosted |

### 6.4 Standards Bodies and External Identifiers (Phase 2)

Phase 2 produces three public artifacts that anchor Phase 3 standards submissions:

| Artifact | Submission target | Phase |
|----------|------------------|-------|
| `darwin.cloud/agenticcloud/attestation/v0.2` | OpenSSF SLSA / Sigstore | Phase 3 (RFC-0001) |
| `darwin.cloud/agenticcloud/substrate-keys/v1` (keylist format) | W3C / JOSE working group | Phase 3 (RFC-0001) |
| `darwin.cloud/event/aws-lambda-runner/v1` (substrate-event contract) | MCP working group | Phase 3 (RFC-0001) |

---

## 7. Testing Scaffolds (Phase 2)

### 7.1 Cumulative Test Suite
tests/
├── substrate/
│   ├── test_base.py             # 27 tests — ABC + evidence registry
│   ├── test_identity.py         # 42 tests — RemoteClassKey + OperatorFallback signers
│   ├── test_local_docker.py     # 36 tests — 35 unit + 1 real-Docker integration
│   └── test_aws_lambda.py       # 58 tests — mocked aiobotocore, all evidence paths
├── test_class_keys.py           # 23 tests — ClassKeyStore + generate / rotate / keylist
├── test_server_signing.py       # 21 tests — /v0/sign-substrate-identity + .well-known endpoint
└── test_admin_cli.py            # 17 tests — admin CLI + entrypoint script subprocess
Current cumulative: 278 / 278 passing (Phase 1 + Phase 2 so far)

### 7.2 Phase 2 Testing Additions Checklist

- [x] Substrate ABC conformance tests reusable across adapters (`assert_substrate_conforms_to_v02` in `tests/substrate/test_base.py`)
- [x] Evidence-registry validation tests — every adapter\'s evidence shape passes round-trip through `build_attestation_dict()`
- [x] Hosted-signer endpoint tests via FastAPI TestClient (round-trip sign + verify against published key)
- [x] Bootstrap entrypoint tested as a subprocess against synthetic env vars
- [x] AWS Lambda adapter tested with mocked aiobotocore client — zero real AWS calls in unit tests
- [ ] Router tests with parametrized cost policies (Phase 2 remaining)
- [ ] End-to-end runtime tests producing v0.2 attestations (Phase 2 remaining)

### 7.3 CI Lanes

| Lane | Python version | What it runs |
|------|----------------|--------------|
| Unit | 3.11, 3.12 | `pytest -m "not integration"` |
| Lint + types | 3.12 | `ruff check`, `ruff format --check`, `mypy` |
| DCO | — | Sign-off check on every commit |
| Integration (Docker) | 3.12 | `pytest -m integration` (skipped without Docker daemon) |

---

## 8. Phase 2 Operational Ceremonies

Two distinct ceremonies were performed (or in progress) for Phase 2:

### 8.1 Fly hosted-signer bootstrap (DONE)

1. `darwin admin class-keys generate local-docker-v0` → PEM at `./class-keys/local-docker-v0.pem`
2. `fly secrets set DARWIN_CLASS_KEY_LOCAL_DOCKER_V0="$(cat ./class-keys/local-docker-v0.pem)" -a darwin-agentic-cloud`
3. `fly deploy -a darwin-agentic-cloud` (forces redeploy with new entrypoint that materializes secrets)
4. `darwin admin class-keys verify local-docker-v0 https://darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json` → prints "match"
5. `rm -rf ./class-keys` (private material off the laptop)

Result: hosted Darwin produces real class-signed identities for `local-docker-v0`. Public keylist live.

### 8.2 AWS Lambda 4-region deploy (IN PROGRESS)

1. `AWS_PROFILE=darwin python infra/aws_runner/deploy.py --regions us-east-1,us-west-2,eu-west-1,ap-northeast-1`
2. Creates IAM execution role, ECR repos per region, builds + pushes 2 images per region, creates 2 Lambda functions per region
3. After deploy: generate 4 class keys (one per `aws-lambda-{region}` substrate)
4. Upload 4 secrets to Fly, redeploy
5. Verify all 4 substrates in the live keylist
6. Smoke-test real `darwin run hello.py --substrate aws-lambda-us-east-1` against live runner

Result (target): hosted Darwin produces real class-signed identities for all 4 `aws-lambda-{region}` substrates with real Lambda invocations.

---

## 9. Funding Sources (Phase 2)

Inherits Phase 1 spec section 9. Phase 2 specifically advances:

| Source | Phase 2 advancement |
|--------|---------------------|
| ARL BAA / ONR | v3.0.0 multi-substrate + Akash decentralized substrate as proof of "no single point of trust" — useful for the credibility-via-architecture pitch |
| NSF SBIR Phase I | v3.0.0 is the working prototype the proposal references |
| NSF PESOSE Track 3 | v0.2 attestation schema = formal artifact to cite |
| AWS Activate | Lambda + ECR costs covered for runner deployment |
| GSA AAS-CSO-2025 | Live multi-region AWS deployment = demonstrable readiness |
| Platform One Solutions Marketplace | Akash decentralized substrate = "operates outside hyperscaler trust assumption" — differentiator vs. competitors |

---

## 10. Key Metrics (post Phase 2)

| Metric | Phase 1 (v2.0.0) | Phase 2 target (v3.0.0) |
|--------|------------------|--------------------------|
| Substrates shipped | 1 | 5 (local-docker, aws-lambda × 4 regions, modal, akash) |
| Attestation schema | v0.1 | v0.2 (polymorphic substrate block) |
| Tests passing | 55 | 350+ projected |
| Class-keys published | 0 | 6 (local-docker + 4 aws-lambda + modal + akash) |
| Public verifiable endpoints | 1 (`/healthz`) | 3 (`/healthz`, `/.well-known/substrate-keys.json`, `/v0/attestations/verify`) |
| Hosted regions | 1 (Fly ord) | 1 Fly + 4 AWS regions |
| Open-source artifacts cited in upcoming RFCs | 0 | 3 (attestation v0.2, substrate-keys v1, aws-lambda-runner-event v1) |

---

## 11. Glossary (Phase 2 additions)

| Term | Definition |
|------|------------|
| **Substrate identity signature** | Ed25519 signature over the substrate\'s identity declaration. Produced by either the hosted signer\'s class key (`darwin-class-key`) or the operator\'s fallback key (`operator-fallback`). Embedded in `attestation.execution_result.substrate.identity_signature`. |
| **Class key** | Per-substrate-class Ed25519 signing key held only by the hosted signer service. Public part published in `.well-known/substrate-keys.json`. Format of identifier: `dac-class-{substrate_id}-{hex16}`. |
| **Operator fallback** | Self-hosted Darwin deployments without hosted-signer access fall back to signing substrate identity with the operator\'s per-deployment key. Marked `signer_type: "operator-fallback"` so verifiers can distinguish. |
| **Evidence registry** | Process-global registry mapping evidence schema URIs to required-field sets and substrate-specific validators. Adapters register at module import. |
| **Wholesale cost** | What Darwin pays the substrate provider per execution. Returned by `Substrate.preflight()`. Customer markup happens in the Phase 6 billing layer, never in the substrate code. |
| **Domain separator** | Constant prefix included in every signed payload to prevent cross-protocol replay. v0.2 uses `darwin.cloud/substrate-identity/v1`. |
| **Class-key rotation** | Operational ceremony where the current active class key is moved to a `{substrate}.pem.rotated/{ISO8601}.pem` archive and a new key is generated. Archived keys remain in the keylist with `status: "rotated"` so historical attestations stay verifiable. |
| **Two-signature model** | v0.2 attestations carry two signatures: the substrate signs its identity declaration, and the operator signs the whole attestation. Verifiers can independently check both. |
| **Per-region substrate ID** | AWS Lambda substrate IDs encode the region: `aws-lambda-us-east-1`, `aws-lambda-eu-west-1`, etc. Router uses this for region-aware selection. |
| **Bootstrap entrypoint** | `docker-entrypoint.sh` shell script that materializes Fly secrets into PEM files at container start. Allows class keys to be delivered as Fly secrets without baking them into the container image. |
