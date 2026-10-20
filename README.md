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
> Built on: webMCP + Docker
>
**Install:** `pip install darwin-agentic-cloud`

**Live demo:** https://darwin-agentic-cloud.fly.dev/demo

**Public keylist:** https://darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json

**v0.2 Schema:** https://darwin-agentic-cloud.fly.dev/.well-known/schemas/attestation/v0.2

---
## Darwin's Mission

Increase AI adoption by increasing AI safety.

---
## How to Run

**1. Install and start Docker.**

    pip install darwin-agentic-cloud
    darwin --help         # confirms the install and lists every command
    open -a Docker        # macOS. On Linux: sudo systemctl start docker
    docker info           # should print server details, not an error

Local runs need a running Docker daemon.

**2. Run something and save the receipt.**

    darwin run 'print("hello")' --cost-cap 0.10 --timeout 60 --save receipt.json

The job runs sandboxed under a hard cost cap and timeout. You get the output plus a signed receipt binding the exact code, output, substrate and cost. To run a script, pass its path: `darwin run ./my_task.py ...`

**3. See who signed it.**

    darwin who receipt.json

Shows the substrate and operator keys behind the receipt. Keep receipts and you have an audit trail of every agent action.

**4. Verify it.**

    darwin verify receipt.json

Anyone can check the signature offline against the public keylist. Change one byte and it fails.

**No Docker? Verify a hosted receipt instead.**

    curl -s https://darwin-agentic-cloud.fly.dev/demo/attestation.json -o att.json
    darwin who att.json
    darwin verify att.json

**For agent builders:** `darwin mcp install` adds DAC as tools in Claude Desktop, Cursor or any MCP client, so your agent gets a receipt for every step.
