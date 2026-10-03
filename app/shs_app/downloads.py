"""App-owned diagnostic export; compression never blocks the controller loop."""
import asyncio
import json
from shs_core.controller_diagnostics import controller_diagnostics, report_parts, report_summary, gzip_report

def encode(value):
    return json.dumps(value,separators=(',',':'),allow_nan=False).encode()

async def controller_download(runtime):
    panel=await runtime.editor.view()
    profiler=runtime.battery.profiler
    def measured_encode(value):
        with profiler.measure('diagnostics_encode'):
            return encode(value)
    async with runtime.controller.lock:
        report=controller_diagnostics(runtime.controller,panel)
        report['resource_profiling']=profiler.snapshot(runtime.battery.resource_counts(),include_samples=True)
        parts,summary=report_parts(report,measured_encode),report_summary(report)
    def compress():
        with profiler.measure('diagnostics_compress'):
            return gzip_report(parts,measured_encode)
    return await asyncio.to_thread(compress),summary
