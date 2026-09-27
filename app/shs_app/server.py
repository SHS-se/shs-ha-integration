"""Ingress web server and snapshot observer. This release cannot dispatch commands."""
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

LOGGER = logging.getLogger(__name__)


class Observer:
    def __init__(self, data, bundle, supervisor="http://supervisor", token=""):
        self.data, self.bundle, self.supervisor, self.token = data, bundle, supervisor, token
        self.manifest = json.loads((bundle / "bundle.json").read_text())
        self.version = json.loads((bundle / "app.json").read_text())["version"]
        self.db = Diagnostics(data)
        self.connection = {"state": "connecting", "message": "Connecting to Home Assistant"}
        self.snapshot = None
        self.system = {"sampled_at": None, "resources": None, "error": None, "database": None}
        self.companion = {"state": "not_requested", "message": "Companion installation is controlled in the app's Home Assistant configuration."}
        self.app_info = None
        self.session = None

    async def get(self, path):
        async with self.session.get(self.supervisor + path) as response:
            if response.status != 200:
                raise ValueError(f"Home Assistant returned HTTP {response.status}")
            return await response.json()

    async def observe(self):
        try:
            snapshot = await self.get("/core/api/shs_energy/app")
            if snapshot.get("protocol") != self.manifest["protocol"] or snapshot.get("integration_version") != self.manifest["integration_version"]:
                self.connection = {"state": "incompatible", "message": "Load the companion version bundled with this app, then restart Home Assistant Core.",
                                   "loaded_version": snapshot.get("integration_version")}
                self.snapshot = None
                return
            self.snapshot = snapshot
            self.connection = {"state": "connected", "message": "Connected to the Home Assistant integration"}
        except (ClientError, TimeoutError, ValueError) as error:
            self.connection = {"state": "disconnected", "message": str(error) + ". Check the companion installation and Home Assistant connection."}

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
            disk = await asyncio.to_thread(shutil.disk_usage, self.data)
            self.system = {"sampled_at": sampled_at, "resources": values, "error": error,
                           "database": database, "filesystem_free_bytes": disk.free}
        except (OSError, ValueError, sqlite3.Error) as exception:
            self.system = {**self.system, "error": "Diagnostic storage failed: " + str(exception)}

    async def run(self):
        cycle = 0
        while True:
            await self.observe()
            if cycle % 4 == 0:
                await self.resources()
            cycle += 1
            await asyncio.sleep(15)

    def payload(self):
        return {"app_version": self.version, "required_companion": self.manifest["integration_version"],
                "protocol": self.manifest["protocol"], "connection": self.connection,
                "snapshot": self.snapshot, "system": self.system, "companion": self.companion,
                "app_slug": self.app_info.get("slug") if self.app_info else None,
                "sidebar_enabled": self.app_info.get("ingress_panel") if self.app_info else None,
                "control_owner": "Home Assistant integration"}


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
        return web.json_response(observer.payload(), headers={"Cache-Control": "no-store"})
    async def index(request):
        return web.FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})
    app.router.add_get("/api/state", state)
    app.router.add_get("/", index)
    app.router.add_static("/", static, show_index=False)
    return app


async def main():
    logging.basicConfig(level=logging.INFO)
    data, bundle = Path("/data"), Path("/opt/shs/companion")
    observer = Observer(data, bundle, token=os.environ["SUPERVISOR_TOKEN"])
    options = json.loads((data / "options.json").read_text())
    if options["install_companion"]:
        try:
            observer.companion = await asyncio.to_thread(install, bundle, Path("/homeassistant"), data)
        except (OSError, ValueError) as error:
            observer.companion = {"state": "failed", "message": str(error)}
        LOGGER.info("Companion: %s", observer.companion["message"])
    async with ClientSession(headers={"Authorization": "Bearer " + observer.token}, timeout=ClientTimeout(total=10)) as session:
        observer.session = session
        runner = web.AppRunner(create_app(observer, Path("/opt/shs/web")))
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
