"""Behavior tests for the narrowed Hermes skill-shadow plugin."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = ROOT / "hermes" / "plugin" / "hermes-jev"

if "hermes_constants" not in sys.modules:
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: Path(tempfile.gettempdir()) / "hermes"
    sys.modules["hermes_constants"] = hermes_constants


def load_plugin(work: Path):
    package_dir = work / "hermes_jev_plugin"
    package_dir.mkdir()
    shutil.copy2(PLUGIN_SOURCE / "__init__.py", package_dir / "__init__.py")
    shutil.copytree(ROOT / "jevkit", package_dir / "jevkit")
    name = f"_hermes_jev_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return name, module


class PluginShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module_name, self.plugin = load_plugin(Path(self.temp.name))
        self.addCleanup(sys.modules.pop, self.module_name, None)

    @staticmethod
    def decision():
        return {
            "status": "ok",
            "needs_skill": 0.88,
            "skills": [
                {"name": "audit-only", "path": "one", "match": 0.93},
                {"name": "personal-investment-analysis", "path": "two", "match": 0.85},
            ],
            "latency_ms": 941,
        }

    def test_home_uses_hermes_profile_scope(self):
        scoped = Path(self.temp.name) / "profiles" / "researcher"
        with mock.patch("hermes_constants.get_hermes_home", return_value=scoped):
            self.assertEqual(self.plugin._home(), scoped)

    def test_profile_state_does_not_inherit_default_profile(self):
        root = Path(self.temp.name) / "hermes"
        profile = root / "profiles" / "researcher"
        (root / "jev").mkdir(parents=True)
        (root / "jev" / "state.json").write_text('{"skills": "on"}', encoding="utf-8")
        with mock.patch("hermes_constants.get_hermes_home", return_value=profile):
            self.assertEqual(self.plugin._state(), {})

    def test_sensitive_turn_is_skipped_before_catalog_discovery_or_api(self):
        with (
            mock.patch.object(self.plugin, "_setting", side_effect=lambda name, default: "shadow" if name == "skills" else default),
            mock.patch.object(self.plugin.skillpick, "discover") as discover,
            mock.patch.object(self.plugin.skillpick, "pick") as pick,
            mock.patch.object(self.plugin, "_log") as log,
        ):
            result = self.plugin._on_pre_llm_call(
                session_id="s", turn_id=1, user_message="my password is hunter2"
            )

        self.assertIsNone(result)
        discover.assert_not_called()
        pick.assert_not_called()
        self.assertEqual(log.call_args.args[0]["status"], "fail_open")
        self.assertEqual(log.call_args.args[0]["reason"], "turn looks sensitive; not sent")

    def test_structured_turn_is_checked_for_secrets_before_api_size_limit(self):
        message = {"note": "x" * 6100, "password": "hunter2"}
        with (
            mock.patch.object(
                self.plugin,
                "_setting",
                side_effect=lambda name, default: "shadow" if name == "skills" else default,
            ),
            mock.patch.object(self.plugin.skillpick, "discover") as discover,
            mock.patch.object(self.plugin.skillpick, "pick") as pick,
            mock.patch.object(self.plugin, "_log") as log,
        ):
            result = self.plugin._on_pre_llm_call(
                session_id="s", turn_id=1, user_message=message
            )

        self.assertIsNone(result)
        discover.assert_not_called()
        pick.assert_not_called()
        self.assertEqual(log.call_args.args[0]["reason"], "turn looks sensitive; not sent")

    def test_context_only_followups_defer_before_catalog_discovery_or_api(self):
        for phrase in ("continue", "Continue.", "make it so", "proceed!", "do it"):
            with self.subTest(phrase=phrase):
                with (
                    mock.patch.object(
                        self.plugin,
                        "_setting",
                        side_effect=lambda name, default: "shadow" if name == "skills" else default,
                    ),
                    mock.patch.object(self.plugin.skillpick, "discover") as discover,
                    mock.patch.object(self.plugin.skillpick, "pick") as pick,
                    mock.patch.object(self.plugin, "_log") as log,
                ):
                    result = self.plugin._on_pre_llm_call(
                        session_id="s", turn_id=1, user_message=phrase
                    )

                self.assertIsNone(result)
                discover.assert_not_called()
                pick.assert_not_called()
                self.assertEqual(log.call_args.args[0]["status"], "defer_native")
                self.assertEqual(log.call_args.args[0]["reason"], "context_only_followup")
                self.assertEqual(log.call_args.args[0]["latency_ms"], 0)

    def test_hermes_control_messages_defer_before_catalog_discovery_or_api(self):
        prefixes = (
            "[ASYNC DELEGATION BATCH COMPLETE",
            "[IMPORTANT: Background process",
            "[CONTEXT COMPACTION",
            "[PRIOR CONTEXT",
            "[Your active task list was preserved across context compression]",
            "[Continuing toward your standing goal]",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                with (
                    mock.patch.object(
                        self.plugin,
                        "_setting",
                        side_effect=lambda name, default: "shadow" if name == "skills" else default,
                    ),
                    mock.patch.object(self.plugin.skillpick, "discover") as discover,
                    mock.patch.object(self.plugin.skillpick, "pick") as pick,
                    mock.patch.object(self.plugin, "_log") as log,
                ):
                    result = self.plugin._on_pre_llm_call(
                        session_id="s", turn_id=1, user_message=prefix + " — details"
                    )

                self.assertIsNone(result)
                discover.assert_not_called()
                pick.assert_not_called()
                self.assertEqual(log.call_args.args[0]["status"], "defer_native")
                self.assertEqual(log.call_args.args[0]["reason"], "hermes_control_message")
                self.assertEqual(log.call_args.args[0]["latency_ms"], 0)

    def test_longer_directive_is_not_mistaken_for_context_only_followup(self):
        catalog = [{"name": "codex", "description": "Use Codex", "path": "codex"}]
        with (
            mock.patch.object(
                self.plugin,
                "_setting",
                side_effect=lambda name, default: "shadow" if name == "skills" else default,
            ),
            mock.patch.object(self.plugin, "_skill_roots", return_value=[]),
            mock.patch.object(self.plugin.skillpick, "discover", return_value=catalog),
            mock.patch.object(self.plugin.skillpick, "pick", return_value=self.decision()) as pick,
            mock.patch.object(self.plugin, "_log"),
        ):
            self.plugin._on_pre_llm_call(
                session_id="s", turn_id=1, user_message="do it after reviewing the Codex login"
            )

        pick.assert_called_once()

    def test_shadow_evaluates_and_logs_two_candidates_without_injecting_context(self):
        roots = [Path(self.temp.name) / "local", Path(self.temp.name) / "external"]
        catalog = [{"name": "one", "description": "first", "path": "one"}]
        with (
            mock.patch.object(self.plugin, "_setting", side_effect=lambda name, default: "shadow" if name == "skills" else default),
            mock.patch.object(self.plugin, "_skill_roots", return_value=roots),
            mock.patch.object(self.plugin, "_disabled_skills", return_value=set()),
            mock.patch.object(self.plugin.skillpick, "discover", return_value=catalog) as discover,
            mock.patch.object(self.plugin.skillpick, "pick", return_value=self.decision()) as pick,
            mock.patch.object(self.plugin, "_log") as log,
        ):
            result = self.plugin._on_pre_llm_call(session_id="s", turn_id=1, user_message="analyze my portfolio")

        self.assertIsNone(result)
        discover.assert_called_once_with(roots, disabled=set())
        pick.assert_called_once_with("analyze my portfolio", catalog, top_k=2)
        entry = log.call_args.args[0]
        self.assertEqual(entry["mode"], "shadow")
        self.assertEqual(entry["picked"], ["audit-only", "personal-investment-analysis"])
        self.assertEqual(entry["matches"], [0.93, 0.85])
        self.assertEqual(entry["latency_ms"], 941)

    def test_advisory_mode_mentions_both_candidates(self):
        with (
            mock.patch.object(self.plugin, "_setting", side_effect=lambda name, default: "on" if name == "skills" else default),
            mock.patch.object(self.plugin, "_skill_roots", return_value=[]),
            mock.patch.object(self.plugin.skillpick, "discover", return_value=[{"name": "one"}]),
            mock.patch.object(self.plugin.skillpick, "pick", return_value=self.decision()),
            mock.patch.object(self.plugin, "_log"),
        ):
            result = self.plugin._on_pre_llm_call(session_id="s", turn_id=1, user_message="analyze my portfolio")

        context = result["context"]
        self.assertIn("audit-only", context)
        self.assertIn("personal-investment-analysis", context)
        self.assertIn("advisory", context.lower())

    def test_skill_roots_follow_hermes_profile_config(self):
        home = Path(self.temp.name) / "hermes"
        local = home / "skills"
        created = Path(self.temp.name) / "created"
        external = Path(self.temp.name) / ".agents" / "skills"
        for path in (local, created, external):
            path.mkdir(parents=True)
        config = {"skills": {"create_dir": str(created), "external_dirs": [str(external), str(local)]}}
        with mock.patch("hermes_constants.get_hermes_home", return_value=home):
            self.assertEqual(self.plugin._skill_roots(config), [local.resolve(), created.resolve(), external.resolve()])

    def test_register_exposes_only_skill_observation_and_status_command(self):
        class Context:
            def __init__(self):
                self.hooks = []
                self.commands = []
                self.tools = []
                self.middleware = []
                self.prompt_sections = []

            def register_hook(self, name, handler):
                self.hooks.append(name)

            def register_command(self, name, handler, **kwargs):
                self.commands.append(name)

            def register_tool(self, **kwargs):
                self.tools.append(kwargs["name"])

            def register_middleware(self, name, handler):
                self.middleware.append(name)

            def register_system_prompt_section(self, name, text, **kwargs):
                self.prompt_sections.append(name)

        context = Context()
        self.plugin.register(context)
        self.assertEqual(context.hooks, ["pre_llm_call"])
        self.assertEqual(context.commands, ["jev"])
        self.assertEqual(context.tools, [])
        self.assertEqual(context.middleware, [])
        self.assertEqual(context.prompt_sections, [])

    def test_command_cannot_enable_disabled_features(self):
        with mock.patch.object(self.plugin, "_read", return_value={}):
            text = self.plugin._jev_command("routing on")
        self.assertIn("skills shadow|on|off", text)
        self.assertIn("not present", text.lower())

    def test_command_discards_unsupported_legacy_switches(self):
        home = Path(self.temp.name) / "hermes"
        state = home / "jev" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"routing": "on", "notice": "on"}', encoding="utf-8")
        with mock.patch("hermes_constants.get_hermes_home", return_value=home):
            self.plugin._jev_command("skills shadow")
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), {"skills": "shadow"})


if __name__ == "__main__":
    unittest.main()
