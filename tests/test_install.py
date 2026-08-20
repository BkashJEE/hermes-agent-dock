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
    def test_release_version_metadata_is_consistent(self):
        version = install_module.VERSION
        plugin_yaml_version = next(
            line.split(":", 1)[1].strip()
            for line in (ROOT / "backend" / "plugin.yaml").read_text(encoding="utf-8").splitlines()
            if line.startswith("version:")
        )
        dashboard_manifest = json.loads(
            (ROOT / "backend" / "dashboard" / "manifest.json").read_text(encoding="utf-8")
        )
        plugin_api_source = (ROOT / "backend" / "dashboard" / "plugin_api.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(plugin_yaml_version, version)
        self.assertEqual(dashboard_manifest["version"], version)
        self.assertIn(f'PLUGIN_VERSION = "{version}"', plugin_api_source)
        self.assertIn('"version": PLUGIN_VERSION', plugin_api_source)


    def test_copy_install_is_idempotent_and_uninstall_is_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            first = install_module.install(home, copy_only=True)
            self.assertTrue((home / "desktop-plugins" / "hermes-agent-dock" / "plugin.js").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "plugin_api.py").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "capability_center.py").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "dock_runner.py").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "control_store.py").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "job_store.py").is_file())
            self.assertTrue((home / "plugins" / "hermes-agent-dock" / "dashboard" / "subagent_progress.py").is_file())
            self.assertEqual(set(first["files"]), {
                "desktop/plugin.js",
                "backend/plugin.yaml",
                "backend/dashboard/manifest.json",
                "backend/dashboard/plugin_api.py",
                "backend/dashboard/capability_center.py",
                "backend/dashboard/dock_runner.py",
                "backend/dashboard/control_store.py",
                "backend/dashboard/job_store.py",
                "backend/dashboard/subagent_progress.py",
            })
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

    def test_custom_home_nested_under_desktop_plugins_keeps_backup_labels_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "desktop-plugins" / "hermes"
            install_module.install(home, copy_only=True)
            second = install_module.install(home, copy_only=True)
            backup = Path(second["backup"])
            self.assertTrue((backup / "desktop" / "plugin.js").is_file())
            self.assertTrue((backup / "backend" / "plugin.yaml").is_file())

    def test_failed_backend_replacement_restores_previous_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            desktop = home / "desktop-plugins" / "hermes-agent-dock"
            backend = home / "plugins" / "hermes-agent-dock"
            (desktop / "previous.txt").write_text("desktop", encoding="utf-8")
            (backend / "previous.txt").write_text("backend", encoding="utf-8")
            real_copytree = install_module.shutil.copytree
            failed = False

            def fail_source_backend_once(source, destination, *args, **kwargs):
                nonlocal failed
                if Path(source) == install_module.ROOT / "backend" and not failed:
                    failed = True
                    raise OSError("simulated backend copy failure")
                return real_copytree(source, destination, *args, **kwargs)

            with patch.object(
                install_module.shutil, "copytree", side_effect=fail_source_backend_once
            ):
                with self.assertRaisesRegex(RuntimeError, "previous files restored"):
                    install_module.install(home, copy_only=True)
            self.assertEqual((desktop / "previous.txt").read_text(encoding="utf-8"), "desktop")
            self.assertEqual((backend / "previous.txt").read_text(encoding="utf-8"), "backend")

    def test_failed_reversible_uninstall_restores_both_components(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            install_module.install(home, copy_only=True)
            desktop = home / "desktop-plugins" / "hermes-agent-dock"
            backend = home / "plugins" / "hermes-agent-dock"
            (desktop / "previous.txt").write_text("desktop", encoding="utf-8")
            (backend / "previous.txt").write_text("backend", encoding="utf-8")
            real_rmtree = uninstall_module.shutil.rmtree
            failed = False

            def fail_backend_removal_once(path, *args, **kwargs):
                nonlocal failed
                if Path(path) == backend and not failed:
                    failed = True
                    raise OSError("simulated backend removal failure")
                return real_rmtree(path, *args, **kwargs)

            with patch.object(uninstall_module.shutil, "rmtree", side_effect=fail_backend_removal_once):
                with self.assertRaisesRegex(RuntimeError, "installed files restored"):
                    uninstall_module.uninstall(home, copy_only=True, purge=False)
            self.assertEqual((desktop / "previous.txt").read_text(encoding="utf-8"), "desktop")
            self.assertEqual((backend / "previous.txt").read_text(encoding="utf-8"), "backend")

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
