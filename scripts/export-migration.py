#!/usr/bin/env python3
"""Export SHS after the operator stops Core; never stops or starts it itself.

Do not use on the household until the replacement app runtime is ready.
Requires the same HAOS host SSH access as deploy.sh. Export files stay in /data.
"""
import argparse
import json
import re
import shlex
import subprocess
from uuid import UUID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='192.168.10.20')
    parser.add_argument('--port', type=int, default=22222)
    parser.add_argument('--container', default='app_6dc5a974_shs_energy')
    parser.add_argument('--entry', required=True)
    parser.add_argument('--migration', required=True, help='Persist this UUID and reuse it for retries')
    args = parser.parse_args()
    UUID(args.migration)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.entry):
        parser.error('Invalid entry ID')
    ssh = ['ssh', '-p', str(args.port), 'root@'+args.host]
    command = ['docker', 'exec', '-i', args.container, 'python', '-u', '-m', 'shs_app.migration_export',
               '--entry', args.entry, '--migration', args.migration]
    with subprocess.Popen(ssh+[shlex.join(command)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as worker:
        for line in worker.stdout:
            message = json.loads(line)
            if message == {'request': 'core_state'}:
                state = subprocess.check_output(ssh+[shlex.join(['docker', 'inspect', '--format', '{{.State.Status}}', 'homeassistant'])], text=True).strip()
                worker.stdin.write(json.dumps({'core_state': state})+'\n')
                worker.stdin.flush()
            else:
                print(json.dumps(message))
        if worker.wait():
            raise SystemExit('Export failed; source may be fenced. Retry the same migration UUID after correcting the reported issue.')


if __name__ == '__main__':
    main()
