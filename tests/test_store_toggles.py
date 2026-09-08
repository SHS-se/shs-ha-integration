"""Each store the customer can switch off must disappear from every surface.

A store that is off has to leave the panel, the services, the capability flags
and the unplanned-service report together. Those are computed in four places
from three different inputs, so a half-finished change here does not fail — it
produces a snapshot that contradicts itself, which is the exact failure
`planned_paths` was introduced to stop.

The behaviour lives in `planning.disabled_store_paths` and is tested against in
`test_planning.py`. What is guarded here is the wiring the suite cannot import:
the panel is a Home Assistant module and its frontend is a browser custom
element.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

PACKAGE = Path(__file__).parents[1] / "custom_components" / "shs_energy"
sys.path.insert(0, str(PACKAGE))

CONFIG_PANEL = (PACKAGE / "configuration_fields.py").read_text(encoding="utf-8")
CONFIGURATION = (PACKAGE / "configuration_schema.py").read_text(encoding="utf-8")
COORDINATOR = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
FRONTEND = (
    PACKAGE / "frontend" / "shs-energy-config-panel.js"
).read_text(encoding="utf-8")

import const  # noqa: E402

# Keyed by the constant's *name*, because what is guarded here is source
# wiring: the panel refers to `c.OPT_POOL_ENABLED`, never to "pool_enabled".
STORE_SECTIONS = {
    "house_battery": "OPT_BATTERY_ENABLED",
    "pool": "OPT_POOL_ENABLED",
    "ev": "OPT_EV_ENABLED",
}


class StoreToggleWiringTests(unittest.TestCase):
    def _section(self, section_id: str) -> str:
        start = CONFIG_PANEL.index(f'"id": "{section_id}",')
        following = CONFIG_PANEL.find('"id": "', start + 1)
        return CONFIG_PANEL[start:] if following < 0 else CONFIG_PANEL[start:following]

    def test_every_store_section_carries_its_own_switch(self) -> None:
        for section_id, name in STORE_SECTIONS.items():
            with self.subTest(section=section_id):
                body = self._section(section_id)
                # Carried beside the title, never among the settings it
                # governs, so no renderer can draw it into the grid.
                self.assertIn(f'"toggle": _field(\n                c.{name},', body)
                fields = body[body.index('"fields": ['):]
                self.assertNotIn(name, fields)

    def test_a_switch_is_off_by_nothing_and_on_by_default(self) -> None:
        """An installation predating these keys must plan exactly as before."""
        for name in STORE_SECTIONS.values():
            with self.subTest(option=name):
                self.assertRegex(CONFIGURATION, rf"{name}: True,")
                # And the value is a real key rather than a typo that would
                # read as absent forever.
                self.assertIsInstance(getattr(const, name), str)

    def test_the_frontend_folds_a_section_it_is_told_is_off(self) -> None:
        renderer = FRONTEND[FRONTEND.index("_renderSection(section) {"):]
        renderer = renderer[: renderer.index("\n  _renderSections(")]
        self.assertIn("const gate = section.toggle;", renderer)
        # The switch sits in the heading, and the fields are unreachable when
        # off: an early return rather than a conditional inside the markup,
        # because the latter is what let a half-applied version render both.
        self.assertIn("section-switch", renderer)
        off, live = renderer.split("if (!on) {", 1)
        folded, expanded = live.split("return `<section class=\"card form-card\">", 1)
        self.assertNotIn("field-grid", folded)
        self.assertIn("field-grid", expanded)
        self.assertIn("section.fields", expanded)

    def test_the_switch_is_still_reachable_when_it_is_not_a_field(self) -> None:
        """`_onChange` resolves a field by key from the sections, so a switch
        held outside `fields` has to be added back there or it does nothing."""
        lookup = FRONTEND[FRONTEND.index("_fieldFromElement(element) {"):]
        lookup = lookup[: lookup.index("\n  _onChange(")]
        self.assertIn("section.toggle ? [section.toggle] : []", lookup)

    def test_the_save_path_persists_a_switch_that_is_not_a_field(self) -> None:
        """Same gap on the Python side: validation walks a section's fields."""
        self.assertIn("for field in section_fields(section)", CONFIGURATION)
        self.assertIn("def section_fields(", CONFIG_PANEL)

    def test_the_snapshot_drops_what_the_panel_hid(self) -> None:
        """The four surfaces, each named where it is decided."""
        self.assertIn("switched_off = disabled_store_paths(options)", COORDINATOR)
        self.assertIn('"pool" not in switched_off', COORDINATOR)
        self.assertIn('"ev" not in switched_off', COORDINATOR)
        # The battery has no planning path, so it is gated where it is read.
        self.assertRegex(
            COORDINATOR,
            r"options\.get\(OPT_BATTERY_ENABLED, True\)",
        )

    def test_hiding_a_section_does_not_erase_what_it_held(self) -> None:
        """Switching a store off and on again must return the settings.

        The renderer stops drawing the fields, so anything that saved only what
        is on screen would quietly wipe an entity id the customer spent time
        choosing. `_save` selects editable keys from every section, including
        collapsed sections. The frontend test also executes that save path.
        """
        saver = FRONTEND[FRONTEND.index("async _save() {"):]
        saver = saver[: saver.index("\n  async _saveDevice(")]
        self.assertIn("this._clone(this._draft)", saver)
        self.assertIn("this._data.sections.flatMap", saver)
        self.assertNotIn(".filter(", saver)

    def test_no_store_toggle_is_left_unrouted(self) -> None:
        """A constant nobody consults is worse than no constant at all."""
        for name in STORE_SECTIONS.values():
            with self.subTest(option=name):
                consumers = [
                    label
                    for label, source in (
                        ("panel", CONFIG_PANEL),
                        ("defaults", CONFIGURATION),
                    )
                    if re.search(rf"\b{name}\b", source)
                ]
                self.assertEqual(
                    consumers,
                    ["panel", "defaults"],
                    f"{name} is not wired end to end",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
