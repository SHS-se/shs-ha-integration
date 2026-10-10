"""Bounded app timings, sharing the battery profiler and its minute samples."""
from functools import wraps
from itertools import islice
import logging

from shs_core.resource_profiling import ResourceProfiler

LOGGER = logging.getLogger(__name__)


class AppProfiler(ResourceProfiler):
    OPERATIONS = ResourceProfiler.OPERATIONS + (
        'runtime_load', 'runtime_activate', 'receipt_consume', 'receipt_apply',
        'projection', 'projection_build', 'display_plan_build', 'native_projection_build', 'gateway_encode', 'gateway_exchange',
        'gateway_decode', 'ha_statistics', 'ha_states', 'ha_forecast', 'ha_metadata',
        'cloud_refresh', 'plan_exchange', 'history_upload', 'battery_inputs',
        'state_encode', 'dashboard_build', 'diagnostics_sample', 'diagnostics_encode', 'diagnostics_compress',
    )
    ASYNC_OPERATIONS = ResourceProfiler.ASYNC_OPERATIONS | frozenset((
        'runtime_load', 'runtime_activate', 'receipt_consume', 'projection',
        'gateway_exchange', 'ha_statistics', 'ha_states', 'ha_forecast', 'ha_metadata',
        'cloud_refresh', 'plan_exchange', 'history_upload', 'battery_inputs', 'diagnostics_sample',
    ))

    def interval(self):
        """Deltas, not lifetime totals; never sum potentially overlapping spans."""
        if len(self.samples) < 2:
            return None
        after, before = islice(reversed(self.samples), 2)
        seconds = after['elapsed_seconds'] - before['elapsed_seconds']
        operations = {name: {key: values[key] - before['operations'][name][key]
                             for key in ('calls', 'failures', 'wall_ms', 'cpu_ms')}
                      for name, values in after['operations'].items()}
        retained = {name: value - before['retained'][name] for name, value in after['retained'].items()
                    if name in before['retained']}
        io = {key:after['process'][key]-before['process'][key]
              for key in ('disk_write_bytes','cancelled_write_bytes','write_syscalls','disk_read_bytes')
              if key in after['process'] and key in before['process']}
        return dict(seconds=seconds, process=dict(after['process']), operations=operations, retained_delta=retained, io_delta=io)

    def log_sample(self):
        report = self.interval()
        if report is None:
            LOGGER.debug('Runtime resource baseline recorded; interval CPU and throughput available next minute')
            return
        process, counters = report['process'], report['retained_delta']
        cpu = process.get('cpu_percent_one_core')
        cpu_text = 'unavailable' if cpu is None else f'{cpu:.1f}% of one core'
        retained = self.samples[-1]['retained']
        per_minute = 60 / report['seconds']
        LOGGER.debug('Runtime resources: CPU=%s RSS=%.1f MiB receipts=%.1f/min backlog=%s '
                    'checkpoint_saves=%.1f/min evidence_query_groups=%.1f/min '
                    'decisions=%.1f/min reducer_events=%.1f/min interval=%.1fs',
                    cpu_text, process['rss_bytes'] / 1048576,
                    counters.get('app_processed_receipts', 0) * per_minute, retained.get('app_receipt_backlog', 0),
                    counters.get('storage_commits', 0) * per_minute, counters.get('evidence_queries', 0) * per_minute,
                    report['operations']['decision']['calls'] * per_minute,
                    report['operations']['reduce']['calls'] * per_minute, report['seconds'])
        # Sorting fixed, small vocabularies is cheap; the evidence itself is never loaded.
        busy = sorted(report['operations'].items(), key=lambda item: item[1]['cpu_ms'], reverse=True)
        LOGGER.debug('Runtime CPU sections (overlapping): %s; storage_worker_cpu_ms=%.1f',
                    ', '.join(f'{name}={values["cpu_ms"]:.0f}ms/{values["calls"]} calls'
                              for name, values in busy[:3] if values['calls']),
                    counters.get('storage_worker_cpu_ms', 0))
        LOGGER.debug('Runtime intake: ordered=%.1f/min replaceable=%.1f/min native_fact_commits=%.1f/min; '
                    'app_disk_write_bytes=%s app_write_syscalls=%s',
                    counters.get('source_ordered',0)*per_minute, counters.get('source_replaceable',0)*per_minute,
                    counters.get('source_fact_commits',0)*per_minute,
                    report['io_delta'].get('disk_write_bytes','unavailable'),
                    report['io_delta'].get('write_syscalls','unavailable'))
        if LOGGER.isEnabledFor(logging.DEBUG):
            for name, values in report['operations'].items():
                if values['calls']:
                    LOGGER.debug('Runtime operation %s: calls=%s failures=%s wall_ms=%.1f cpu_ms=%s',
                                 name, values['calls'], values['failures'], values['wall_ms'],
                                 f'{values["cpu_ms"]:.1f}' if self.operations[name]['cpu_measured'] else 'not measured across awaits')
            LOGGER.debug('Runtime counter deltas: %s', counters)

    def snapshot(self, retained, *, include_samples=False):
        value = super().snapshot(retained, include_samples=include_samples)
        value['basis'] = value['basis'].replace('Process gauges include every integration.',
                                              'Process gauges cover this SHS app process and its threads.')
        value['basis'] = value['basis'].replace('All profiler history resets on integration reload.',
                                              'Profiler history resets when the app runtime reconnects or restarts.')
        value['sample_count'] = len(self.samples)
        value['samples_included'] = include_samples
        value['latest_interval'] = self.interval()
        return value


def profiled(operation):
    """Async spans report wall time only, on the caller's shared profiler."""
    def decorate(method):
        @wraps(method)
        async def run(self, *args, **kwargs):
            with self.profiler.measure(operation):
                return await method(self, *args, **kwargs)
        return run
    return decorate
