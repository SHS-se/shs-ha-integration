import json
import unittest
from household_fixture import Rig, cloud_status


class ForecastAccuracyTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_report_reaches_status_and_logs_each_calculation_once(self):
        rig = Rig()
        report = dict(version=1, scheduled_at='2026-10-17T16:00:00Z', forecast_plan_id='plan',
                      forecast=dict(percentage=84, grade='lime', devices=[]),
                      historical=dict(percentage=74, grade='yellow', devices=[]),
                      calculations=[dict(key='battery', bias_kwh=-1.5)])
        with self.assertLogs('shs_core.household', level='INFO') as captured:
            rig.household._read_forecast_accuracy(cloud_status(forecast_accuracy=report))
            rig.household._read_forecast_accuracy(cloud_status(forecast_accuracy=report))
        self.assertEqual(len(captured.records), 1)
        logged = json.loads(captured.records[0].getMessage().split(': ', 1)[1])
        self.assertEqual(logged, report)
        self.assertEqual(rig.household.forecast_accuracy, report)
        rig.household._read_forecast_accuracy(cloud_status(forecast_accuracy=None, forecast_accuracy_error='Accuracy unavailable'))
        self.assertIsNone(rig.household.forecast_accuracy)
        self.assertEqual(rig.household.forecast_accuracy_error, 'Accuracy unavailable')
