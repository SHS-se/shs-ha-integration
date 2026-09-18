"""A plan's contract is checked once per clock regime while it is the owner's plan."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from optimisation import OptimisationInputError, PlanContractCache, validate_plan_contract
from presentation import operational_status

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/schema-9-mixed-mode-plan.json').read_text())


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class PlanContractCacheTests(unittest.TestCase):
    def setUp(self):
        self.plan = deepcopy(FIXTURE['plan'])
        self.current = self.plan
        self.now = instant(FIXTURE['validation_time'])
        self.checked = []

        def validate(plan, now, **kwargs):
            self.checked.append(plan)
            validate_plan_contract(plan, now, **kwargs)

        self.cache = PlanContractCache(lambda: self.current, validate)

    def check(self, plan, at=None, **kwargs):
        self.cache(plan, at or self.now, **{'require_recent_issue': False, **kwargs})

    def test_repeated_reads_check_each_branch_once(self):
        for _ in range(3):
            self.check(self.plan)
            self.check(self.plan['execution_plan'])
        self.assertEqual([id(plan) for plan in self.checked], [id(self.plan), id(self.plan['execution_plan'])])

    def test_crossing_expiry_uses_the_verdict_for_that_time(self):
        expiry = instant(self.plan['valid_until'])
        self.check(self.plan)
        for _ in range(2):
            with self.assertRaisesRegex(OptimisationInputError, 'already expired'):
                self.check(self.plan, expiry)
        self.check(self.plan)
        self.assertEqual(len(self.checked), 3)

    def test_recent_issue_requirement_is_part_of_the_verdict(self):
        late = instant(self.plan['issued_at']) + timedelta(minutes=20)
        self.check(self.plan, late)
        with self.assertRaisesRegex(OptimisationInputError, 'not issued recently'):
            self.check(self.plan, late, require_recent_issue=True)
        self.check(self.plan, late)
        self.assertEqual(len(self.checked), 3)

    def test_failures_are_reused_with_the_same_error(self):
        self.plan['services'] = 'not a list'
        messages = []
        for _ in range(3):
            with self.assertRaises(OptimisationInputError) as caught:
                self.check(self.plan)
            messages.append(str(caught.exception))
        self.assertEqual(messages, [messages[0]] * 3)
        self.assertEqual(len(self.checked), 1)

    def test_replacing_the_plan_discards_earlier_verdicts(self):
        previous = self.plan
        self.check(previous)
        self.current = deepcopy(previous)
        self.check(self.current)
        self.check(self.current)
        self.assertEqual(len(self.checked), 2)
        # A plan that is no longer the owner's is still checked, but never cached.
        self.check(previous)
        self.check(previous)
        self.assertEqual(len(self.checked), 4)

    def test_other_objects_are_checked_on_every_call(self):
        view = {**self.plan['execution_plan'], 'battery_execution': None}
        self.check(view)
        self.check(view)
        self.check(deepcopy(self.plan))
        self.assertEqual(len(self.checked), 3)

    def test_status_matches_uncached_validation_throughout_the_plan_lifetime(self):
        issued, expiry = instant(self.plan['issued_at']), instant(self.plan['valid_until'])
        moments = (issued - timedelta(minutes=10), issued - timedelta(minutes=5), issued, self.now,
                   expiry - timedelta(seconds=1), expiry, expiry + timedelta(hours=6))
        for invalid in (False, True):
            if invalid:
                self.plan['execution_plan']['plans']['priority']['slots'][0]['binding'] = 'yes'
                self.current = self.plan = deepcopy(self.plan)
            for _ in range(2):
                for at in moments:
                    for plan in (self.plan, self.plan['execution_plan']):
                        with self.subTest(invalid=invalid, at=at, branch=plan.get('schema_version')):
                            self.assertEqual(operational_status(plan, 'live', [], at, validate=self.cache),
                                             operational_status(plan, 'live', [], at))


if __name__ == '__main__':
    unittest.main()
