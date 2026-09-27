"""Prepare the exact integration bundled with the app image."""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from shs_app.companion import hashes

destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
source = ROOT / "custom_components/shs_energy"
shutil.copytree(source, destination / "shs_energy", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
manifest = json.loads((source / "manifest.json").read_text())
(destination / "bundle.json").write_text(json.dumps({
    "protocol": 1, "integration_version": manifest["version"], "files": hashes(source),
    "replaceable": [json.loads((ROOT / "app/companion-baseline.json").read_text())],
}, indent=2) + "\n")
