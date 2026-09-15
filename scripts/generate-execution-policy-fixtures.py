#!/usr/bin/env python3
"""Copy reproducible provider fixtures into the HA consumer's test contract."""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('backend', type=Path)
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
subprocess.run(['deno', 'run', '--cached-only', '--allow-read', *([] if args.check else ['--allow-write']),
                'scripts/generate-battery-execution-fixtures.ts', *(['--check'] if args.check else [])],
               cwd=args.backend, check=True)
source = args.backend / 'docs/energy-optimisation/fixtures/battery-execution'
target = Path(__file__).resolve().parents[1] / 'tests/fixtures'
for name, output in [('policy.json','battery-execution-policy.json'),
                     ('current-vectors.json','battery-execution-current-vectors.json'),
                     ('continuation-vectors.json','battery-execution-continuation-vectors.json'),
                     ('native-permissions-current-vectors.json','battery-execution-native-permissions-current-vectors.json')]:
    data = (source/name).read_bytes()
    if args.check:
        if (target/output).read_bytes() != data:
            raise SystemExit(f'Provider fixture changed: {output}')
    else:
        (target/output).write_bytes(data)
    print(f'{"Verified" if args.check else "Generated"} {output}')
