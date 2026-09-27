"""App-owned diagnostic export; compression never blocks the controller loop."""
import asyncio
import json
from shs_core.controller_diagnostics import controller_diagnostics, report_parts, report_summary, gzip_report

def encode(value):
    return json.dumps(value,separators=(',',':'),allow_nan=False).encode()

async def controller_download(runtime):
    panel=await runtime.editor.view()
    async with runtime.controller.lock:
        report=controller_diagnostics(runtime.controller,panel)
        report['resource_profiling']=runtime.battery.profiler.snapshot(runtime.battery.resource_counts())
        parts,summary=report_parts(report,encode),report_summary(report)
    return await asyncio.to_thread(gzip_report,parts,encode),summary
