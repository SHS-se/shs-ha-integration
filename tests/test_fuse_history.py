"""Historical subscriptions resolve per day without splitting monthly demand."""
import unittest
from datetime import date
from tests.test_tariff import catalog, config, hourly_readings, components
from shs_core.tariff import calculate_month, display_components, UnsupportedTariffError


def historical_catalog(change='2026-09-11'):
    payload = catalog(config(fuse_a=25, include_vat=True))
    payload['configuration']['fuse_periods'] = [
        {'valid_from': None, 'fuse_a': 35},
        {'valid_from': change, 'fuse_a': 25},
    ]
    for version, rates in zip(payload['profiles'][0]['versions'], [(732,292),(792,316),(904,592)]):
        version['definition']['plans']['three_phase']['fixed_monthly_sek_ex_vat'] = {'35':rates[0], '25':rates[1]}
    return payload


class FuseHistoryTests(unittest.TestCase):
    def test_september_uses_both_subscriptions_and_preserves_energy(self):
        result = calculate_month(historical_catalog(), hourly_readings(date(2026,9,1), date(2026,9,30)), '2026-09-01')
        fixed = components(result, 'fixed_fee')
        self.assertEqual([x['amount_sek'] for x in fixed], [301.33,394.67])
        self.assertEqual([(x['period_start'],x['period_end']) for x in fixed], [('2026-09-01','2026-09-10'),('2026-09-11','2026-09-30')])
        self.assertEqual(result['total_amount_sek'],870)
        displayed = [row for row in display_components(result) if row['category'] == 'fixed_fee']
        self.assertEqual([row['amount_sek'] for row in displayed], [376.66,493.34])
        self.assertEqual(result['grid_import_kwh'],0)
        self.assertEqual(result['grid_export_kwh'],0)

    def test_entire_earlier_months_use_35_a(self):
        for month, last, expected in [('2025-02-01',28,915),('2026-02-01',28,990),('2026-08-01',31,1130),('2026-10-01',31,740)]:
            first = date.fromisoformat(month)
            result = calculate_month(historical_catalog(), hourly_readings(first, first.replace(day=last)),month)
            self.assertEqual(result['total_amount_sek'],expected)

    def test_missing_hours_do_not_reduce_fixed_fee(self):
        result = calculate_month(historical_catalog(),hourly_readings(date(2026,9,12),date(2026,9,20)),'2026-09-01')
        self.assertEqual(result['total_amount_sek'],870)
        self.assertFalse(result['is_complete'])

    def test_mid_month_change_does_not_duplicate_demand(self):
        readings=hourly_readings(date(2026,2,1),date(2026,2,28),{(date(2026,2,2),12):9,(date(2026,2,16),12):6,(date(2026,2,20),12):3})
        result=calculate_month(historical_catalog('2026-02-11'),readings,'2026-02-01')
        peak=components(result,'peak_demand')
        self.assertEqual(len(peak),1)
        self.assertEqual(peak[0]['quantity'],6)
        self.assertEqual(peak[0]['amount_sek'],390)

    def test_missing_or_ambiguous_history_fails_explicitly(self):
        for periods in [None,[],[{'valid_from':'2026-09-11','fuse_a':25}],[{'valid_from':None,'fuse_a':35},{'valid_from':'2026-09-11','fuse_a':25},{'valid_from':'2026-09-11','fuse_a':35}]]:
            payload=historical_catalog()
            payload['configuration']['fuse_periods']=periods
            with self.assertRaises(UnsupportedTariffError):
                calculate_month(payload,hourly_readings(date(2026,9,1),date(2026,9,30)),'2026-09-01')
