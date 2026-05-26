# Open compute for AI agents

**Five substrates. Same cryptographic receipt. v3.0.0 today.**

---

Every time an AI agent runs code, three questions go unanswered.

What ran. Where it ran. What it produced.

Today, the answer is "trust us." If a LangChain agent shells out to Modal, you have a Modal log. If an OpenClaw agent shells to AWS, you have a CloudWatch trace. If your agent runs anything on the user\'s laptop, you have nothing. Three substrates, three different log formats, three different verification stories, and no way to compare them.

That\'s the problem we built Darwin Agentic Cloud to solve.

## Darwin is the receipt, not the compute

Darwin doesn\'t replace AWS, Modal, or Akash. It wraps them.

Every workload an agent runs through Darwin produces an Ed25519-signed attestation: a cryptographic receipt binding the workload hash, the output hash, the substrate that executed it, the cost, and the signer\'s identity. The substrate signs its own identity declaration. Darwin signs the whole envelope. Two signatures, one open schema.

The same agent code runs on local Docker, AWS Lambda (4 regions), Modal, or Akash. The receipt format never changes. The verifier never needs to learn a new schema.

That\'s open compute for agents.

## What ships in v3.0.0

| Substrate | Cost model | Use case |
|-----------|-----------|----------|
| `local-docker-v0` | $0.0001 / wall-second (synthetic) | Local dev, CI, low-stakes work |
| `aws-lambda-{us-east-1, us-west-2, eu-west-1, ap-northeast-1}` | AWS public pricing, passthrough | Production, enterprise, hyperscaler trust |
| `modal-v0` | Modal billing, passthrough | AI engineering trust tier |
| `akash-v0` | tAKT / AKT, passthrough | Decentralized compute, no hyperscaler dependency |

Five substrates. One schema. v0.2 attestation lives at `darwin.cloud/agenticcloud/attestation/v0.2`. Apache 2.0.

## What "open" means here

- **The schema is open.** v0.2 attestation schema is published, versioned, and verifiable at a stable URL.
- **The verification is open.** Anyone with internet access can fetch `darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json` and verify any class-signed attestation. No Darwin account required.
- **The substrate ABC is open.** Want to ship a Darwin adapter for your own substrate? It\'s a Python class with five methods. Reference implementations live in `darwin/agenticcloud/substrate/`.
- **The code is open.** Apache 2.0. `github.com/vje013/darwin-agentic-cloud`.

## The toll booth

Darwin charges nothing to verify. Darwin charges a small toll per execution it produces a receipt for.

| Tier | What you get |
|------|--------------|
| Free CLI/SDK | Self-hosted. Bring your own substrate accounts. Same open standard, no toll. |
| $99 / month individual | Hosted toll booth. Darwin invokes substrates on your behalf, signs attestations with the hosted class keys, stores audit history. |
| $999 / month team | Multi-user, audit dashboards. |
| $5K–$50K / month enterprise | Compliance reports, dedicated regions, SLA. |

You can verify Darwin attestations for free, forever, regardless of which tier produced them. The toll is for the routing, the signing, and the convenience — never the verification.

## Try it

```bash
pip install darwin-agentic-cloud
darwin run hello.py --substrate aws-lambda-us-east-1
```

```bash
# Inspect the v0.2 attestation panel
darwin substrates demo aws-lambda-us-east-1
```

```bash
# Verify any Darwin attestation — even one you didn\'t produce
curl https://darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json
```

## Where this goes next

v3.0.0 is the foundation. Phase 3 makes Darwin the standard.

- **RFC-0001** — v0.2 attestation schema submitted to OpenSSF and the MCP working group
- **RFC-0002** — capability tokens (Phase 4 prep)
- **RFC-0003** — substrate identity (Phase 7 / TEE prep)

The open-standard play depends on critical mass. Five substrates is the proof the abstraction holds. The next ten depend on the community.

## Why now

Agentic frameworks ship every week. Every new framework reinvents the same execution receipt and the same audit story. None of them interop. Enterprise buyers want one verification story. Government buyers (CMMC, FedRAMP) want a cryptographic proof. Open-source maintainers want a default sandbox layer with cryptographic guarantees.

Darwin is the abstraction that gives all three audiences the same answer.

---

**Repo:** github.com/vje013/darwin-agentic-cloud
**PyPI:** `pip install darwin-agentic-cloud`
**Hosted demo:** darwin-agentic-cloud.fly.dev
**Public keylist:** darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json
**License:** Apache 2.0
**Schema URI:** darwin.cloud/agenticcloud/attestation/v0.2
