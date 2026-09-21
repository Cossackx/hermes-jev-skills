import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("jev_install", Path(__file__).resolve().parents[1] / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

CONFIG = """model:
  default: some/model   # keep this comment
plugins:
  enabled:
    - coagent-observer
  disabled: []
  entries:
    resource-lifecycle:
      allow_tool_override: false
security:
  redact_secrets: true
"""


class InstallTests(unittest.TestCase):
    def test_plugin_runtime_contains_only_skill_observer_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "jevkit"
            install._copy_observer_jevkit(destination)
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                ["__init__.py", "client.py", "keystore.py", "privacy.py", "skillpick.py"],
            )

    def test_enable_and_disable_touch_only_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG)
            self.assertEqual(install.enable_plugin(config, True), "enabled")
            self.assertEqual(install.enable_plugin(config, True), "already enabled")
            text = config.read_text()
            self.assertIn("  enabled:\n    - hermes-jev\n    - coagent-observer\n", text)
            self.assertIn("# keep this comment", text)
            self.assertEqual(install.enable_plugin(config, False), "disabled")
            self.assertEqual(config.read_text(), CONFIG)

    def test_empty_inline_list_and_missing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled: []\nother: 1\n")
            install.enable_plugin(config, True)
            self.assertEqual(config.read_text(), "plugins:\n  enabled:\n    - hermes-jev\nother: 1\n")
            config.write_text("other: 1\n")
            install.enable_plugin(config, True)
            self.assertIn("plugins:\n  enabled:\n    - hermes-jev", config.read_text())

    def test_nonempty_inline_list_is_preserved_without_duplicate_enabled_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled: [other, 'quoted'] # keep\nother: 1\n")
            self.assertEqual(install.enable_plugin(config, True), "enabled")
            text = config.read_text()
            self.assertEqual(text.count("  enabled:"), 1)
            self.assertIn("  enabled: # keep\n", text)
            self.assertIn("    - hermes-jev\n", text)
            self.assertIn("    - other\n", text)
            self.assertIn("    - 'quoted'\n", text)
            self.assertEqual(install.enable_plugin(config, False), "disabled")
            self.assertEqual(config.read_text(), "plugins:\n  enabled: # keep\n    - other\n    - 'quoted'\nother: 1\n")

    def test_existing_multiline_list_uses_its_actual_indent_and_is_idempotent(self):
        original = "plugins:\n  enabled:\n    - hermes-jev-pruner\n    - other\n  disabled: []\nother: 1\n"
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(original)
            self.assertEqual(install.enable_plugin(config, True), "enabled")
            self.assertEqual(install.enable_plugin(config, True), "already enabled")
            self.assertEqual(
                config.read_text(),
                "plugins:\n  enabled:\n    - hermes-jev\n    - hermes-jev-pruner\n    - other\n  disabled: []\nother: 1\n",
            )
            self.assertEqual(install.enable_plugin(config, False), "disabled")
            self.assertEqual(config.read_text(), original)

    def test_crlf_plugin_list_retains_crlf_and_disable_restores_the_input(self):
        original = b"plugins:\r\n  enabled:\r\n    - hermes-jev-pruner\r\nother: 1\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_bytes(original)
            self.assertEqual(install.enable_plugin(config, True), "enabled")
            enabled = config.read_bytes()
            self.assertIn(b"  enabled:\r\n    - hermes-jev\r\n    - hermes-jev-pruner\r\n", enabled)
            self.assertNotIn(b"\n", enabled.replace(b"\r\n", b""))
            self.assertEqual(install.enable_plugin(config, False), "disabled")
            self.assertEqual(config.read_bytes(), original)

    def test_installed_launcher_uses_the_retained_checkout_not_its_own_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / ".local" / "bin" / "jev"
            install._write_cli_launcher(launcher)
            script = launcher.read_text()
            self.assertIn("repo=", script)
            self.assertNotIn("readlink", script)
            result = subprocess.run(
                ["sh", str(launcher), "--version"],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": ""},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0.3.5")
            self.assertTrue(install._remove(launcher))
            self.assertFalse(launcher.exists())

    def test_cli_check_does_not_create_a_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with mock.patch.object(install.Path, "home", return_value=home):
                report = install.install_cli(check=True)
            self.assertEqual(report["command"], str(home / ".local" / "bin" / "jev"))
            self.assertFalse((home / ".local" / "bin" / "jev").exists())

    def test_next_steps_enable_only_skill_shadow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            (root / "config.yaml").write_text(CONFIG)
            output = io.StringIO()
            with mock.patch.object(
                sys,
                "argv",
                ["install.py", "--check", "--hermes-home", str(root)],
            ), redirect_stdout(output):
                self.assertEqual(install.main(), 0)
            report = json.loads(output.getvalue())
            next_steps = "\n".join(report["next"])
            self.assertIn("/jev skills shadow", next_steps)
            self.assertNotIn("routing", next_steps.lower())

    def test_full_install_links_every_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            for home in (root, root / "profiles" / "alpha"):
                home.mkdir(parents=True)
                (home / "config.yaml").write_text(CONFIG)
            report = install.install_hermes(root, "alpha", check=False)
            self.assertTrue((root / "plugins" / "hermes-jev" / "jevkit" / "client.py").is_file())
            profile_plugin = root / "profiles" / "alpha" / "plugins" / "hermes-jev"
            self.assertTrue(profile_plugin.is_dir())
            self.assertEqual(
                (profile_plugin / "plugin.yaml").read_bytes(),
                (root / "plugins" / "hermes-jev" / "plugin.yaml").read_bytes(),
            )
            self.assertTrue((root / "profiles" / "alpha" / "skills" / "jev" / "jev-setup" / "SKILL.md").is_file())
            self.assertEqual(report["enabled_in"], ["alpha: enabled"])
            self.assertNotIn("hermes-jev", (root / "config.yaml").read_text())
            install.uninstall_hermes(root)
            self.assertFalse(os.path.lexists(profile_plugin))
            self.assertFalse(os.path.lexists(root / "profiles" / "alpha" / "skills" / "jev"))
            self.assertEqual((root / "profiles" / "alpha" / "config.yaml").read_text(), CONFIG)


if __name__ == "__main__":
    unittest.main()
