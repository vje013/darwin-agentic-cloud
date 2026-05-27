"""Real Modal smoke test. Not part of the unit suite. Run manually with:

    uv run python infra/modal_smoketest.py

Requires MODAL_TOKEN_ID + MODAL_TOKEN_SECRET (or ~/.modal.toml).
Will spawn a real Modal sandbox and incur real cost (~$0.001-0.005).
"""

from darwin.agenticcloud.substrate.modal import ModalSubstrate
from darwin.agenticcloud.types import WorkloadSpec


def main() -> int:
    sub = ModalSubstrate()
    ws = WorkloadSpec(
        code="print('hello from real modal')",
        language="python",
        timeout_sec=30,
        memory_mb=512,
        cost_cap_usd=1.0,
    )
    print(f"substrate: {sub.substrate_id}")
    print(f"preflight: {sub.preflight(ws)}")
    print()
    print("running on real Modal...")
    result = sub.run(ws)
    print(f"  status: ok")
    print(f"  stdout: {result.stdout!r}")
    print(f"  output_hash: {result.output_hash[:16]}...")
    print(f"  cost_usd:    {result.cost_usd:.6f}")
    print(f"  evidence:    {result.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
