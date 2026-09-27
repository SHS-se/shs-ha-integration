"""Explicit, journalled companion installation; never touches HA data stores."""
import hashlib
import json
import os
from pathlib import Path
import shutil


def hashes(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Companion paths must not contain symbolic links")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_journal(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install(bundle, homeassistant, data):
    """Resume forward after interruption. Unknown local edits stop installation."""
    manifest = json.loads((bundle / "bundle.json").read_text())
    source = bundle / "shs_energy"
    expected = manifest["files"]
    if hashes(source) != expected:
        raise ValueError("Bundled companion checksum verification failed")
    parent = homeassistant / "custom_components"
    if homeassistant.is_symlink() or parent.is_symlink():
        raise ValueError("Home Assistant component directory is a symbolic link")
    parent.mkdir(exist_ok=True)
    target = parent / "shs_energy"
    # HA discovers every directory under custom_components, including dotfiles.
    # Keep both manifests outside that tree, on the same filesystem for rename.
    workspace = homeassistant / ".shs-companion-install"
    stage = workspace / "stage"
    backup = workspace / "backup"
    journal_path = data / "companion-install.json"
    if any(p.is_symlink() for p in (target, workspace, stage, backup, journal_path)):
        raise ValueError("Companion installation paths must not be symbolic links")
    workspace.mkdir(exist_ok=True)
    current = hashes(target) if target.exists() else None
    if current == expected:
        return {"state": "installed", "version": manifest["integration_version"],
                "message": "Matching companion files are installed. Restart Home Assistant Core if the loaded version differs."}
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
    if journal and journal["version"] != manifest["integration_version"]:
        raise ValueError("An earlier installation journal needs review before upgrading")
    if current is not None and current not in manifest["replaceable"]:
        raise ValueError("The installed integration differs from a verified release. Update it through HACS or review local changes before installing the companion.")
    if backup.exists() and not journal:
        raise ValueError("An existing companion backup needs review")
    if not target.exists() and backup.exists() and (not journal or hashes(backup) != journal["previous"]):
        raise ValueError("Interrupted installation backup does not match its journal")
    if not stage.exists():
        shutil.copytree(source, stage)
    if hashes(stage) != expected:
        raise ValueError("Staged companion is incomplete; review .shs-companion-install/stage before retrying")
    for path in stage.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for path in sorted((p for p in stage.rglob("*") if p.is_dir()), reverse=True):
        sync_directory(path)
    sync_directory(stage)
    if not journal:
        journal = {"version": manifest["integration_version"], "previous": current}
        write_journal(journal_path, journal)
    if target.exists():
        if backup.exists():
            raise ValueError("Both previous and active integration directories exist; review before proceeding")
        target.rename(backup)
        sync_directory(parent)
        sync_directory(workspace)
    stage.rename(target)
    sync_directory(parent)
    sync_directory(workspace)
    return {"state": "installed", "version": manifest["integration_version"],
            "message": "Companion installed. Restart Home Assistant Core to load it. Existing configuration and history were preserved."}
