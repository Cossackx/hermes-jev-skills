import importlib.util
import io
import json
import os
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
            self.assertIn("  enabled:\n  - hermes-jev\n  - coagent-observer\n", text)
            self.assertIn("# keep this comment", text)
            self.assertEqual(install.enable_plugin(config, False), "disabled")
            self.assertEqual(config.read_text(), CONFIG)

    def test_empty_inline_list_and_missing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled: []\nother: 1\n")
            install.enable_plugin(config, True)
            self.assertEqual(config.read_text(), "plugins:\n  enabled:\n  - hermes-jev\nother: 1\n")
            config.write_text("other: 1\n")
            install.enable_plugin(config, True)
            self.assertIn("plugins:\n  enabled:\n  - hermes-jev", config.read_text())

    def test_nonempty_inline_list_is_preserved_without_duplicate_enabled_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled: [other, 'quoted'] # keep\nother: 1\n")
            self.assertEqual(install.enable_plugin(config, True), "enabled")
            text = config.read_text()
            self.assertEqual(text.count("  enabled:"), 1)
            self.assertIn("  enabled: # keep\n", text)
            self.assertIn("  - hermes-jev\n", text)
            self.assertIn("  - other\n", text)
            self.assertIn("  - 'quoted'\n", text)

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
