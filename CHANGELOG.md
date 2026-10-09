# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `docker-entrypoint.sh` materializes `DARWIN_CLASS_KEY_MODAL_V0` into
  `/data/class-keys/modal-v0.pem`, closing the v3.1 known issue where `modal-v0`
  was allowlisted but the hosted signer had no key for it. Receipts for Modal
  workloads now anchor to the public keylist once the Fly ceremony runs
  (`scripts/ceremony-modal-v0.sh`).
- `infra/modal_smoketest.py` takes `--image`, `--timeout`, `--code`, `--memory`
  so a custom registry tag can be confirmed against real Modal before a trial
  runner depends on it.
- Entrypoint tests: `modal-v0` materialization, and a sync check that every
  `materialize_key` line names an allowlisted substrate with the conventional
  env var (only `akash-v0` may lack a line).

### Fixed

- Modal substrate booked provisioning time (app lookup, image build or pull,
  scheduling) as workload execution. A first run of `python:3.11-slim` reported
  `wall_time_sec` 1272 s and `cost_usd` $0.17 for a one-line print with a 30 s
  timeout, 36x the preflight ceiling the cost cap had been enforced against.
  The real factory now reports `wall_time_sec` (sandbox create to wait) and
  `provision_sec` separately; `cost_usd` follows run time only, and
  `provision_sec` is an optional evidence field.

### Changed

- Modal substrate `MAX_WALL_TIME_SEC` raised from 900 s to 3600 s. The 900 s
  figure mirrored the AWS Lambda ceiling, which does not apply to Modal
  sandboxes (24 h platform limit). Preflight upper bound at the ceiling is
  about $0.47 wholesale at the default 0.125-core sandbox.
- `ModalConfig` docstring states that `image_python` / `image_node` accept any
  public registry tag and that the tag is recorded verbatim in
  `evidence.image_tag`.

## [3.0.3] — 2026-05-28

### Added

- CLI integration tests for `darwin run` (`tests/cli/test_run.py`). 15 new tests
  covering argument parsing (no-args error, help output, documented flags),
  workload resolution (file path vs inline code, nonexistent-file fallback),
  flag handling (--substrate, --cost-cap, --timeout, --memory, --language),
  output formats (--save writes valid JSON, --json emits parseable output), and
  smoke regression for the file-exists-and-inline-code happy paths. Tests use
  `typer.testing.CliRunner` with the underlying `darwin.run()` mocked so they
  run without Docker or AWS credentials.
- New `tests/cli/` directory + `__init__.py` for future per-verb CLI test files.

### Internal

- Test suite total: 388 → 403 passing.

## [3.0.2] — 2026-05-28

### Added

- First-run welcome banner. When a user runs any `darwin` command for the first
  time after install, an ASCII brand banner prints with the tagline, motto, three
  starter commands, and links to demo, repo, and docs. Shown once per install
  via a marker file at `~/.darwin/welcomed`. Suppressed in non-interactive shells
  (pipes, scripts, CI) and when `DARWIN_SUPPRESS_WELCOME=1` is set.
- `darwin welcome` command to re-trigger the banner on demand.

### Internal

- New `render_welcome()` and `print_welcome()` functions in
  `darwin/agenticcloud/ui.py`, reusing the existing `BANNER`, `BRAND_GREEN`,
  `BRAND_AMBER`, and `BRAND_DIM` brand tokens for visual continuity with the
  attestation cert panel.

## [3.0.1] — 2026-05-27

### Fixed

- README rewrite shipped to PyPI. v3.0.0 was tagged before the README rewrite landed,
  so PyPI rendered the v2.0.0 description. v3.0.1 republishes with the full v3.0.0
  README (problem-first framing, agent-first audience, 7-verb CLI reference, Python
  API documentation, MCP setup, architecture, security model, examples, roadmap,
  software citations).
- Removed unused `# noqa: E402` directive flagged by ruff RUF100 in `darwin/__init__.py`.

No functional changes. No new dependencies. No behavior change in `darwin run`,
`darwin verify`, or any other surface.

## [3.0.0] — 2026-05-27

The verifiable-compute toll-booth. This release introduces the v0.2 attestation schema,
the multi-substrate router, and a clean seven-verb public CLI/MCP surface that mirrors
the Apple-grade `darwin.run()` agent API.

### Highlights

- **`from darwin import run` — one-line agent API.** Auto-discovers available substrates,
  routes to the cheapest one by default, executes workload, returns a cryptographically
  signed v0.2 attestation.
- **v0.2 attestation schema** at `darwin.cloud/agenticcloud/attestation/v0.2`. Adds the
  `value_added_service` block (cost-cap enforcement, routing decision, identity-signing
  metadata) on top of the substrate execution receipt.
- **Multi-substrate routing.** Workloads can run on `local-docker`, `aws-lambda`
  (4 regions), `modal`, or `aws-batch-ec2-spot-v0-us-east-1`. The router picks by cost,
  by explicit substrate, or by capability. Auto-discovery checks credentials and
  daemon availability at call time.
