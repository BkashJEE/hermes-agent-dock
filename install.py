#!/usr/bin/env python
"""Install Hermes Agent Dock without package-manager lifecycle scripts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

PLUGIN_ID = "hermes-agent-dock"
VERSION = "0.2.1"
ROOT = Path(__file__).resolve().parent
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PLAIN_ROW_RE = re.compile(r"^(not enabled|enabled|disabled)\s+\S+\s+\S+\s+(\S+)\s*$", re.IGNORECASE)


def default_home() -> Path:
    explicit = (os.environ.get("HERMES_HOME") or "").strip()
    if explicit:
        return Path(explicit)
    if os.name == "nt":
        local = (os.environ.get("LOCALAPPDATA") or "").strip()
        if local:
            return Path(local) / "hermes"
    return Path.home() / ".hermes"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup_existing(home: Path, destinations: list[Path]) -> Path | None:
    existing = [path for path in destinations if path.exists()]
    if not existing:
        return None
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    backup = home / "backups" / PLUGIN_ID / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for path in existing:
        label = "desktop" if path == home / "desktop-plugins" / PLUGIN_ID else "backend"
        shutil.copytree(path, backup / label)
    return backup


def restore_installation(backup: Path | None, desktop_dir: Path, backend_dir: Path) -> None:
    """Restore the exact pre-install file state after a failed replacement."""
    for path in (desktop_dir, backend_dir):
        if path.exists():
            shutil.rmtree(path)
    if backup is None:
        return
    for label, destination in (("desktop", desktop_dir), ("backend", backend_dir)):
        source = backup / label
        if source.exists():
            shutil.copytree(source, destination)


def run_hermes(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("hermes")
    if not executable:
        raise RuntimeError("Hermes CLI was not found on PATH")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    return subprocess.run([executable, *args], env=env, text=True, capture_output=True, check=False)


def plugin_status(listing: str) -> str | None:
    """Return the exact Hermes status for this plugin's inventory row."""
    for raw_line in listing.splitlines():
        row = PLAIN_ROW_RE.fullmatch(ANSI_RE.sub("", raw_line).strip())
        if not row or row.group(2).lower() != PLUGIN_ID:
            continue
        return row.group(1).lower()
    return None


def install(home: Path, copy_only: bool = False) -> dict[str, object]:
    source_plugin = ROOT / "plugin.js"
    source_backend = ROOT / "backend"
    if not source_plugin.is_file() or not (source_backend / "plugin.yaml").is_file():
        raise RuntimeError("Run install.py from a complete Hermes Agent Dock checkout")

    desktop_dir = home / "desktop-plugins" / PLUGIN_ID
    backend_dir = home / "plugins" / PLUGIN_ID
    home.mkdir(parents=True, exist_ok=True)
    backup = backup_existing(home, [desktop_dir, backend_dir])
    try:
        if desktop_dir.exists():
            shutil.rmtree(desktop_dir)
        desktop_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_plugin, desktop_dir / "plugin.js")
        if backend_dir.exists():
            shutil.rmtree(backend_dir)
        shutil.copytree(
            source_backend,
            backend_dir,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.pyd"),
        )

        manifest = {
            "plugin": PLUGIN_ID,
            "version": VERSION,
            "installed_at": int(time.time()),
            "source": str(ROOT),
            "backup": str(backup) if backup else None,
            "files": {
                "desktop/plugin.js": sha256(desktop_dir / "plugin.js"),
                "backend/plugin.yaml": sha256(backend_dir / "plugin.yaml"),
                "backend/dashboard/manifest.json": sha256(backend_dir / "dashboard" / "manifest.json"),
                "backend/dashboard/plugin_api.py": sha256(backend_dir / "dashboard" / "plugin_api.py"),
                "backend/dashboard/dock_runner.py": sha256(backend_dir / "dashboard" / "dock_runner.py"),
            },
        }
        enable_result = None
        if not copy_only:
            result = run_hermes(home, "plugins", "enable", PLUGIN_ID)
            enable_result = {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
            # Windows may report an atomic replace failure after the state changed.
            check = run_hermes(home, "plugins", "list", "--plain", "--no-bundled")
            listing = f"{check.stdout}\n{check.stderr}"
            if check.returncode != 0 or plugin_status(listing) != "enabled":
                raise RuntimeError("Hermes did not confirm backend enablement")

        manifest["enable_result"] = enable_result
        (backend_dir / "install-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return manifest
    except Exception as exc:
        try:
            restore_installation(backup, desktop_dir, backend_dir)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"Installation failed and automatic rollback also failed; preserved backup: {backup}"
            ) from rollback_exc
        raise RuntimeError(f"Installation failed; previous files restored: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Hermes Agent Dock")
    parser.add_argument("--home", type=Path, default=default_home(), help="Hermes home (defaults to HERMES_HOME/platform location)")
    parser.add_argument("--copy-only", action="store_true", help="Copy files without changing plugins.enabled (for isolated tests)")
    args = parser.parse_args()
    try:
        result = install(args.home.resolve(), copy_only=args.copy_only)
    except Exception as exc:
        print(f"INSTALL BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print("Restart Hermes Desktop once to mount the Python backend; plugin.js itself hot-reloads.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
