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
                self.assertIn("fix={", body)

    def test_the_frontend_renders_and_routes_attention(self) -> None:
        self.assertIn("_renderAttention()", FRONTEND)
        self.assertIn("attention-item", FRONTEND)
        # Steering: a tab badge, and a button that switches to the right tab.
        self.assertIn("tab-badge", FRONTEND)
        self.assertIn("_attentionForTab(", FRONTEND)
        self.assertIn('data-action="tab" data-tab="${this._escape(fix.tab)}"', FRONTEND)

    def test_a_readiness_card_cannot_be_greener_than_its_repairs(self) -> None:
        """Four green badges beside an open warning is how green stops meaning anything."""
        self.assertIn("ATTENTION_BY_CARD", FRONTEND)
        card = FRONTEND[FRONTEND.index("_readinessCard(title") :]
        card = card[: card.index("_renderOverview")]
        self.assertIn("ATTENTION_BY_CARD[title]", card)

    def test_every_repair_is_owned_by_a_readiness_card(self) -> None:
        raised = {
            line.split("=", 1)[1].strip().strip('"')
            for line in CONST.splitlines()
            if line.startswith("ISSUE_")
        }
        owned = set()
        block = FRONTEND[FRONTEND.index("ATTENTION_BY_CARD = {") :]
        block = block[: block.index("};")]
        for key in raised:
            if f'"{key}"' in block:
                owned.add(key)
        self.assertEqual(
            sorted(raised - owned),
            [],
            "a repair no card owns can leave that card green while it is open",
        )

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