- **AWS Batch substrate adapter.** Real EC2 Spot pricing via `DescribeSpotPriceHistory`,
  60-second minimum billing, soft-fail on pricing API unreachability. Substrate identity
  signed by the hosted Darwin class key.
- **Engraved-certificate panel.** v0.2 attestations render as a Treasury-style
  certificate: double-line borders, gold seals (`✦`), diamond dividers, motto
  (`SECURITAS · STABILITAS · SIGNUM`), and full-center alignment. The cert reads as
  the receipt agents actually need.
- **HTML demo page** at `/demo` with a live signed attestation, downloadable JSON
  artifact, and three-step verification instructions.
- **Public v0.2 JSON Schema** (Draft 07) at `/.well-known/schemas/attestation/v0.2`
  for self-describing attestations.

### Added

- `darwin/__init__.py` re-exports `run`, `Runtime`, `CostCapExceeded` for ergonomic
  imports.
- `darwin.agenticcloud.runtime_v02.Runtime` — auto-discovering runtime that constructs
  the v0.2 attestation, validates against the schema registry, and signs the outer
  envelope.
- `darwin.agenticcloud.router` — substrate discovery + routing policies
  (`pick_by_cost`, `pick_by_substrate`, `pick_by_capability`). Short-name resolution
  (`aws-batch` → `aws-batch-ec2-spot-v0-us-east-1` using `AWS_REGION`).
- `darwin.agenticcloud.substrate.aws_batch` — full AWS Batch (EC2 Spot) substrate
  adapter with idempotent `boto3` deploy orchestrator (`infra/aws_runner/batch_deploy.py`).
- Seven top-level CLI verbs: `run`, `verify`, `price`, `list`, `sign`, `try`, `who`.
- Seven MCP tools mirroring the CLI: `darwin_run`, `darwin_verify`, `darwin_price`,
  `darwin_list`, `darwin_who`, `darwin_history`, `darwin_stats`.
- `infra/bake_demo_attestation.py` — bake-time generator for the hosted demo page.
- Pre-flight cost-cap enforcement (was in [Unreleased]). Workloads whose preflight
  estimate exceeds `cost_cap_usd` are rejected before any sandbox is launched;
  the rejection is signed.
- 79 new tests across substrate adapters, router, runtime, pricing client, schema
  validation. Total project tests: 388 passing.

### Changed

- `darwin run` is now the v0.2 entry point. Accepts code-as-string OR file path
  (heuristic: if the argument exists as a file, read it; otherwise execute inline).
- Legacy v0.1 `run` preserved at `darwin attest run-v01`.
- `darwin substrates demo` now executes real substrates by default; `--mock`
  preserves the prior synthetic behavior.
- Attestation panel renderer redesigned around the engraved-certificate aesthetic.
- `SUBSTRATE_KEYLIST_URL` updated to `https://darwin-agentic-cloud.fly.dev` until
  DNS for `darwin.cloud` is provisioned.
- MCP tool surface rewritten from `dac_*` prefix to `darwin_*`. Old tool names
  removed (breaking change for MCP client configs).

### Removed

- v0.1-only MCP tools (`dac_run_python`, `dac_run_node`, etc.). Their behavior is
  preserved via the new `darwin_run` tool with optional `language` parameter.

## [0.1.0] — initial alpha

### Added

- **Attestation layer.** Ed25519-signed, canonical-JSON-hashed, tamper-evident
  attestations binding workload spec, execution result, substrate identity,
  cost, and signer identity.
- **Docker sandbox.** Memory limits, CPU limits, pids limit, no network,
  read-only-friendly tmpfs, no-new-privileges, non-root user (65534:65534),
  all capabilities dropped. Python and Node.js runtimes.
- **Runtime orchestrator.** Single entry point: `Runtime().run(spec)`
  returns a `SignedAttestation`.
- **CLI** (`dac`): `run`, `attest verify`, `attest show`, `keys init`,
  `keys show`, `serve`, `mcp serve`, `version`.
- **HTTP server** (FastAPI): `GET /healthz`, `GET /v0/identity`,
  `POST /v0/run`, `POST /v0/attestations/verify`. Auto-generated OpenAPI
  docs at `/docs`.
- **MCP server**: stdio transport, four tools (`dac_run_python`,
  `dac_run_node`, `dac_verify_attestation`, `dac_identity`). Verified
  end-to-end with Claude Desktop.
- Apache 2.0 license, contributing guide with DCO sign-off, security
  policy, code of conduct, issue/PR templates.
- 16 attestation tests proving tamper-evidence under every tampering
  vector (stdout, cost, workload spec, signer key id, public key,
  signature), JSON round-trip, deepcopy survival, and malformed input
  rejection.

[Unreleased]: https://github.com/vje013/darwin-agentic-cloud/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vje013/darwin-agentic-cloud/releases/tag/v0.1.0
