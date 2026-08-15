"""Every name a module uses must actually exist.

`_tariff_calculations` referenced `time.min` for months without importing
`time`. Nothing caught it: the module compiles, and the only code path that
reaches those two lines is a scheduled push that swallowed the NameError into
an unretrieved task. Home Assistant modules cannot be imported in CI, so this
resolves names statically instead — cheap, and it covers the whole package
rather than only the pure tier.
"""

from __future__ import annotations

import builtins
from pathlib import Path
import symtable
import unittest

PACKAGE = Path(__file__).parents[1] / "custom_components" / "shs_energy"
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}


def unresolved_globals(path: Path) -> list[str]:
    """Return globals a module reads but never defines or imports."""
    source = path.read_text(encoding="utf-8")
    table = symtable.symtable(source, str(path), "exec")
    module_names = {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported()
    }

    missing: list[str] = []

    def walk(scope: symtable.SymbolTable) -> None:
        for symbol in scope.get_symbols():
            name = symbol.get_name()
            if (
                symbol.is_global()
                and symbol.is_referenced()
                and name not in module_names
                and name not in BUILTINS
            ):
                missing.append(f"{scope.get_name()}: {name}")
        for child in scope.get_children():
            walk(child)

    walk(table)
    return sorted(set(missing))


class NameResolutionTests(unittest.TestCase):
    def test_no_module_reads_a_name_it_never_defines(self) -> None:
        for path in sorted(PACKAGE.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(
                    unresolved_globals(path),
                    [],
                    f"{path.name} reads names that do not exist; a missing "
                    "import only fails on the code path that uses it",
                )


if __name__ == "__main__":
    unittest.main()
