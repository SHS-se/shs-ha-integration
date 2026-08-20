"""Executable checks for the generated private API contract surface."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

MODULE_ROOT = Path(__file__).parents[1] / "custom_components" / "shs_energy"
sys.path.insert(0, str(MODULE_ROOT))

from api_contract import (  # noqa: E402
    API_VERSION,
    ApiContractError,
    INTEGRATION_VERSION,
    validate_server_contract,
)


class ApiContractTests(unittest.TestCase):
    def status(self, **overrides):
        return {
            "api_version": API_VERSION,
            "supported_snapshot_schema_versions": [5, 6],
            "supported_plan_schema_versions": [5, 6],
            "minimum_snapshot_schema_version": 5,
            "minimum_plan_schema_version": 5,
            "latest_plan_request_id": None,
            **overrides,
        }

    def test_matching_server_contract_is_accepted(self) -> None:
        validate_server_contract(self.status())

    def test_no_mutual_plan_schema_fails_before_planning(self) -> None:
        with self.assertRaisesRegex(ApiContractError, "cannot return plan schema"):
            validate_server_contract(
                self.status(supported_plan_schema_versions=[5])
            )

    def test_api_version_mismatch_is_explicit(self) -> None:
        with self.assertRaisesRegex(ApiContractError, "API version"):
            validate_server_contract(self.status(api_version=2))

    def test_generated_integration_version_matches_manifest(self) -> None:
        manifest = json.loads(
            (MODULE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(INTEGRATION_VERSION, manifest["version"])


if __name__ == "__main__":
    unittest.main()
