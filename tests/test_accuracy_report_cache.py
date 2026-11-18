"""Synthetic revision transport tests; no controller or household state changes."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from shs_core.accuracy_report_cache import AccuracyReportCache
from shs_core.api import ShsApiClient
from household_fixture import cloud_status

HOME='11111111-1111-4111-8111-111111111111'
OTHER='22222222-2222-4222-8222-222222222222'
REV='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
NEXT='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
REPORT={'version':3,'scheduled_at':'2026-11-18T06:00:00Z','calculations':[{'key':'synthetic','bias_kwh':1}]}


def status(state, revision=None, report=None, home=HOME, error=None):
    return dict(home_id=home,forecast_accuracy_state=state,forecast_accuracy_revision=revision,
                forecast_accuracy=report,forecast_accuracy_error=error,subscription_active=True)


class AccuracyReportCacheTests(unittest.TestCase):
    def test_retains_diagnostics_and_invalidates_on_correction_absence_and_error(self):
        cache=AccuracyReportCache()
        request,known=cache.begin(); self.assertIsNone(known)
        first=cache.apply(status('changed',REV,REPORT),request)
        first['forecast_accuracy']['calculations'].clear()
        request,known=cache.begin(); self.assertEqual(known,REV)
        self.assertEqual(cache.apply(status('unchanged',REV),request)['forecast_accuracy'],REPORT)
        corrected={**REPORT,'calculations':[{'key':'synthetic','bias_kwh':2}]}
        request,_=cache.begin()
        self.assertEqual(cache.apply(status('changed',NEXT,corrected),request)['forecast_accuracy'],corrected)
        request,_=cache.begin()
        self.assertIsNone(cache.apply(status('missing'),request)['forecast_accuracy'])
        self.assertIsNone(cache.begin()[1])
        request,_=cache.begin(); cache.apply(status('changed',REV,REPORT),request)
        request,_=cache.begin()
        result=cache.apply(status('unavailable',error='Accuracy unavailable'),request)
        self.assertIsNone(result['forecast_accuracy']); self.assertTrue(result['subscription_active'])
        self.assertIsNone(cache.begin()[1])

    def test_cache_loss_and_home_change_cannot_acknowledge_an_unretained_report(self):
        cache=AccuracyReportCache(); request,_=cache.begin()
        result=cache.apply(status('unchanged',REV),request)
        self.assertIsNone(result['forecast_accuracy']); self.assertEqual(result['forecast_accuracy_error'],'Accuracy unavailable')
        request,_=cache.begin(); cache.apply(status('changed',REV,REPORT),request)
        request,_=cache.begin(); result=cache.apply(status('unchanged',REV,home=OTHER),request)
        self.assertIsNone(result['forecast_accuracy']); self.assertIsNone(cache.begin()[1])
        self.assertIsNone(AccuracyReportCache().begin()[1])

    def test_older_overlapping_reply_cannot_replace_a_newer_report(self):
        cache=AccuracyReportCache(); old,_=cache.begin(); new,_=cache.begin()
        corrected={**REPORT,'calculations':[{'key':'synthetic','bias_kwh':2}]}
        cache.apply(status('changed',NEXT,corrected),new)
        self.assertEqual(cache.apply(status('changed',REV,REPORT),old)['forecast_accuracy'],corrected)
        self.assertEqual(cache.begin()[1],NEXT)

    def test_reader_first_rollout_keeps_full_responses_without_retaining_a_token(self):
        cache=AccuracyReportCache(); request,_=cache.begin()
        full=dict(home_id=HOME,forecast_accuracy=REPORT,forecast_accuracy_error=None)
        self.assertEqual(cache.apply(full,request),full)
        self.assertIsNone(cache.begin()[1])


class AccuracyApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_and_runtime_rehydrate_unchanged_reports_while_notifications_skip_them(self):
        api=ShsApiClient(None,'https://synthetic.invalid','synthetic-token')
        notification=cloud_status()
        api._request=AsyncMock(side_effect=[status('changed',REV,REPORT),
            {**status('unchanged',REV),'runtime_received':True},notification,status('unchanged',REV)])
        self.assertEqual((await api.status())['forecast_accuracy'],REPORT)
        self.assertEqual((await api.report_runtime({}))['forecast_accuracy'],REPORT)
        await api.wait_for_replan(None)
        self.assertEqual((await api.status())['forecast_accuracy'],REPORT)
        paths=[c.args[1] for c in api._request.await_args_list]
        self.assertEqual(paths,['integration-status',
            'integration-status?known_forecast_accuracy_revision='+REV,
            'integration-status?wait_for_replan=true',
            'integration-status?known_forecast_accuracy_revision='+REV])

    async def test_overlapping_status_runtime_requests_keep_the_latest_issued_report(self):
        api=ShsApiClient(None,'https://synthetic.invalid','synthetic-token')
        waiting=asyncio.Event(); release=asyncio.Event()
        async def response(method,_path,**_kwargs):
            if method=='GET':
                waiting.set(); await release.wait(); return status('changed',REV,REPORT)
            return {**status('changed',NEXT,{**REPORT,'version':3,'new':True}),'runtime_received':True}
        api._request=AsyncMock(side_effect=response)
        older=asyncio.create_task(api.status()); await waiting.wait()
        newer=await api.report_runtime({}); release.set(); earlier=await older
        self.assertEqual(earlier['forecast_accuracy'],newer['forecast_accuracy'])
        self.assertTrue(earlier['forecast_accuracy']['new'])


if __name__=='__main__':
    unittest.main()
