#!/usr/bin/env python
"""Reversibly uninstall Hermes Agent Dock."""
from __future__ import annotations

import argparse
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
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PLAIN_ROW_RE = re.compile(r"^(not enabled|enabled|disabled)\s+\S+\s+\S+\s+(\S+)\s*$", re.IGNORECASE)


def plugin_status(listing: str) -> str | None:
    """Return the exact Hermes status for this plugin's inventory row."""
    for raw_line in listing.splitlines():
        row = PLAIN_ROW_RE.fullmatch(ANSI_RE.sub("", raw_line).strip())
        if not row or row.group(2).lower() != PLUGIN_ID:
            continue
        return row.group(1).lower()
    return None


def default_home() -> Path:
    explicit = (os.environ.get("HERMES_HOME") or "").strip()
    if explicit:
        return Path(explicit)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return Path.home() / ".hermes"


def disable(home: Path) -> dict[str, object]:
    executable = shutil.which("hermes")
    if not executable:
        raise RuntimeError("Hermes CLI was not found on PATH; plugin disablement cannot be verified")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [executable, "plugins", "disable", PLUGIN_ID],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    check = subprocess.run(
        [executable, "plugins", "list", "--plain", "--no-bundled"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    listing = f"{check.stdout}\n{check.stderr}"
    status = plugin_status(listing)
    confirmed_disabled = check.returncode == 0 and status in {"not enabled", "disabled"}
    if not confirmed_disabled:
        raise RuntimeError(
            "Hermes did not confirm plugin disablement; installed code was left untouched"
        )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "confirmed_disabled": True,
    }


def uninstall(home: Path, copy_only: bool = False, purge: bool = False) -> dict[str, object]:
    desktop = home / "desktop-plugins" / PLUGIN_ID
    backend = home / "plugins" / PLUGIN_ID
    disable_result = None if copy_only else disable(home)
    moved: list[str] = []
    backup = None
    existing = [path for path in (desktop, backend) if path.exists()]
    if existing and not purge:
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        backup = home / "backups" / PLUGIN_ID / f"uninstall-{stamp}"
        backup.mkdir(parents=True, exist_ok=False)
        for path in existing:
            label = "desktop" if path == desktop else "backend"
            shutil.copytree(path, backup / label)
    try:
        for path in existing:
            label = "desktop" if path == desktop else "backend"
            shutil.rmtree(path)
            moved.append(label)
    except Exception as exc:
        if purge or backup is None:
            raise
        try:
            for path in existing:
                label = "desktop" if path == desktop else "backend"
                if path.exists():
                    shutil.rmtree(path)
                shutil.copytree(backup / label, path)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"Uninstall failed and automatic rollback also failed; preserved backup: {backup}"
            ) from rollback_exc
        raise RuntimeError("Uninstall failed; installed files restored") from exc
    return {
        "plugin": PLUGIN_ID,
        "disabled": disable_result,
        "removed_paths": len(existing),
        "purged": purge,
        "backup": str(backup) if backup else None,
        "moved": moved,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Uninstall Hermes Agent Dock")
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("--copy-only", action="store_true", help="Skip plugins.disabled mutation (for isolated tests)")
    parser.add_argument("--purge", action="store_true", help="Delete code instead of moving it to a timestamped backup")
    args = parser.parse_args()
    try:
        result = uninstall(args.home.resolve(), copy_only=args.copy_only, purge=args.purge)
    except Exception as exc:
        print(f"UNINSTALL BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print("Restart Hermes Desktop once if the backend had already been mounted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
