from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_dock_capability_center",
    ROOT / "backend" / "dashboard" / "capability_center.py",
)
capability = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(capability)


class CapabilityCenterTests(unittest.TestCase):
    def test_snapshot_is_profile_scoped_and_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "skills" / "safe-skill").mkdir(parents=True)
            (home / "skills" / "safe-skill" / "SKILL.md").write_text("# Safe", encoding="utf-8")
            config = {
                "model": {"provider": "openai", "default": "gpt-safe", "api_key": "do-not-return"},
                "skills": {"disabled": ["SAFE-SKILL"]},
                "tools": {"enabled_toolsets": ["terminal"]},
                "mcp_servers": {"github": {"enabled": True, "command": "secret-command", "env": {"TOKEN": "secret"}}},
                "approvals": {"destructive_slash_confirm": True, "mcp_reload_confirm": False},
                "terminal": {
                    "backend": "docker",
                    "docker_image": "python:3.13",
                    "docker_volumes": ["private:/workspace"],
                    "docker_forward_env": ["OPENAI_API_KEY"],
                    "container_memory": "2g",
                },
            }
            with patch.object(capability, "_toolsets", return_value=[{"name": "terminal", "label": "Terminal", "enabled": True}]):
                payload = capability.snapshot(home, config)
            serialized = json.dumps(payload)
            self.assertEqual(payload["execution"]["target"], "docker")
            self.assertEqual(payload["execution"]["limits"]["memory"], "2g")
            self.assertEqual(payload["skills"], [{"name": "safe-skill", "enabled": False}])
            self.assertEqual(payload["mcp_servers"], [{"name": "github", "enabled": True}])
            self.assertTrue(payload["credentials"]["provider_configured"])
            self.assertNotIn("do-not-return", serialized)
            self.assertNotIn("secret-command", serialized)
            self.assertNotIn("private:/workspace", serialized)
            self.assertNotIn("OPENAI_API_KEY", serialized)

    def test_target_update_preserves_safe_unmanaged_terminal_settings(self):
        config = {"terminal": {"timeout": 90}}
        updated = capability.set_target(config, "docker", "ghcr.io/example/agent:1")
        self.assertEqual(updated["terminal"]["backend"], "docker")
        self.assertEqual(updated["terminal"]["env_type"], "docker")
        self.assertEqual(updated["terminal"]["docker_image"], "ghcr.io/example/agent:1")
        self.assertEqual(updated["terminal"]["timeout"], 90)
        capability.set_target(updated, "host", None)
        self.assertEqual(updated["terminal"]["backend"], "local")

    def test_enabling_docker_fails_closed_on_dormant_privileged_settings(self):
        risky = [
            {"docker_volumes": ["private:/workspace"]},
            {"docker_mount_cwd_to_workspace": True},
            {"docker_forward_env": ["TOKEN"]},
            {"docker_env": {"TOKEN": "secret"}},
            {"docker_extra_args": ["--privileged"]},
            {"container_persistent": True},
        ]
        for terminal in risky:
            with self.subTest(terminal=terminal), self.assertRaisesRegex(ValueError, "privileged settings"):
                capability.set_target({"terminal": terminal}, "docker", None)

    def test_snapshot_discloses_only_boolean_docker_risk_categories(self):
        payload = capability.snapshot(
            Path("missing"),
            {"terminal": {"docker_volumes": ["private:/workspace"], "docker_forward_env": ["TOKEN"]}},
        )
        self.assertEqual(
            payload["execution"]["privileged_settings"],
            {"mounts": True, "forwarded_environment": True, "extra_arguments": False, "persistent_container": False},
        )
        self.assertFalse(payload["execution"]["safe_to_enable_docker"])
        self.assertNotIn("private:/workspace", json.dumps(payload))
        self.assertNotIn("TOKEN", json.dumps(payload))

    def test_target_and_image_allowlists_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "host or docker"):
            capability.set_target({}, "ssh", None)
        with self.assertRaisesRegex(ValueError, "invalid"):
            capability.set_target({}, "docker", "image; whoami")

    def test_provider_credential_state_is_boolean_only(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "top-secret"}, clear=True):
            payload = capability.snapshot(Path("missing"), {"model": {"provider": "openrouter"}})
        self.assertIs(payload["credentials"]["provider_configured"], True)
        self.assertNotIn("top-secret", json.dumps(payload))
