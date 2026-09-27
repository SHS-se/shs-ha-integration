"""Check the files Supervisor discovers and the exact bundled branding/version."""
import json
from pathlib import Path
import yaml

root = Path(__file__).resolve().parents[1]
config = yaml.safe_load((root / "apps/shs_energy/config.yaml").read_text())
package = json.loads((root / "web/package.json").read_text())
assert config["version"] == package["version"]
assert set(config["arch"]) == {"aarch64", "amd64"}
assert config["ingress"] and config["panel_admin"] and config["homeassistant_api"]
assert config["options"] == {"install_companion": False}
assert "ports" not in config  # Only authenticated Ingress exposes the UI.
assert config["image"] == "ghcr.io/shs-se/shs-energy-app"
brand = (root / "custom_components/shs_energy/brand/icon@2x.png").read_bytes()
for path in ("apps/shs_energy/icon.png", "apps/shs_energy/logo.png", "web/public/shs.png"):
    assert (root / path).read_bytes() == brand, path
assert yaml.safe_load((root / "repository.yaml").read_text())["url"] == "https://github.com/SHS-se/shs-ha-integration"
print("App metadata, version and existing SHS branding verified")
