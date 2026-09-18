"""Read-only HAOS Python stack and process-I/O sampling over deployment SSH.

Run with Python 3 locally; the container needs CPython 3.14's _remote_debugging.
No package installation, injected code, integration reload or restart is needed.
Only function names/locations and counters are collected, never frame locals.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def process_counters(pid):
    root = Path('/proc') / str(pid)
    # comm may contain spaces or parentheses; fields after it begin at field 3.
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    io = {key: int(value) for key, value in
          (line.split(':', 1) for line in (root / 'io').read_text().splitlines())}
    return {'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
            **io}


def sample(seconds, interval_ms):
    from _remote_debugging import RemoteUnwinder

    pids = []
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = path.read_bytes().split(b'\0')
        except FileNotFoundError:
            continue  # A process exited during discovery.
        if args[1:3] == [b'-m', b'homeassistant']:
            pids.append(int(path.parent.name))
    if len(pids) != 1:
        raise RuntimeError(f'Expected one Home Assistant Python process, found {pids}')
    pid = pids[0]
    unwinder = RemoteUnwinder(pid)
    leaf, shs_leaf, shs_inclusive, errors = Counter(), Counter(), Counter(), Counter()
    callers = Counter()
    started_at = datetime.now(timezone.utc).isoformat()
    before = process_counters(pid)
    start = time.monotonic()
    attempts = successful = empty = shs_samples = 0
    while time.monotonic() - start < seconds:
        attempts += 1
        try:
            threads = unwinder.get_stack_trace()
        except (RuntimeError, OSError, UnicodeDecodeError) as error:
            # The running interpreter can change frames during a read. Keep
            # failed reads explicit; never count them as idle/non-SHS samples.
            errors[type(error).__name__] += 1
        else:
            successful += 1
            frames = threads[0][1] if threads else []
            if not frames:
                empty += 1
            else:
                def label(frame):
                    return f'{frame.filename}:{frame.lineno} {frame.funcname}'
                leaf[label(frames[0])] += 1
                callers[' <- '.join(label(frame) for frame in frames[:12])] += 1
                own = [frame for frame in frames if '/custom_components/shs_energy/' in frame.filename]
                if own:
                    shs_samples += 1
                    shs_leaf[label(own[0])] += 1
                    shs_inclusive.update({f'{Path(frame.filename).name}:{frame.funcname}' for frame in own})
        time.sleep(interval_ms / 1000)
    elapsed = time.monotonic() - start
    after = process_counters(pid)
    delta = {key: after[key] - value for key, value in before.items()}
    return {
        'started_at': started_at, 'elapsed_seconds': elapsed, 'pid': pid,
        'installed_version': json.loads(Path('/config/custom_components/shs_energy/manifest.json').read_text())['version'],
        'python_version': sys.version, 'cpu_count': os.cpu_count(),
        'process_cpu_percent_one_core': 100 * delta['cpu_seconds'] / elapsed,
        'process_cpu_percent_machine': 100 * delta['cpu_seconds'] / elapsed / os.cpu_count(),
        'process_io_delta': delta,
        'sampling': {'attempts': attempts, 'successful_reads': successful, 'empty_reads': empty,
                     'failed_reads': dict(errors), 'shs_stack_samples': shs_samples,
                     'interval_ms': interval_ms},
        'top_main_thread_frames': leaf.most_common(30),
        'top_main_thread_callers': callers.most_common(30),
        'top_shs_frames': shs_leaf.most_common(30),
        'shs_inclusive_functions': shs_inclusive.most_common(40),
        'interpretation': (
            'Stacks sample main-thread wall time, including blocking/idle waits; they are not CPU percentages. '
            'Inclusive counts overlap and must not be added. Failed/empty reads are not idle evidence. '
            'Process CPU and I/O include all HA integrations and threads, excluding child processes. '
            'rchar/wchar count file API bytes including cache; read_bytes/write_bytes count storage I/O '
            'charged by the kernel to this process. Installed version may differ until HA restarts.'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.environ.get('HA_HOST', '192.168.10.20'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('HA_PORT', '22222')))
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--interval-ms', type=float, default=20)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--inside-container', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0 < args.seconds <= 300 or not 1 <= args.interval_ms <= 1000:
        parser.error('Use 0 < seconds <= 300 and 1 <= interval-ms <= 1000')
    if args.inside_container:
        print(json.dumps(sample(args.seconds, args.interval_ms), indent=2))
        return
    result = subprocess.run([
        'ssh', '-p', str(args.port), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
        f'root@{args.host}', 'docker', 'exec', '-i', 'homeassistant', 'python', '-',
        '--inside-container', '--seconds', str(args.seconds), '--interval-ms', str(args.interval_ms),
    ], input=Path(__file__).read_text(), text=True, capture_output=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    report = json.dumps(json.loads(result.stdout), indent=2) + '\n'
    if args.output:
        args.output.write_text(report)
        print(f'Profile saved to {args.output}')
    else:
        print(report, end='')


if __name__ == '__main__':
    main()
