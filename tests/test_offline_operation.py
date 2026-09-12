"""Cloud failures retain useful state; execution uses the complete local schedule."""
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import sys

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.insert(0, str(ROOT))
from presentation import operational_status


def methods(names, namespace):
    cls = next(n for n in ast.parse((ROOT / 'coordinator.py').read_text()).body
               if isinstance(n, ast.ClassDef) and n.name == 'ShsStatusCoordinator')
    nodes = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'coordinator.py', 'exec'), namespace)
    return namespace


class OfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_status_errors_keep_cached_values_and_plan(self):
        namespace = methods({'_async_update_data'}, dict(
            Any=object, ShsApiError=ValueError, ApiContractError=TypeError,
            UpdateFailed=RuntimeError, validate_server_contract=lambda _: None,
            _LOGGER=SimpleNamespace(warning=lambda *args: None)))
        status = {'subscription_active': True}
        plan = {'plan_id': 'last-good'}
        coordinator = SimpleNamespace(data=status, optimisation_plan=plan,
            client=SimpleNamespace(status=AsyncMock(side_effect=ValueError('504 timeout'))))
        for _ in range(10):
            self.assertIs(await namespace['_async_update_data'](coordinator), status)
            self.assertIs(coordinator.optimisation_plan, plan)
        self.assertEqual(coordinator.last_connection_error, '504 timeout')
        coordinator.data = None
        with self.assertRaises(RuntimeError):
            await namespace['_async_update_data'](coordinator)

    async def test_failed_tariff_and_price_refresh_preserve_both_series(self):
        now = datetime.now(timezone.utc)
        namespace = methods({'_async_update_data'}, dict(
            Any=object, ShsApiError=ValueError, ApiContractError=TypeError,
            TariffError=ValueError, SupplierPriceError=ValueError,
            ShsSubscriptionInactiveError=PermissionError, UpdateFailed=RuntimeError,
            validate_server_contract=lambda _: None,
            dt_util=SimpleNamespace(utcnow=lambda: now),
            _LOGGER=SimpleNamespace(warning=lambda *args: None)))
        tariff, prices = {'revision': 1}, {'slots': ['cached']}
        coordinator = SimpleNamespace(data={'subscription_active': True},
            tariff_catalog=tariff, supplier_prices=prices, tariff_status='configured',
            tariff_components={'fee': 'cached'}, _sync_subscription_issue=lambda _: None,
            _sync_missing_input_issue=lambda: None,
            client=SimpleNamespace(status=AsyncMock(return_value={'subscription_active': True}),
                tariff=AsyncMock(side_effect=ValueError('504')), prices=AsyncMock(side_effect=ValueError('504'))))
        await namespace['_async_update_data'](coordinator)
        self.assertIs(coordinator.tariff_catalog, tariff)
        self.assertIs(coordinator.supplier_prices, prices)
        self.assertEqual(coordinator.tariff_components, {'fee': 'cached'})
        self.assertEqual(coordinator.tariff_status, 'configured')
        self.assertEqual(coordinator.last_connection_success, now.isoformat())

    async def test_market_boundary_only_advances_local_sensor_values(self):
        namespace = methods({'async_price_refresh'}, {'datetime': datetime})
        updates = []
        coordinator = SimpleNamespace(async_update_listeners=lambda: updates.append(True))
        await namespace['async_price_refresh'](coordinator)
        self.assertEqual(updates, [True])

    def test_cached_commands_remain_executable_for_three_days_without_contact(self):
        plan = json.loads((Path(__file__).parent / 'fixtures/schema-7-device-plan.json').read_text())['plan']
        start = datetime.fromisoformat(plan['plans']['priority']['slots'][0]['start'])
        # Repeat a physically validated relay/thermostat quarter for a full
        # horizon. There are no battery or discrete energy-budget services.
        plan['valid_until'] = (start + timedelta(hours=72)).isoformat()
        plan['binding_until'] = (start + timedelta(hours=1)).isoformat()
        for model in plan['device_models']:
            model['forecast_w_by_slot'] = [model['forecast_w_by_slot'][0]] * 288
        for scenario in plan['plans'].values():
            first = deepcopy(scenario['slots'][0])
            scenario['slots'] = [{**deepcopy(first), 'start': (start + timedelta(minutes=15*i)).isoformat(),
                                  'binding': i < 4} for i in range(288)]
        namespace = methods({'current_plan_slot'}, {'Any': object,
            'datetime': datetime, 'timedelta': timedelta,
            'dt_util': SimpleNamespace(utcnow=lambda: now)})
        getter = namespace['current_plan_slot'].fget
        for hours in (0, 2, 24, 48, 71.75):
            now = start + timedelta(hours=hours)
            status = operational_status(plan, 'live', ['website offline'], now)
            self.assertTrue(status['actionable'], status)
            coordinator = SimpleNamespace(optimisation_plan=plan, operational_status=status)
            self.assertIsNotNone(getter(coordinator)['device_commands'])
        now = start + timedelta(hours=72)
        status = operational_status(plan, 'live', [], now)
        self.assertFalse(status['actionable'])
        self.assertEqual(status['state'], 'expired')
