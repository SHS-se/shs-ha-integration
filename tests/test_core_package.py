"""Distribution and pre-extraction parity, with no HA module available."""
import ast
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
CORE = ROOT / 'custom_components/shs_energy/shs_core'
sys.path.append(str(CORE.parent))
from shs_core.execution_storage import ExecutionStorage, read_execution_snapshot
from shs_core.plan_execution import feedback
from shs_core.runtime_json import encode_value
from test_execution_storage import session, META


def digest(value):
    return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class CorePackageTests(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_core_reopens_existing_format_and_keeps_pre_extraction_results(self):
        baseline = json.loads((ROOT / 'tests/fixtures/core-extraction-baseline.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copytree(CORE, tmp / 'shs_core', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(CORE.parent/'manifest.json', tmp/'manifest.json')
            path = tmp / 'execution.sqlite'
            store = ExecutionStorage(path, asyncio.to_thread)
            await store.load()
            original = session(100)
            await store.save(META, original)
            result = subprocess.run([sys.executable, '-I', '-c', '''
import sys, pathlib, importlib, json, hashlib
sys.path.insert(0, sys.argv[1])
for path in (pathlib.Path(sys.argv[1])/'shs_core').glob('*.py'):
    importlib.import_module('shs_core.'+path.stem)
from shs_core.execution_storage import read_execution_snapshot
from shs_core.runtime_json import encode_value
from shs_core.plan_execution import feedback
snapshot=read_execution_snapshot(pathlib.Path(sys.argv[1])/'execution.sqlite')
assert 'homeassistant' not in sys.modules
assert 'home_runtime' not in sys.modules
assert type(snapshot.session).__module__ == 'shs_core.home_runtime'
digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
print(json.dumps({'session':digest(encode_value(snapshot.session)), 'feedback':digest(feedback(snapshot.session.account,900000))}))
''', str(tmp)], cwd=tmp, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout), {k:baseline[k] for k in ('session','feedback')})
        replay = subprocess.run([sys.executable, str(ROOT/'scripts/replay-home-runtime.py'),
            str(ROOT/'tests/fixtures/home-runtime-crash-recovery.json')], check=True, capture_output=True, text=True)
        self.assertEqual(digest(json.loads(replay.stdout)), baseline['crash_replay'])

    def test_packaged_controllers_execute_through_ports_without_ha(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copytree(CORE, tmp/'shs_core', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(CORE.parent/'manifest.json', tmp/'manifest.json')
            result = subprocess.run([sys.executable, '-I', '-c', """
import asyncio, pathlib, sys
sys.path.insert(0, sys.argv[1])
sys.path.append(sys.argv[2])
from shs_core.controller import ScheduledController
from shs_core.battery_runtime import BatteryRuntime
from test_controller import ControllerTests
from test_battery_runtime import Rig
assert pathlib.Path(sys.modules['shs_core.controller'].__file__).is_relative_to(sys.argv[1])
assert pathlib.Path(sys.modules['shs_core.battery_runtime'].__file__).is_relative_to(sys.argv[1])
async def exercise():
    pool = ControllerTests()
    pool.setUp()
    pool.options['device_modes']['$pool'] = 'controlling'
    await pool.controller.async_start()
    assert ('switch.pool', 'on') in pool.calls, pool.controller.status
    assert not hasattr(pool.controller, 'hass')
    assert pool.controller.ownership.records['pool']['originals'] == {'switch.pool': 'off'}
    await pool.controller.async_stop()
    battery = Rig()
    await battery.start()
    assert battery.calls, battery.runtime.snapshot()
    assert battery.runtime.host.state is not None
    await battery.runtime.close()
asyncio.run(exercise())
assert 'homeassistant' not in sys.modules
print('packaged pool and battery runtimes executed through explicit ports')
""", str(tmp), str(ROOT/'tests')], cwd=tmp, check=True, capture_output=True, text=True, timeout=30)
            self.assertIn('executed through explicit ports', result.stdout)

    def test_packaged_household_runs_plan_exchange_and_source_scenarios_without_ha(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copytree(CORE, tmp/'shs_core', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(CORE.parent/'manifest.json', tmp/'manifest.json')
            result = subprocess.run([sys.executable, '-I', '-c', """
import pathlib, sys, unittest
sys.path.insert(0, sys.argv[1])
sys.path.append(sys.argv[2])
from shs_core.household import Household
import shs_core.household
assert pathlib.Path(shs_core.household.__file__).is_relative_to(sys.argv[1])
loader = unittest.TestLoader()
suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in ('test_household', 'test_plan_continuity', 'test_plan_recovery'))
result = unittest.TextTestRunner().run(suite)
assert result.wasSuccessful()
assert 'homeassistant' not in sys.modules
print('household exchange runs from the isolated distribution')
""", str(tmp), str(ROOT/'tests')], cwd=tmp, check=True, capture_output=True, text=True, timeout=30)
            self.assertIn('household exchange runs', result.stdout)

    def test_core_dependency_boundary_is_closed(self):
        modules = {p.stem for p in CORE.glob('*.py')}
        for path in CORE.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        self.assertEqual(node.level, 1, str(path))
                        names = [node.module.split('.')[0]] if node.module else [n.name for n in node.names]
                        self.assertTrue(set(names) <= modules, (path, names))
                    else:
                        self.assertIn(node.module.split('.')[0], sys.stdlib_module_names, str(path))
                elif isinstance(node, ast.Import):
                    for name in node.names:
                        self.assertIn(name.name.split('.')[0], sys.stdlib_module_names | ({'aiohttp'} if path.name == 'api.py' else set()), str(path))

    async def test_read_only_reader_does_not_complete_legacy_cleanup_or_create_files(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'execution.sqlite'
            with self.assertRaises(FileNotFoundError):
                read_execution_snapshot(path)
            self.assertFalse(path.exists())
            store = ExecutionStorage(path, asyncio.to_thread)
            await store.load()
            await store.save(META, session(3))
            with sqlite3.connect(path) as db:
                db.execute('UPDATE head SET cleanup_pending=1')
            before = path.read_bytes()
            self.assertEqual(read_execution_snapshot(path).cleanup_pending, 1)
            self.assertEqual(path.read_bytes(), before)
            with sqlite3.connect(path) as db:
                db.execute('DELETE FROM meters WHERE ordinal=0')
            with self.assertRaisesRegex(ValueError, 'count'):
                read_execution_snapshot(path)
