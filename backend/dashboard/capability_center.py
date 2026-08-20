"""Profile-scoped Capability Center helper.

This file runs in a child process with HERMES_HOME pointing at exactly one
installed profile. Its stdout is a deliberately small public projection.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

TARGETS = {"host": "local", "docker": "docker"}
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$")
PRIVILEGED_DOCKER_FIELDS = {
    "mounts": ("docker_volumes", "docker_mount_cwd_to_workspace"),
    "forwarded_environment": ("docker_forward_env", "docker_env"),
    "extra_arguments": ("docker_extra_args",),
    "persistent_container": ("container_persistent",),
}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any, *, limit: int = 200) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({str(item).strip()[:120] for item in value if str(item).strip()})[:limit]


def _skills(home: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    disabled = {name.lower() for name in _strings(_dict(config.get("skills")).get("disabled"))}
    rows: dict[str, dict[str, Any]] = {}
    root = home / "skills"
    if root.is_dir():
        for marker in root.rglob("SKILL.md"):
            name = marker.parent.name.strip()[:120]
            if name and not name.startswith("."):
                rows[name.lower()] = {"name": name, "enabled": name.lower() not in disabled}
    return [rows[key] for key in sorted(rows)][:200]


def _toolsets(config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from hermes_cli.tools_config import (
            _get_effective_configurable_toolsets,
            _get_platform_tools,
            _toolset_allowed_for_platform,
        )

        enabled = set(_get_platform_tools(config, "cli", include_default_mcp_servers=False))
        rows = []
        for key, label, _description in _get_effective_configurable_toolsets():
            if _toolset_allowed_for_platform(key, "cli"):
                rows.append({"name": str(key)[:120], "label": str(label)[:120], "enabled": key in enabled})
        return sorted(rows, key=lambda row: row["name"])[:200]
    except Exception:
        enabled = _strings(_dict(config.get("tools")).get("enabled_toolsets"))
        return [{"name": name, "label": name, "enabled": True} for name in enabled]


def _mcp_servers(config: dict[str, Any]) -> list[dict[str, Any]]:
    section = config.get("mcp_servers")
    if not isinstance(section, dict):
        section = _dict(_dict(config.get("mcp")).get("servers"))
    rows = []
    for raw_name, raw_value in section.items():
        name = str(raw_name).strip()[:120]
        if not name or name.startswith("_"):
            continue
        value = _dict(raw_value)
        rows.append({"name": name, "enabled": value.get("enabled") is not False})
    return sorted(rows, key=lambda row: row["name"].lower())[:200]


def _credential_configured(config: dict[str, Any], provider: str) -> bool:
    model = _dict(config.get("model"))
    if model.get("api_key"):
        return True
    normalized = provider.upper().replace("-", "_").replace(".", "_")
    candidates = {
        f"{normalized}_API_KEY",
        "OPENAI_API_KEY" if provider in {"openai", "openai-codex"} else "",
        "ANTHROPIC_API_KEY" if provider == "anthropic" else "",
        "OPENROUTER_API_KEY" if provider == "openrouter" else "",
    }
    return any(name and bool(os.environ.get(name)) for name in candidates)


def _docker_privilege_flags(terminal: dict[str, Any]) -> dict[str, bool]:
    return {
        category: any(bool(terminal.get(field)) for field in fields)
        for category, fields in PRIVILEGED_DOCKER_FIELDS.items()
    }


def snapshot(home: Path, config: dict[str, Any]) -> dict[str, Any]:
    model = _dict(config.get("model"))
    terminal = _dict(config.get("terminal"))
    approvals = _dict(config.get("approvals"))
    provider = str(model.get("provider") or "auto")[:120]
    backend = str(terminal.get("backend") or terminal.get("env_type") or "local").lower()
    target = "docker" if backend == "docker" else "host" if backend == "local" else "other"
    image = str(terminal.get("docker_image") or "")[:200]
    limits = {
        "cpu": str(terminal.get("container_cpu") or "")[:32] or None,
        "memory": str(terminal.get("container_memory") or "")[:32] or None,
        "disk": str(terminal.get("container_disk") or "")[:32] or None,
        "timeout_seconds": terminal.get("timeout") if isinstance(terminal.get("timeout"), (int, float)) else None,
    }
    docker_privileges = _docker_privilege_flags(terminal)
    return {
        "model": {"provider": provider, "name": str(model.get("default") or model.get("model") or "")[:200]},
        "credentials": {"provider_configured": _credential_configured(config, provider)},
        "skills": _skills(home, config),
        "toolsets": _toolsets(config),
        "mcp_servers": _mcp_servers(config),
        "approvals": {
            "destructive_confirm": approvals.get("destructive_slash_confirm", True) is not False,
            "mcp_reload_confirm": approvals.get("mcp_reload_confirm", True) is not False,
        },
        "execution": {
            "target": target,
            "backend": backend[:40],
            "docker_available": shutil.which("docker") is not None,
            "docker_image": image or None,
            "limits": limits,
            "privileged_settings": docker_privileges,
            "safe_to_enable_docker": not any(docker_privileges.values()),
            "editable_targets": ["host", "docker"],
            "takes_effect": "new_sessions",
        },
    }


def set_target(config: dict[str, Any], target: str, image: str | None) -> dict[str, Any]:
    if target not in TARGETS:
        raise ValueError("Execution target must be host or docker")
    if image is not None:
        image = image.strip()
        if not IMAGE_RE.fullmatch(image):
            raise ValueError("Docker image is invalid")
    terminal = _dict(config.get("terminal")).copy()
    current_backend = str(terminal.get("backend") or terminal.get("env_type") or "local").lower()
    if target == "docker" and current_backend != "docker" and any(_docker_privilege_flags(terminal).values()):
        raise ValueError("Docker has privileged settings; review them in Hermes before enabling this target")
    terminal["backend"] = TARGETS[target]
    terminal["env_type"] = TARGETS[target]
    if target == "docker" and image:
        terminal["docker_image"] = image
    config["terminal"] = terminal
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-target", choices=sorted(TARGETS))
    parser.add_argument("--image")
    args = parser.parse_args()
    from hermes_cli.config import load_config, save_config

    home = Path(os.environ["HERMES_HOME"]).resolve()
    config = load_config() or {}
    if args.set_target:
        save_config(set_target(config, args.set_target, args.image))
        config = load_config() or {}
    print(json.dumps(snapshot(home, config), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
