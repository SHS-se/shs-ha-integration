"""The pure tier must stay importable without Home Assistant.

CI installs no Home Assistant, so any module that reaches for it can never be
executed by a test — only read as text and asserted against with brittle string
matches. That is how the planner's service construction went years without
behavioural coverage. This guard keeps the boundary from eroding again: logic
belongs in the pure tier, and the Home Assistant modules stay thin enough that
losing coverage of them costs little.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
import sys
import unittest

PACKAGE = Path(__file__).parents[1] / "custom_components" / "shs_energy"
sys.path.insert(0, str(PACKAGE))

# Every module holding decisions rather than plumbing.
PURE_MODULES = (
    "const",
    "device_controls",
    "optimisation",
    "planning",
    "supplier",
    "tariff",
    "thermal",
)

# Thin by design: they wire Home Assistant to the modules above.
HOME_ASSISTANT_MODULES = (
    "__init__",
    "config_flow",
    "config_panel",
    "configuration",
    "coordinator",
    "sensor",
)


class ModuleBoundaryTests(unittest.TestCase):
    def test_every_pure_module_imports_without_home_assistant(self) -> None:
        self.assertNotIn(
            "homeassistant",
            sys.modules,
            "the guard is meaningless if Home Assistant is importable here",
        )
        for name in PURE_MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_no_pure_module_reaches_for_home_assistant(self) -> None:
        for name in PURE_MODULES:
            with self.subTest(module=name):
                source = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
                offenders = [
                    f"line {number}: {line.strip()}"
                    for number, line in enumerate(source.splitlines(), start=1)
                    if "homeassistant" in line
                ]
                self.assertEqual(
                    offenders,
                    [],
                    f"{name}.py would become untestable; keep the decision here "
                    "and take Home Assistant's values as arguments",
                )

    def test_no_function_reassigns_a_parameter_it_calls(self) -> None:
        """An injected reader must not be rebound to its own result.

        Doing so works for the first item of a loop and then calls that result
        on the second, which is how a power reader became a float mid-sweep.
        Narrowing a plain value parameter is a different, harmless thing, so
        only parameters actually used as functions are guarded.
        """
        for path in sorted(PACKAGE.glob("*.py")):
            for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                parameters = {
                    arg.arg
                    for arg in (*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs)
                }
                called = {
                    node.func.id
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in parameters
                }
                rebound = sorted({
                    node.id
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Store)
                    and node.id in called
                })
                with self.subTest(module=path.name, function=fn.name):
                    self.assertEqual(
                        rebound,
                        [],
                        f"{path.name}:{fn.name} reassigns a parameter it also "
                        "calls; give the result its own name",
                    )

    def test_the_module_lists_still_describe_the_package(self) -> None:
        listed = set(PURE_MODULES) | set(HOME_ASSISTANT_MODULES) | {"api"}
        actual = {path.stem for path in PACKAGE.glob("*.py")}
        self.assertEqual(
            actual - listed,
            set(),
            "a new module must be classified as pure or Home Assistant-facing",
        )


if __name__ == "__main__":
    unittest.main()
