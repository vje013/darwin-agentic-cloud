# AGENT TASK: Make Darwin Agentic Cloud A2A-discoverable

@copilot — this is a multi-PR task. Do NOT close this issue until every checklist item is complete. After each PR you open, return to this issue, update the checklist by checking the box, and pick the next unchecked item.

## Goal

Make Darwin Agentic Cloud discoverable to other agents via the A2A (Agent-to-Agent) protocol. Other A2A clients should be able to find Darwin via the well-known URI, parse its Agent Card, and invoke its skills.

## Background

A2A specification reference: https://github.com/google-a2a/A2A

Darwin already has:
- Working HTTPS endpoint at https://darwin-agentic-cloud.fly.dev
- MCP server transport (`darwin/agenticcloud/mcp_server.py`)
- HTTP transport (`darwin/agenticcloud/server.py`) with FastAPI
- Three runtime capabilities exposed via /v0/run, /v0/identity, /v0/attestations
- Ed25519 signing primitive (`darwin/agenticcloud/signing.py`)

A2A is the missing protocol layer. Once added, Darwin becomes both MCP-native AND A2A-discoverable — covering both major agent-interop standards.

## Try / Fail / Retry rules

1. Read the A2A specification at https://github.com/google-a2a/A2A first. Confirm v0.3.0+ (well-known URI is `/.well-known/agent-card.json`, NOT the older `agent.json`).
2. Every commit must be DCO signed-off OR authored by `copilot[bot]` (CI auto-exempts copilot commits).
3. If a PR fails CI, fix and retry up to 3 times. After 3 failures, add the `needs-human` label and a comment explaining the blocker.
4. Fix-it-when-you-see-it rule: if you find a systemic config issue (missing dep, broken lint rule, outdated action), open a separate PR to fix the root cause before continuing.
5. Every 6 hours, this issue gets a fresh `@copilot` mention via the outreach-loop workflow. Pick up where you left off.

## Phase 1: Agent Card schema and generation

- [ ] PR: add `darwin/agenticcloud/a2a/` module with:
  - `agent_card.py` — Pydantic model matching A2A v0.3.0+ Agent Card schema (name, description, version, supported_interfaces, capabilities, securitySchemes, security, defaultInputModes, defaultOutputModes, skills[])
  - `skills.py` — explicit skill definitions for Darwin's three core capabilities:
    1. `darwin.run` — execute a workload, get signed attestation
    2. `darwin.verify` — verify an attestation signature
    3. `darwin.identity` — return signer identity and substrate info
  - Each skill must declare id, name, description, supported input/output modes (JSON), and example requests/responses.
- [ ] PR: add `tests/test_a2a_agent_card.py` validating the card against the A2A JSON schema (vendor schema from a2a-project if available, otherwise hand-built from the spec).

## Phase 2: Well-known URI hosting

- [ ] PR: add `/.well-known/agent-card.json` endpoint to `darwin/agenticcloud/server.py`. Must return the Agent Card JSON dynamically — reading version from `darwin.agenticcloud.__version__`, substrate from `sandbox.SUBSTRATE_ID`, signer identity from `Signer.public_identity()`. NOT static JSON.
- [ ] The endpoint must serve `Content-Type: application/json` and respond to GET only.
- [ ] PR: ensure the Fly.io deployment serves this. After PR merges, verify via `curl https://darwin-agentic-cloud.fly.dev/.well-known/agent-card.json`.

## Phase 3: Optional Agent Card signing

- [ ] PR: add Ed25519 signature to the Agent Card per A2A spec section on signatures.
- [ ] Canonicalize the card with RFC 8785 (JCS) BEFORE signing — Darwin already uses JCS for attestation signing, reuse the same helper from `darwin/agenticcloud/hashing.py`.
- [ ] Embed the signature in the `signatures` array per A2A spec.
- [ ] The card must remain verifiable by clients that don't care about signatures (signature is optional).
- [ ] PR: add `tests/test_a2a_card_signing.py` validating signed cards round-trip through sign → serialize → parse → verify.

## Phase 4: A2A protocol surface implementation

- [ ] PR: implement A2A protocol endpoints in `darwin/agenticcloud/a2a/server.py`. Mount under `/a2a/v1/` on the existing FastAPI app.
- [ ] Implement at minimum: skill invocation endpoint, task status polling, capability negotiation. Read the A2A spec for the canonical surface — do not invent endpoints.
- [ ] Each A2A skill invocation must produce a Darwin attestation as part of the response (this is the differentiator — A2A invocations on Darwin are cryptographically receipted).
- [ ] PR: add `tests/test_a2a_protocol.py` covering invocation, status polling, error responses.

## Phase 5: Registry submissions and discoverability

- [ ] Search for active A2A registries/catalogs (the A2A spec mentions registries as a discovery path).
- [ ] For each registry found, fork the registry repo (if it's a GitHub-hosted list) and open a PR adding Darwin Agentic Cloud.
- [ ] Use this consistent description: "Darwin Agentic Cloud — verifiable compute substrate for AI agents. Every A2A skill invocation produces a cryptographically signed Ed25519 attestation. MCP-native and A2A-discoverable. Open source (Apache 2.0). Agent Card: https://darwin-agentic-cloud.fly.dev/.well-known/agent-card.json"
- [ ] If no public registries exist yet, open a Discussion on the google-a2a/A2A repo announcing Darwin as A2A-compliant and offering the Agent Card URL for any reference list they curate.

## Phase 6: Documentation and surface

- [ ] PR: update README.md with an "A2A" section explaining: Darwin is A2A-discoverable, here's the Agent Card URL, here are the skills, here's how to invoke them.
- [ ] PR: update the docs.html branded landing page (`darwin/agenticcloud/templates/docs.html`) to show an "A2A" badge in the chip row near "Live" and "v2.0.0", linking to the Agent Card URL.
- [ ] PR: add an A2A example to `examples/a2a_invocation.py` — a 30-line Python script that fetches Darwin's Agent Card, invokes the `darwin.run` skill, and verifies the returned attestation.

## Phase 7: Cross-protocol parity

- [ ] PR: ensure feature parity between MCP and A2A surfaces. Every MCP tool should have an A2A skill equivalent. Open a `docs/PROTOCOL_PARITY.md` table comparing the two.
- [ ] PR: update CONTRIBUTING.md "For Autonomous Agents" section to include A2A discovery instructions alongside the existing MCP instructions.

## Status reporting

After each work cycle, add a comment to this issue with:
- PRs opened (with links)
- PRs merged (with links)
- PRs blocked (with reason)
- Registry submissions made (with links)
- What you're attempting next

## Definition of done

Issue closes only when:
1. `curl https://darwin-agentic-cloud.fly.dev/.well-known/agent-card.json` returns a valid A2A v0.3.0+ Agent Card
2. The card declares at least 3 skills with full I/O modes
3. The /a2a/v1/ protocol surface is live and tested
4. An external A2A client can fetch the card and invoke `darwin.run` successfully
5. At least one A2A registry/catalog/discussion has been notified or PR'd
6. README.md and docs.html surface the A2A capability prominently
