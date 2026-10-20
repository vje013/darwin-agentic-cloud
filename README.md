# Darwin Agentic Cloud


[![PyPI](https://img.shields.io/pypi/v/darwin-agentic-cloud.svg)](https://pypi.org/project/darwin-agentic-cloud/)
[![CI](https://github.com/vje013/darwin-agentic-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/vje013/darwin-agentic-cloud/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Schema: v0.2](https://img.shields.io/badge/schema-v0.2-FFB86C.svg)](https://darwin-agentic-cloud.fly.dev/.well-known/schemas/attestation/v0.2)

> **Verifiable and free cloud compute for AI agents.**
>
> We provide an agent-first environment where every autonomous action and choice is bound and signed, including what they buy. 
>
> Your agent acts only where it can provably succeed, spends only what you authorized, and brings back a signed receipt for every step it took.
> 
> No new accounts. No new API keys.
>
> Open source. Free to users. All agent and human contributors welcome.
>
> Built on: webMCP + AP2
>
> Sandboxed Beta + Research: 6/1/2026

**Install:** `pip install darwin-agentic-cloud`

**Live demo:** https://darwin-agentic-cloud.fly.dev/demo

**Public keylist:** https://darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json

**v0.2 Schema:** https://darwin-agentic-cloud.fly.dev/.well-known/schemas/attestation/v0.2

---
## Darwin's Mission

Increase AI adoption by increasing AI safety.

---
## How to Run


**1. Install.**
`pip install darwin-agentic-cloud`
Value: one free package gives you the CLI, the Python SDK and an MCP server. No account, no API key, no card.

**2. See where you can run and what it costs.**
`darwin list`
`darwin price my_task.py`
Value: DAC finds every substrate you can use (local Docker, AWS Lambda, AWS Batch, Modal) and quotes each one before you spend anything.

**3. Run your workload under hard limits.**
`darwin run my_task.py --cost-cap 0.10 --timeout 60 --save receipt.json`
In Python: `from darwin import run; att = run(code, cost_cap=0.10, timeout=60)`
Value: the job runs sandboxed on the cheapest substrate. If the estimate exceeds your cap, it never launches. You get back the output plus a signed receipt binding the exact code, the exact output, where it ran and what it cost. Use `darwin try` to force local-only execution while testing.

**4. Keep the receipt as your audit log.**
`darwin who receipt.json`
Value: every agent action leaves a record of who signed it and on what substrate. Save the receipts and you have an append-only trail of what your agent did, in what order, at what cost.

**5. Let anyone verify it.**
`darwin verify receipt.json`
Value: anyone can check the signature offline against the public keylist. They don't have to trust you or Darwin. Change one byte of code or output and verification fails.

***Bonus for agent builders:*** 
`darwin mcp install` exposes all of this as tools in Claude Desktop, Cursor or any MCP client, so the agent itself runs through DAC and returns a receipt for every step.
