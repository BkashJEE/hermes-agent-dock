from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


install_module = load("agent_dock_install", "install.py")
uninstall_module = load("agent_dock_uninstall", "uninstall.py")


class InstallLifecycleTests(unittest.TestCase):
    def test_copy_install_is_idempotent_and_uninstall_is_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            first = install_module.install(home, copy_only=True)
            self.assertTrue((home / "desktop-plugins" / "hermes-agent-dock" / "plugin.js").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "plugin_api.py").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "dock_runner.py").is_file())
            self.assertEqual(len(first["files"]), 5)
            self.assertFalse(any((home / "plugins" / "hermes-agent-dock").rglob("__pycache__")))
            on_disk = json.loads(
                (home / "plugins" / "hermes-agent-dock" / "install-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("enable_result", on_disk)
            self.assertIsNone(on_disk["enable_result"])

            second = install_module.install(home, copy_only=True)
            self.assertIsNotNone(second["backup"])
            result = uninstall_module.uninstall(home, copy_only=True, purge=False)
            self.assertEqual(result["removed_paths"], 2)
            self.assertTrue(Path(result["backup"]).is_dir())
            self.assertFalse((home / "desktop-plugins" / "hermes-agent-dock").exists())
            self.assertFalse((home / "plugins" / "hermes-agent-dock").exists())

    def test_purge_removes_installed_code(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            result = uninstall_module.uninstall(home, copy_only=True, purge=True)
            self.assertTrue(result["purged"])
            self.assertIsNone(result["backup"])

    def test_install_manifest_records_final_enable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            responses = [
                subprocess.CompletedProcess(
                    ["hermes", "plugins", "enable", "hermes-agent-dock"],
                    0,
                    "enabled",
                    "",
                ),
                subprocess.CompletedProcess(
                    ["hermes", "plugins", "list"],
                    0,
                    "enabled      user     0.1.0    hermes-agent-dock",
                    "",
                ),
            ]
            with patch.object(install_module.shutil, "which", return_value="hermes"), patch.object(
                install_module.subprocess, "run", side_effect=responses
            ):
                result = install_module.install(home, copy_only=False)
            on_disk = json.loads(
                (home / "plugins" / "hermes-agent-dock" / "install-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(on_disk["enable_result"], result["enable_result"])
            self.assertEqual(on_disk["enable_result"]["returncode"], 0)

    def test_install_rejects_not_enabled_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            responses = [
                subprocess.CompletedProcess(["hermes", "plugins", "enable"], 1, "", "write failed"),
                subprocess.CompletedProcess(
                    ["hermes", "plugins", "list"],
                    0,
                    "enabled      user     0.1.0    hermes-agent-dock-old\n"
                    "not enabled  user     0.1.0    hermes-agent-dock",
                    "",
                ),
            ]
            with patch.object(install_module.shutil, "which", return_value="hermes"), patch.object(
                install_module.subprocess, "run", side_effect=responses
            ):
                with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                    install_module.install(home, copy_only=False)
            self.assertFalse(
                (home / "plugins" / "hermes-agent-dock" / "install-manifest.json").exists()
            )

    def test_rapid_reinstalls_use_unique_backup_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            second = install_module.install(home, copy_only=True)
            third = install_module.install(home, copy_only=True)
            self.assertNotEqual(second["backup"], third["backup"])
            self.assertTrue(Path(second["backup"]).is_dir())
            self.assertTrue(Path(third["backup"]).is_dir())

    def test_uninstall_leaves_code_when_disable_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            responses = [
                subprocess.CompletedProcess(["hermes", "plugins", "disable"], 1, "", "state write failed"),
                subprocess.CompletedProcess(
                    ["hermes", "plugins", "list"],
                    0,
                    "disabled     user     0.1.0    hermes-agent-dock-old\n"
                    "enabled      user     0.1.0    hermes-agent-dock",
                    "",
                ),
            ]
            with patch.object(uninstall_module.shutil, "which", return_value="hermes"), patch.object(
                uninstall_module.subprocess, "run", side_effect=responses
            ):
                with self.assertRaisesRegex(RuntimeError, "left untouched"):
                    uninstall_module.uninstall(home, copy_only=False)
            self.assertTrue((home / "desktop-plugins" / "hermes-agent-dock").is_dir())
            self.assertTrue((home / "plugins" / "hermes-agent-dock").is_dir())

    def test_uninstall_rejects_missing_inventory_row(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            responses = [
                subprocess.CompletedProcess(["hermes", "plugins", "disable"], 1, "", "state write failed"),
                subprocess.CompletedProcess(
                    ["hermes", "plugins", "list"],
                    0,
                    "enabled      user     1.0.0    other-plugin",
                    "",
                ),
            ]
            with patch.object(uninstall_module.shutil, "which", return_value="hermes"), patch.object(
                uninstall_module.subprocess, "run", side_effect=responses
            ):
                with self.assertRaisesRegex(RuntimeError, "left untouched"):
                    uninstall_module.uninstall(home, copy_only=False)
            self.assertTrue((home / "desktop-plugins" / "hermes-agent-dock").is_dir())
            self.assertTrue((home / "plugins" / "hermes-agent-dock").is_dir())

    def test_uninstall_accepts_verified_disabled_state_after_cli_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            responses = [
                subprocess.CompletedProcess(["hermes", "plugins", "disable"], 1, "", "state write failed"),
                subprocess.CompletedProcess(
                    ["hermes", "plugins", "list"],
                    0,
                    "disabled     user     0.1.0    hermes-agent-dock",
                    "",
                ),
            ]
            with patch.object(uninstall_module.shutil, "which", return_value="hermes"), patch.object(
                uninstall_module.subprocess, "run", side_effect=responses
            ):
                result = uninstall_module.uninstall(home, copy_only=False)
            self.assertEqual(result["removed_paths"], 2)
            self.assertTrue(result["disabled"]["confirmed_disabled"])

    def test_uninstall_accepts_actual_not_enabled_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            responses = [
                subprocess.CompletedProcess(["hermes", "plugins", "disable"], 0, "disabled", ""),
                subprocess.CompletedProcess(
                    ["hermes", "plugins", "list"],
                    0,
                    "not enabled  user     0.1.0    hermes-agent-dock",
                    "",
                ),
            ]
            with patch.object(uninstall_module.shutil, "which", return_value="hermes"), patch.object(
                uninstall_module.subprocess, "run", side_effect=responses
            ):
                result = uninstall_module.uninstall(home, copy_only=False)
            self.assertEqual(result["removed_paths"], 2)
            self.assertTrue(result["disabled"]["confirmed_disabled"])


if __name__ == "__main__":
    unittest.main()
