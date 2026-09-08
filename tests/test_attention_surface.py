"""Every repair must reach the panel, and name somewhere to go.

Home Assistant's repair list and this integration's configuration panel were
two independent renderings of the same installation, so the panel could show
four green "Ready" badges while a warning sat in the repairs list about the
very thing a card described. Worse, a warning could name a problem without
naming a setting: "set the EV charging meter to variable-power control on the
website" described no field a customer had ever been shown.

These are source-level guards because the panel is a Home Assistant module and
its frontend is a browser custom element; neither can be imported here. They
protect the wiring rather than the rendering, which is the part that regresses
silently.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

PACKAGE = Path(__file__).parents[1] / "custom_components" / "shs_energy"
CONFIG_PANEL = (PACKAGE / "config_panel.py").read_text(encoding="utf-8")
COORDINATOR = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
FRONTEND = (
    PACKAGE / "frontend" / "shs-energy-config-panel.js"
).read_text(encoding="utf-8")
CONST = (PACKAGE / "const.py").read_text(encoding="utf-8")


class AttentionSurfaceTests(unittest.TestCase):
    def test_the_panel_payload_carries_the_attention_list(self) -> None:
        self.assertIn('"attention": [', CONFIG_PANEL)
        self.assertIn("coordinator.attention_items", CONFIG_PANEL)

    def test_a_website_fix_is_resolved_to_a_link(self) -> None:
        """A path a customer has to assemble themselves is not a fix."""
        self.assertIn("shs_const.website_url(", CONFIG_PANEL)
        self.assertIn('WEBSITE_ORIGIN_BY_ENVIRONMENT', CONST)

    def test_every_attention_item_states_where_its_fix_lives(self) -> None:
        for call in COORDINATOR.split("self._set_attention(")[1:]:
            body = call[: call.index("\n        )")]
            with self.subTest(item=body.split(",", 1)[0].strip()):
                # Either a literal, or one resolved from a table that this
                # file separately proves covers every case it can be asked for.
                self.assertRegex(body, r"fix=(\{|dict\(fix\))")


    def test_a_banner_exists_for_every_kind_of_gap(self) -> None:
        """A remedy with no banner would fall back to the wrong instruction."""
        table = COORDINATOR[COORDINATOR.index("PLANNING_BANNER_BY_REMEDY") :]
        table = table[: table.index("\n}")]
        for remedy in ("REMEDY_SETTING", "REMEDY_WAITING", "REMEDY_DEFECT"):
            with self.subTest(remedy=remedy):
                self.assertIn(f"{remedy}: (", table)

    def test_only_an_actionable_gap_sends_the_reader_to_a_tab(self) -> None:
        """The reported bug: "Go to Energy inputs" for a shortfall of history.

        A button that names a page promises a field on it. Where no setting can
        answer the gap, the banner has to carry no destination at all.
        """
        table = COORDINATOR[COORDINATOR.index("PLANNING_BANNER_BY_REMEDY") :]
        table = table[: table.index("\n}")]
        setting, rest = table.split("REMEDY_WAITING", 1)
        self.assertIn('"kind": "panel"', setting)
        self.assertNotIn('"kind": "panel"', rest)
        self.assertEqual(rest.count('{"kind": "none"}'), 2)


    def test_a_gap_no_setting_can_answer_is_tagged_at_the_raise(self) -> None:
        """Classification belongs where the reason is known, not in a matcher.

        Deciding this by matching the message text downstream is how the
        base-load shortfall came to be announced as a missing panel field.
        """
        optimisation = (PACKAGE / "optimisation.py").read_text(encoding="utf-8")
        self.assertIn("remedy: str = REMEDY_SETTING", optimisation)
        start = optimisation.index("lacks {minimum_samples} samples")
        raise_call = optimisation[start : optimisation.index("\n        )", start)]
        self.assertIn("remedy=REMEDY_WAITING", raise_call)

    def test_an_attention_item_is_built_from_persisted_state(self) -> None:
        """State that only a device exchange fills is empty on every restart.

        The unplanned-service warning names the meter a customer has to change,
        which it read from an instance attribute set during the device
        exchange. The optimisation push can run first, so the first warning of
        a session announced that no meter was classified for the service at all
        — about a home that has one. The persisted configuration is already an
        argument to the snapshot builder; nothing needed caching.
        """
        call = COORDINATOR[
            COORDINATOR.index("self.optimisation_unplanned_services = unplanned_services(") :
        ]
        call = call[: call.index("\n        )")]
        self.assertIn('stored.get("optimisation_device_configuration"', call)
        self.assertNotIn(
            "self.device_configuration",
            COORDINATOR,
            "read the persisted exchange rather than caching it on the coordinator",
        )


if __name__ == "__main__":
    unittest.main()
