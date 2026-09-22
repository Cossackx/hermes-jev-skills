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
                sorted(install.OBSERVER_JEVKIT),
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
            from jevkit import __version__
            self.assertEqual(result.stdout.strip(), __version__)
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

    def test_external_dirs_parser_accepts_scalar_and_flow_list_but_not_nested_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.yaml"
            config.write_text(
                "skills:\n"
                "  cache:\n"
                "    external_dirs: ignored\n"
                "  external_dirs: ['shared#one', shared-two] # keep\n"
            )
            self.assertEqual(
                install._configured_external_dirs(config),
                [base / "shared#one", base / "shared-two"],
            )
            config.write_text("skills:\n  external_dirs: 'single root' # keep\n")
            self.assertEqual(install._configured_external_dirs(config), [base / "single root"])
            config.write_text("skills:\n  cache:\n    external_dirs: nested-only\n")
            self.assertEqual(install._configured_external_dirs(config), [])

    def test_check_predicts_shared_skills_that_the_real_install_will_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            shared = home / ".agents" / "skills"
            shared.parent.mkdir(parents=True)
            root = base / "hermes"
            root.mkdir()
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs: {shared}\n"
            )
            output = io.StringIO()
            with mock.patch.object(install.Path, "home", return_value=home), mock.patch.object(
                sys,
                "argv",
                ["install.py", "--check", "--hermes-home", str(root), "--enable", "none"],
            ), redirect_stdout(output):
                self.assertEqual(install.main(), 0)

            report = json.loads(output.getvalue())
            self.assertFalse(shared.exists())
            self.assertEqual(report["hermes"]["skills_external_in"], ["default"])
            self.assertEqual(report["hermes"]["skills_linked_in"], [])

    def test_main_installs_shared_skills_before_choosing_hermes_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            shared = home / ".agents" / "skills"
            shared.parent.mkdir(parents=True)
            root = base / "hermes"
            root.mkdir()
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs:\n    - {shared}\n"
            )
            output = io.StringIO()
            with mock.patch.object(install.Path, "home", return_value=home), mock.patch.object(
                sys,
                "argv",
                ["install.py", "--hermes-home", str(root), "--enable", "none"],
            ), redirect_stdout(output):
                self.assertEqual(install.main(), 0)

            report = json.loads(output.getvalue())
            self.assertTrue((shared / "jev-computer-use" / "SKILL.md").is_file())
            self.assertFalse(os.path.lexists(root / "skills" / "jev"))
            self.assertEqual(report["hermes"]["skills_external_in"], ["default"])

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
            self.assertTrue((root / "skills" / "jev" / "jev-setup" / "SKILL.md").is_file())
            self.assertTrue((root / "profiles" / "alpha" / "skills" / "jev" / "jev-setup" / "SKILL.md").is_file())
            self.assertEqual(report["skills_linked_in"], ["default", "alpha"])
            self.assertEqual(report["enabled_in"], ["alpha: enabled"])
            self.assertNotIn("hermes-jev", (root / "config.yaml").read_text())
            install.uninstall_hermes(root)
            self.assertFalse(os.path.lexists(profile_plugin))
            self.assertFalse(os.path.lexists(root / "profiles" / "alpha" / "skills" / "jev"))
            self.assertEqual((root / "profiles" / "alpha" / "config.yaml").read_text(), CONFIG)

    def test_external_skills_replace_stale_default_copy_without_hiding_profile_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "hermes"
            shared = base / "shared-skills"
            install.install_skills(shared, check=False)
            root.mkdir()
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs:\n    - {shared}\n"
            )
            profile = root / "profiles" / "alpha"
            profile.mkdir(parents=True)
            (profile / "config.yaml").write_text(CONFIG)
            stale = root / "skills" / "jev"
            install.install_skills(stale, check=False)

            report = install.install_hermes(root, "all", check=False)

            self.assertFalse(os.path.lexists(stale))
            self.assertTrue((shared / "jev-computer-use" / "SKILL.md").is_file())
            self.assertTrue((profile / "skills" / "jev" / "jev-computer-use" / "SKILL.md").is_file())
            self.assertEqual(report["skills_external_in"], ["default"])
            self.assertEqual(report["skills_linked_in"], ["alpha"])
            self.assertEqual(report["skills_conflicts_in"], [])

    def test_categorized_external_root_prevents_duplicate_local_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "hermes"
            shared = base / "shared-skills"
            install.install_skills(shared / "jev", check=False)
            root.mkdir()
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs: [{shared}]\n"
            )

            report = install.install_hermes(root, "all", check=False)

            self.assertEqual(report["skills_external_in"], ["default"])
            self.assertFalse(os.path.lexists(root / "skills" / "jev"))

    def test_local_projection_cannot_count_as_its_own_external_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            local = root / "skills" / "jev"
            install.install_skills(local, check=False)
            root.mkdir(exist_ok=True)
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs: {local}\n"
            )

            report = install.install_hermes(root, "all", check=False)

            self.assertEqual(report["skills_external_in"], [])
            self.assertEqual(report["skills_linked_in"], ["default"])
            self.assertTrue((local / "jev-computer-use" / "SKILL.md").is_file())

    def test_modified_local_bundle_is_preserved_and_reported_as_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "hermes"
            shared = base / "shared-skills"
            install.install_skills(shared, check=False)
            local = root / "skills" / "jev"
            install.install_skills(local, check=False)
            changed = local / "jev-computer-use" / "SKILL.md"
            changed.write_text(changed.read_text() + "\nlocal change\n")
            root.mkdir(exist_ok=True)
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs: {shared}\n"
            )

            report = install.install_hermes(root, "all", check=False)

            self.assertEqual(report["skills_conflicts_in"], ["default"])
            self.assertIn("local change", changed.read_text())
            uninstall = install.uninstall_hermes(root)
            self.assertIn(str(local), uninstall["preserved_unmanaged"])
            self.assertIn("local change", changed.read_text())

    def test_unrelated_local_link_is_preserved_during_install_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "hermes"
            shared = base / "shared-skills"
            install.install_skills(shared, check=False)
            unrelated = base / "user-owned-jev"
            unrelated.mkdir()
            sentinel = unrelated / "keep.txt"
            sentinel.write_text("keep")
            local = root / "skills" / "jev"
            install._link(unrelated, local)
            root.mkdir(exist_ok=True)
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs: {shared}\n"
            )

            report = install.install_hermes(root, "all", check=False)

            self.assertEqual(report["skills_conflicts_in"], ["default"])
            self.assertEqual(install._real_target(local), install._real_target(unrelated))
            self.assertEqual(sentinel.read_text(), "keep")
            uninstall = install.uninstall_hermes(root)
            self.assertIn(str(local), uninstall["preserved_unmanaged"])
            self.assertTrue(os.path.lexists(local))
            self.assertEqual(sentinel.read_text(), "keep")

    def test_added_empty_directory_makes_local_bundle_unmanaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "hermes"
            shared = base / "shared-skills"
            install.install_skills(shared, check=False)
            local = root / "skills" / "jev"
            install.install_skills(local, check=False)
            empty = local / "jev-computer-use" / "user-empty-directory"
            empty.mkdir()
            root.mkdir(exist_ok=True)
            (root / "config.yaml").write_text(
                CONFIG + f"skills:\n  external_dirs: {shared}\n"
            )

            report = install.install_hermes(root, "all", check=False)

            self.assertEqual(report["skills_conflicts_in"], ["default"])
            self.assertTrue(empty.is_dir())

    def test_preserved_conflict_without_skill_is_not_a_planned_external_skill(self):
        for check in (True, False):
            with self.subTest(check=check), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                home = base / "home"
                shared = home / ".agents" / "skills"
                conflict = shared / "jev-browser-use"
                conflict.mkdir(parents=True)
                sentinel = conflict / "keep.txt"
                sentinel.write_text("user owned")
                root = base / "hermes"
                root.mkdir()
                (root / "config.yaml").write_text(
                    CONFIG + f"skills:\n  external_dirs: {shared}\n"
                )
                argv = ["install.py", "--hermes-home", str(root), "--enable", "none"]
                if check:
                    argv.append("--check")
                output = io.StringIO()
                with mock.patch.object(install.Path, "home", return_value=home), \
                        mock.patch.object(sys, "argv", argv), redirect_stdout(output):
                    self.assertEqual(install.main(), 0)
                report = json.loads(output.getvalue())
                self.assertEqual(report["skill_folders"][0]["conflicts"], ["jev-browser-use"])
                self.assertEqual(report["hermes"]["skills_external_in"], [])
                self.assertEqual(report["hermes"]["skills_linked_in"], ["default"])
                self.assertEqual(sentinel.read_text(), "user owned")
                local = root / "skills" / "jev" / "jev-browser-use" / "SKILL.md"
                self.assertEqual(local.is_file(), not check)

    def test_top_level_uninstall_preserves_modified_generic_skill_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            folder = base / "generic-skills"
            install.install_skills(folder, check=False)
            changed = folder / "jev-computer-use" / "SKILL.md"
            changed.write_text(changed.read_text() + "\nlocal change\n")
            output = io.StringIO()
            with mock.patch.object(install.Path, "home", return_value=home), mock.patch.object(
                sys,
                "argv",
                ["install.py", "--uninstall", "--hermes-home", str(base / "missing"),
                 "--skills-dir", str(folder)],
            ), redirect_stdout(output):
                self.assertEqual(install.main(), 0)

            report = json.loads(output.getvalue())
            self.assertTrue(changed.is_file())
            self.assertIn(str(folder / "jev-computer-use"),
                          report["skills_preserved_unmanaged"])
            self.assertFalse((folder / "jev-setup").exists())


if __name__ == "__main__":
    unittest.main()
