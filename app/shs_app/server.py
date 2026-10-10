"""Ingress dashboard and supervisor for the app-owned household runtime."""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3

from aiohttp import ClientSession, ClientTimeout, ClientError, web

from .companion import install
from .storage import Diagnostics
from .database_census import DatabaseCensus
from .profiling import AppProfiler, profiled
from .logging_config import configure_logging, ConnectionLog
from shs_wire.protocol import PROTOCOL

LOGGER = logging.getLogger(__name__)


class Dashboard:
    def __init__(self, data, bundle, supervisor="http://supervisor", token=""):
        self.profiler = AppProfiler()
        self.data, self.bundle, self.supervisor, self.token = data, bundle, supervisor, token
        self.manifest = json.loads((bundle / "bundle.json").read_text())
        self.version = json.loads((bundle / "app.json").read_text())["version"]
        self.db = Diagnostics(data)
        self.storage_census = DatabaseCensus()
        self.connection = {"state": "connecting", "message": "Connecting to Home Assistant"}
        self.snapshot = None
        self.system = {"sampled_at": None, "resources": None, "error": None, "database": None}
        self.companion = {"state": "not_requested", "message": "Companion installation is controlled in the app's Home Assistant configuration."}
        self.app_info = None
        self.session = None
        self.engine = None
        self.control_owner = "Migration pending"
        self.connection_log = ConnectionLog(LOGGER)

    async def get(self, path):
        async with self.session.get(self.supervisor + path, headers={"Authorization":"Bearer "+self.token}) as response:
            if response.status != 200:
                raise ValueError(f"Home Assistant returned HTTP {response.status}")
            return await response.json()

    def publish_runtime(self, value):
        with self.profiler.measure('dashboard_build'):
            from math import isfinite
            from shs_core.app_projection import schedule, attention_links
            engine = self.engine
            values = value['values']
            operation = values['operational_status']
            entry = {'id':engine.identity['entry_id'], 'title':'Smart Home Solutions', 'state':'loaded',
                'operation':operation, 'schedule':schedule(values['optimisation_plan'],operation),
                'controllers':value['controllers'], 'attention':attention_links(values['attention_items'],engine.identity['entry_id']),
                'measurements':{}}
            options = engine.household.resolved_options()
            for name,key,unit in (('house','house_consumption_power_entity','W'),('solar','solar_production_power_entity','W'),
                ('grid','grid_power_entity','W'),('battery','battery_power_measurement_entity','W'),('battery_soc','battery_soc_entity','%')):
                entity = options.get(key)
                row = engine.mirror.report(entity)
                measured = None
                if row:
                    source_unit = row['attributes'].get('unit_of_measurement')
                    try:
                        raw = float(row['state'])
                        if isfinite(raw) and source_unit in ({'W','kW'} if unit=='W' else {'%'}):
                            measured = raw*(1000 if source_unit=='kW' else 1)
                    except (TypeError,ValueError):pass
                entry['measurements'][name] = dict(value=measured,unit=unit,entity_id=entity,
                    observed_at=row['last_reported'] if row else None)
            self.snapshot = dict(protocol=PROTOCOL,integration_version=engine.gateway.connected['contract']['companion_version'],
                sampled_at=datetime.now(timezone.utc).isoformat(),entries=[entry])
            self.control_owner = 'SHS app'
            self.connection = {'state':'connected','message':'App runtime active; connected to the Home Assistant gateway'}
            self.connection_log.connected()

    async def runtime(self):
        from hashlib import sha256
        from .companion import hashes
        from .engine import AppEngine
        from .backends import BackendChanged
        from .migration_check import encoded
        from .records import RecordStore
        pair = dict(protocol=self.manifest['protocol'],integration_version=self.manifest['integration_version'],
            app_version=self.version,core_sha256=sha256(encoded(hashes(self.bundle/'core/shs_core'))).hexdigest())
        target = RecordStore(self.data/'runtime-target.json')
        # The one-off import worker selects the exact private runtime directory.
        # No source integration or observer controller is started here.
        waiting = False
        while True:
            selected = await target.async_load()
            if selected is None:
                if not waiting:
                    LOGGER.info('Waiting for the saved SHS runtime migration')
                    waiting = True
                self.connection = {'state':'connecting','message':'Waiting for the one-off SHS data migration'}
                await asyncio.sleep(5)
                continue
            self.engine = AppEngine(selected['path'],self.session,self.supervisor+'/core/websocket',self.token,
                paired_release=pair,publish=self.publish_runtime,app_url='/app/'+self.app_info['slug'])
            self.profiler = self.engine.profiler
            self.connection = {'state':'recovering','message':'Restoring the saved SHS runtime and processing queued Home Assistant observations.'}
            self.connection_log.attempt()
            try:
                await self.engine.run()
            except asyncio.CancelledError:
                raise
            except BackendChanged:
                self.connection = {'state':'recovering','message':'Applying the selected plan backend.'}
                continue
            except Exception as error:
                self.connection_log.disconnected(error)
                self.connection = {'state':'disconnected','message':str(error)+'. Reconnecting with a new gateway session.'}
            await asyncio.sleep(5)

    @profiled('diagnostics_sample')
    async def resources(self):
        sampled_at = datetime.now(timezone.utc).isoformat()
        values, error = None, None
        try:
            response = await self.get("/addons/self/stats")
            values = response["data"]
            self.app_info = (await self.get("/addons/self/info"))["data"]
        except (ClientError, TimeoutError, ValueError, KeyError) as exception:
            error = str(exception)
        try:
            telemetry = {}
            if self.connection["state"] == "connected" and self.snapshot:
                now = datetime.fromisoformat(sampled_at)
                for entry in self.snapshot["entries"]:
                    plan = entry.get("schedule")
                    active = next((slot for slot in plan["household"] if
                        datetime.fromisoformat(slot["start"]) <= now < datetime.fromisoformat(slot["start"]) + timedelta(hours=slot["duration_hours"])), None) if plan else None
                    telemetry[entry["id"]] = {"measured_w": entry.get("measurements", {}).get("house", {}).get("value"),
                        "planned_w": active.get("load_w") if active else None,
                        "plan_id": plan["plan_id"] if active else None}
            await asyncio.to_thread(self.db.record, sampled_at, {"resources": values, "telemetry": telemetry})
            database = await asyncio.to_thread(self.db.snapshot)
            storage = await asyncio.to_thread(self.storage_census.sample,self.data,self.engine.root if self.engine else None,
                self.engine.identity["entry_id"] if self.engine and self.engine.identity else None)
            if self.engine and self.engine.battery:
                storage["operations"] = self.engine.battery.store.resource_counts()
            disk = await asyncio.to_thread(shutil.disk_usage, self.data)
            self.system = {"sampled_at": sampled_at, "resources": values, "error": error,
                           "database": database, "storage": storage, "filesystem_free_bytes": disk.free}
        except (OSError, ValueError, sqlite3.Error) as exception:
            self.system = {**self.system, "error": "Diagnostic storage failed: " + str(exception)}

    async def run(self):
        self.app_info = (await self.get('/addons/self/info'))['data']
        async def sample():
            while True:
                await self.resources()
                await asyncio.sleep(60)
        tasks = [asyncio.create_task(self.runtime()),asyncio.create_task(sample())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    def payload(self):
        recovery = None
        if self.engine and not self.engine.started:
            processing = self.engine.battery._processing if self.engine.battery else None
            recovery = {'processed_receipt': (processing['receipt'] - int(not processing['complete'])) if processing else None}
        return {"app_version": self.version, "required_companion": self.manifest["integration_version"],
                "recovery": recovery,
                "protocol": PROTOCOL, "connection": self.connection,
                "snapshot": self.snapshot, "system": self.system, "companion": self.companion,
                "app_slug": self.app_info.get("slug") if self.app_info else None,
                "sidebar_enabled": self.app_info.get("ingress_panel") if self.app_info else None,
                "control_owner": self.control_owner}


def create_app(observer, static, *, trusted_peer="172.30.32.2"):
    @web.middleware
    async def ingress(request, handler):
        # Check the socket peer, never X-Forwarded-For or another supplied header.
        if request.remote != trusted_peer:
            raise web.HTTPForbidden(text="Access this app through Home Assistant Ingress")
        response = await handler(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"})
        return response

    app = web.Application(middlewares=[ingress])
    async def state(request):
        with observer.profiler.measure('state_encode'):
            response = web.json_response(observer.payload(), headers={'Cache-Control':'no-store'})
        return response
    async def index(request):
        return web.FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})
    def engine():
        value = observer.engine
        if value is None or value.closed or not value.started or not value.gateway.connected:
            raise web.HTTPServiceUnavailable(text='SHS is reconnecting. Wait for the app to become ready before editing settings.')
        return value

    async def configuration(request):
        if request.content_type != 'application/json' or request.headers.get('Sec-Fetch-Site') in ('cross-site','same-site'):
            raise web.HTTPForbidden(text='Configuration changes must originate in the SHS app')
        runtime = engine()
        try:
            body = await request.json()
            if type(body) is not dict:
                raise ValueError('Configuration request must be an object')
            result = await runtime.editor.action(request.match_info['action'],body)
            if request.match_info['action'] not in ('pair_backend','select_backend'):
                await runtime.project()
            return web.json_response(result,headers={'Cache-Control':'no-store'})
        except (ValueError,TypeError,KeyError) as error:
            return web.json_response(dict(code='invalid_configuration',message=str(error),
                field_errors=getattr(error,'field_errors',{}),configuration=runtime.configuration.status()),
                status=409,headers={'Cache-Control':'no-store'})

    async def diagnostics(request):
        from .downloads import controller_download
        content,summary = await controller_download(engine())
        return web.Response(body=content,content_type='application/gzip',headers={
            'Content-Disposition':'attachment; filename="shs-controller-diagnostics.json.gz"',
            'X-SHS-Diagnostics-Summary':json.dumps(summary),'Cache-Control':'no-store'})

    app.router.add_get("/api/state", state)
    app.router.add_post('/api/configuration/{action}',configuration)
    app.router.add_get('/api/diagnostics/controller.json.gz',diagnostics)
    async def app_logs(request):
        from .downloads import app_log_download
        content = await asyncio.to_thread(app_log_download, observer.data/'logs')
        return web.Response(body=content, content_type='application/zip', headers={
            'Content-Disposition':'attachment; filename="shs-app-logs.zip"', 'Cache-Control':'no-store'})
    app.router.add_get('/api/diagnostics/app-logs.zip', app_logs)
    app.router.add_get("/", index)
    app.router.add_static("/", static, show_index=False)
    return app


async def main():
    data, bundle = Path("/data"), Path("/opt/shs/companion")
    options = json.loads((data / 'options.json').read_text())
    configure_logging(options.get('log_level','info'), data/'logs')
    observer = Dashboard(data, bundle, token=os.environ['SUPERVISOR_TOKEN'])
    LOGGER.info('Starting SHS app %s; companion=%s log_level=%s',observer.version,observer.manifest['integration_version'],options.get('log_level','info'))
    if options["install_companion"]:
        try:
            observer.companion = await asyncio.to_thread(install, bundle, Path("/homeassistant"), data)
        except (OSError, ValueError) as error:
            observer.companion = {"state": "failed", "message": str(error)}
        LOGGER.info("Companion: %s", observer.companion["message"])
    async with ClientSession(timeout=ClientTimeout(total=180)) as session:
        observer.session = session
        runner = web.AppRunner(create_app(observer, Path("/opt/shs/web")),access_log=logging.getLogger('aiohttp.access'))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 8099).start()
        task = asyncio.create_task(observer.run())
        try:
            await task
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
