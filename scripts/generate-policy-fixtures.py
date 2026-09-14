#!/usr/bin/env python3
"""Generate HA reader fixtures with the existing offline backend compiler."""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("backend", type=Path)
parser.add_argument("--deno", default="deno")
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
output = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
for source, name in (("fixed-tail-reversal", "fixed-tail"), ("negative-price-recovery", "negative-price")):
    result = subprocess.run([
        args.deno, "run", "--cached-only", "--allow-read", "scripts/compile-battery-policy.ts",
        f"docs/energy-optimisation/fixtures/battery-policy/{source}.json",
    ], cwd=args.backend, check=True, capture_output=True)
    target = output / f"battery-policy-{name}.json"
    if args.check:
        if target.read_bytes() != result.stdout:
            raise SystemExit(f"Provider fixture changed: {target.name}")
    else:
        target.write_bytes(result.stdout)
    print(f"{'Verified' if args.check else 'Generated'} {target.name}")
