"""Real Modal smoke test. Not part of the unit suite. Run manually with:

    uv run python infra/modal_smoketest.py
    uv run python infra/modal_smoketest.py --image ghcr.io/org/engine:sha-abc123
    uv run python infra/modal_smoketest.py --timeout 3600 --code "import time; time.sleep(5); print('long')"

Requires MODAL_TOKEN_ID + MODAL_TOKEN_SECRET (or ~/.modal.toml).
Will spawn a real Modal sandbox and incur real cost (~$0.001-0.005 for the
default hello-world; a custom image adds its pull time to the wall clock).

Use --image to confirm a custom registry tag pulls, runs, and lands in
evidence.image_tag before pointing a trial runner at it.
"""

from __future__ import annotations

import argparse

from darwin.agenticcloud.substrate.modal import (
    DEFAULT_PYTHON_IMAGE,
    MAX_WALL_TIME_SEC,
    ModalConfig,
    ModalSubstrate,
)
from darwin.agenticcloud.types import WorkloadSpec


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--image", default=DEFAULT_PYTHON_IMAGE, help="Registry tag for the python sandbox image."
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=30,
        help=f"Workload timeout in seconds (1..{MAX_WALL_TIME_SEC}).",
    )
    ap.add_argument(
        "--code", default="print('hello from real modal')", help="Python source to run."
    )
    ap.add_argument("--memory", type=int, default=512, help="Sandbox memory in MB.")
    args = ap.parse_args()

    sub = ModalSubstrate(config=ModalConfig(image_python=args.image))
    ws = WorkloadSpec(
        code=args.code,
        language="python",
        timeout_sec=args.timeout,
        memory_mb=args.memory,
        cost_cap_usd=1.0,
    )
    print(f"substrate: {sub.substrate_id}")
    print(f"image:     {args.image}")
    print(f"preflight: {sub.preflight(ws)}")
    print()
    print("running on real Modal...")
    result = sub.run(ws)
    print("  status: ok")
    print(f"  stdout: {result.stdout!r}")
    print(f"  output_hash: {result.output_hash[:16]}...")
    print(f"  cost_usd:    {result.cost_usd:.6f}")
    print(f"  evidence:    {result.evidence}")
    if result.evidence["image_tag"] != args.image:
        print(f"  MISMATCH: evidence.image_tag={result.evidence['image_tag']!r} != {args.image!r}")
        return 1
    print("  image_tag in evidence matches requested tag")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
