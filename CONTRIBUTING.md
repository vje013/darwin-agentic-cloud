
# Contributing to Darwinic Agentic Cloud

Thank you for considering a contribution. DAC is developed in the open and welcomes patches, RFCs, issue reports, and discussions.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating you agree to abide by it.

## Developer Certificate of Origin (DCO)

All contributions must be signed off under the [Developer Certificate of Origin](https://developercertificate.org/). This is a lightweight alternative to a CLA: by signing off on a commit, you certify that you wrote the patch or otherwise have the right to contribute it under the project's license.

Sign off every commit by adding the `-s` flag:

```bash
git commit -s -m "feat: add foo"
```

Or configure it permanently for this repo:

```bash
git config format.signOff true
```

A signed-off commit looks like:
feat: add foo
Signed-off-by: Your Name you@example.com

PRs without DCO sign-off will be flagged by CI and cannot be merged.

## Development setup

### Prerequisites

- Python 3.11 or newer
- Docker Desktop (for sandbox tests)
- [`uv`](https://docs.astral.sh/uv/)

### Get the code

```bash
git clone https://github.com/vje013/darwin-agentic-cloud.git
cd darwin-agentic-cloud
make dev
```

This creates a `.venv/` with all dev, test, and docs dependencies.

### Common commands

```bash
make help              # show all commands
make lint              # run ruff
make format            # auto-format with ruff
make typecheck         # run mypy
make test              # run all tests
make test-unit         # fast tests, no Docker required
make test-integration  # integration tests (requires Docker)
make run               # start the DAC server locally
make mcp               # run the DAC MCP server (for Claude Desktop)
```

## Submitting a change

1. **Open an issue first** for non-trivial changes. Lets us discuss approach before you spend time on a PR.
2. **Fork and branch.** Use descriptive branch names: `feat/capability-tokens`, `fix/attestation-canonical-json`, `docs/mcp-quickstart`.
3. **Write tests.** New code must include tests. Bug fixes must include a regression test.
4. **Keep PRs small.** Smaller PRs get reviewed faster. Split refactors from feature work.
5. **Pass CI.** Lint, typecheck, and tests must all pass. Run `make check && make test` locally before pushing.
6. **Sign off your commits.** See DCO section above.
7. **Write a clear PR description.** What problem does this solve? How? What is the test plan? Reference the issue number.

## Commit message conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/) loosely:

- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation only
- `refactor:` code change that neither fixes a bug nor adds a feature
- `test:` test-only changes
- `chore:` build, tooling, CI, deps
- `perf:` performance improvement
- `security:` security-relevant change

Examples:
feat(sandbox): add gVisor runtime option
fix(attestation): canonical JSON sorts keys lexicographically
docs(mcp): add Claude Desktop walkthrough

## RFCs

Significant changes — to the attestation format, capability token grammar, substrate identity, or any public API — go through the RFC process in `docs/rfc/`.

1. Copy `docs/rfc/TEMPLATE.md` to `docs/rfc/RFC-XXXX-your-title.md` (use the next available number).
2. Open a PR with the RFC as the first commit.
3. Discussion happens in the PR. Once accepted, the RFC is merged and implementation PRs reference it.

## Security issues

Do **not** open public issues for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the responsible disclosure process.

## Areas where help is especially welcome

- Sandbox runtime implementations (Firecracker, gVisor production paths)
- TEE attestation (Intel TDX, NVIDIA H100 Confidential Compute)
- Substrate adapters (Akash, Ray, SLURM, Kubernetes)
- MCP integration tests against real agent frameworks
- Documentation and examples
- Performance benchmarks

## For Autonomous Agents

This section is for AI coding agents (Copilot, Sweep, Devin, Aider, Codex CLI, etc.) contributing to Darwin Agentic Cloud. Human contributors should follow the standard flow above.

### DCO sign-off

All commits — including those from agents — **must** include DCO sign-off. Use the `-s` flag:

```bash
git commit -s -m "feat: add foo"
```

Agents that cannot sign off interactively should include the `Signed-off-by:` trailer in the commit message body:

```text
feat(sandbox): add gVisor support

Signed-off-by: Agent Name <agent@example.com>
```

### Branch naming

Agent branches must follow the pattern:

```text
<agent>/<short-description>
```

Examples:
- `copilot/fix-attestation-hash`
- `sweep/add-bun-runtime`
- `devin/otel-logging`
- `aider/benchmark-harness`

### PR template

Agent-opened PRs must include:

1. **Title:** Conventional Commit style (e.g., `feat(sandbox): add Bun runtime`)
2. **Body:**
   - What problem this solves (reference the issue number)
   - What changed (brief summary)
   - How to test (commands to run)
3. **Labels:** Add relevant labels (`agent-contributed`, plus area labels)
4. **Linked issue:** Reference the triggering issue with `Closes #N` or `Fixes #N`

### CI requirements

All PRs must pass before merge:

- `make lint` — Ruff linting (zero warnings)
- `make typecheck` — mypy strict mode
- `make test-unit` — Unit tests (no Docker required)
- `make test-integration` — Integration tests (Docker required, runs in CI)

If CI fails, agents should attempt to fix up to 3 times. After 3 failures, add the `needs-human` label.

### Mention syntax reference

| Agent | How to invoke |
|-------|---------------|
| Copilot Coding Agent | `@copilot` in issue/PR comments |
| Sweep | Issue title prefix `sweep:` or `/sweep` comment |
| Devin | `@devin-ai-integration` in comments |
| PR-Agent | `/review`, `/improve`, `/describe` in PR comments |

For full details on each agent's capabilities, see [`docs/AGENT_INVENTORY.md`](docs/AGENT_INVENTORY.md).

### Good-first-issue guidelines for agents

Issues labeled `good first issue` + `agent-friendly` are designed for autonomous agents:
- Self-contained with clear acceptance criteria
- Include pointers to relevant source files
- Specify expected test coverage
- Reference any relevant RFCs or design docs

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
