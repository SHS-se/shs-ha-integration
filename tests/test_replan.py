"""Answering a replan the household asked for on the website.

The website used to rebuild the plan from the snapshot stored beside it. That
snapshot is only replaced when this integration pushes one, so for most of every
quarter it was already older than the planner's fifteen-minute freshness limit
and the button failed with `captured_at must describe a fresh snapshot`. The
house is the only thing that can supply a fresh measurement, so it is asked.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

MODULE_ROOT = Path(__file__).parents[1] / "custom_components" / "shs_energy"
sys.path.insert(0, str(MODULE_ROOT))

from api_contract import (  # noqa: E402
    API_VERSION,
    ApiContractError,
    MAX_REPLAN_ERROR_CHARS,
    validate_server_contract,
)

API = (MODULE_ROOT / "api.py").read_text(encoding="utf-8")
COORDINATOR = (MODULE_ROOT / "coordinator.py").read_text(encoding="utf-8")
INIT = (MODULE_ROOT / "__init__.py").read_text(encoding="utf-8")
CONST = (MODULE_ROOT / "const.py").read_text(encoding="utf-8")

REQUEST_ID = "2f1c0c74-9d31-4f0e-9a45-9c6f2f5f0a11"


def status(**overrides):
    return {
        "api_version": API_VERSION,
        "supported_snapshot_schema_versions": [5, 6, 7, 8],
        "supported_plan_schema_versions": [5, 6, 7, 8],
        "minimum_snapshot_schema_version": 5,
        "minimum_plan_schema_version": 5,
        "latest_plan_request_id": None,
        **overrides,
    }


class ReplanContractTests(unittest.TestCase):
    def test_a_pending_request_is_a_readable_part_of_the_contract(self) -> None:
        validate_server_contract(status(pending_replan_request_id=REQUEST_ID))
        validate_server_contract(status(pending_replan_request_id=None))

    def test_a_server_without_replan_requests_still_plans(self) -> None:
        # The field is absent on a server older than this contract. Refusing it
        # would stop a house being planned at all over a button it cannot offer.
        validate_server_contract(status())

    def test_an_unusable_request_id_is_refused_rather_than_echoed(self) -> None:
        # It is sent back to complete the request, so an id that cannot be sent
        # back is worse than none: the house would answer into nothing.
        for unusable in (12, "", [REQUEST_ID]):
            with self.assertRaisesRegex(ApiContractError, "replan request"):
                validate_server_contract(
                    status(pending_replan_request_id=unusable)
                )

    def test_a_failure_report_fits_what_the_server_accepts(self) -> None:
        self.assertEqual(MAX_REPLAN_ERROR_CHARS, 1000)
        self.assertIn("error[:MAX_REPLAN_ERROR_CHARS]", API)


class ReplanWiringTests(unittest.TestCase):
    """Home Assistant is not installed in CI, so this tier is read as text."""

    def test_a_request_is_picked_up_on_its_own_poll(self) -> None:
        # The hourly status poll also refreshes the tariff catalogue and
        # supplier prices, so it is the wrong loop to shorten for this.
        self.assertIn("REPLAN_POLL_INTERVAL_MINUTES", CONST)
        self.assertIn("coordinator.async_replan_poll", INIT)
        self.assertIn(
            "timedelta(minutes=REPLAN_POLL_INTERVAL_MINUTES)", INIT
        )

    def test_the_request_travels_with_the_snapshot_that_answers_it(self) -> None:
        # Only a generated plan settles a request, and only a snapshot produces
        # one, so the id is attached inside the snapshot branch.
        push = API[API.index("async def push_optimisation") :]
        snapshot_branch = push[push.index('body["snapshot"] = snapshot') :]
        self.assertIn(
            'body["replan_request_id"] = replan_request_id', snapshot_branch
        )
        self.assertIn("replan_request_id=replan_request_id", COORDINATOR)

    def test_a_house_that_cannot_plan_says_so_instead_of_going_quiet(self) -> None:
        poll = COORDINATOR[
            COORDINATOR.index("async def async_replan_poll") :
            COORDINATOR.index("async def _prepared_device_inventory")
        ]
        self.assertIn("force_plan=True, replan_request_id=requested", poll)
        self.assertIn("if self.last_optimisation_error is not None", poll)
        self.assertIn("_report_replan_failure", poll)
        # Planning switched off is not a fault and will never resolve itself,
        # so it is reported rather than left for the website to time out.
        self.assertIn("mode != PLANNING_MODE_LIVE", poll)

    def test_one_request_is_answered_once(self) -> None:
        poll = COORDINATOR[COORDINATOR.index("async def async_replan_poll") :]
        self.assertIn("requested == self._answered_replan_request_id", poll)


if __name__ == "__main__":
    unittest.main()
