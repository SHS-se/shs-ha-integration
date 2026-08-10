"""Tests for non-secret backend diagnostics."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from const import backend_attributes  # noqa: E402


class BackendAttributesTests(unittest.TestCase):
    def test_identifies_production_and_test_projects(self) -> None:
        self.assertEqual(
            backend_attributes(
                "https://oosxndduqzhvrorgogaw.supabase.co/functions/v1"
            ),
            {
                "backend_environment": "production",
                "backend_host": "oosxndduqzhvrorgogaw.supabase.co",
            },
        )
        self.assertEqual(
            backend_attributes(
                "https://vxqpgbzseckgceopitpm.supabase.co/functions/v1"
            )["backend_environment"],
            "test",
        )

    def test_custom_server_does_not_expose_credentials(self) -> None:
        self.assertEqual(
            backend_attributes("https://secret@example.test/functions/v1"),
            {"backend_environment": "custom", "backend_host": "example.test"},
        )


if __name__ == "__main__":
    unittest.main()
