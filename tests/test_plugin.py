"""Behavior tests for the narrowed Hermes skill-shadow plugin."""
from __future__ import annotations

import importlib.util
import json
import os
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
    for file in PLUGIN_SOURCE.glob("*.py"):
        shutil.copy2(file, package_dir / file.name)
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
            mock.patch.object(self.plugin, "_skill_catalog", return_value=catalog),
            mock.patch.object(self.plugin.skillpick, "pick", return_value=self.decision()) as pick,
            mock.patch.object(self.plugin, "_log"),
        ):
            self.plugin._on_pre_llm_call(
                session_id="s", turn_id=1, user_message="do it after reviewing the Codex login"
            )

        pick.assert_called_once()

    def test_shadow_evaluates_and_logs_two_candidates_without_injecting_context(self):
        catalog = [{"name": "one", "description": "first", "path": "one"}]
        with (
            mock.patch.object(self.plugin, "_setting", side_effect=lambda name, default: "shadow" if name == "skills" else default),
            mock.patch.object(self.plugin, "_skill_catalog", return_value=catalog),
            mock.patch.object(self.plugin.skillpick, "pick", return_value=self.decision()) as pick,
            mock.patch.object(self.plugin, "_log") as log,
        ):
            result = self.plugin._on_pre_llm_call(session_id="s", turn_id=1, user_message="analyze my portfolio")

        self.assertIsNone(result)
        pick.assert_called_once_with("analyze my portfolio", catalog, top_k=2)
        entry = log.call_args.args[0]
        self.assertEqual(entry["mode"], "shadow")
        self.assertEqual(entry["picked"], ["audit-only", "personal-investment-analysis"])
        self.assertEqual(entry["matches"], [0.93, 0.85])
        self.assertEqual(entry["latency_ms"], 941)

    def test_advisory_mode_mentions_both_candidates(self):
        with (
            mock.patch.object(self.plugin, "_setting", side_effect=lambda name, default: "on" if name == "skills" else default),
            mock.patch.object(self.plugin, "_skill_catalog", return_value=[{"name": "one"}]),
            mock.patch.object(self.plugin.skillpick, "pick", return_value=self.decision()),
            mock.patch.object(self.plugin, "_log"),
        ):
            result = self.plugin._on_pre_llm_call(session_id="s", turn_id=1, user_message="analyze my portfolio")

        context = result["context"]
        self.assertIn("audit-only", context)
        self.assertIn("personal-investment-analysis", context)
        self.assertIn("advisory", context.lower())

    def test_fast_selector_setting_routes_through_optimized_picker(self):
        catalog = [{"name": "one", "description": "first", "path": "one"}]

        def setting(name, default):
            return {"skills": "on", "selector": "fast"}.get(name, default)

        with (
            mock.patch.object(self.plugin, "_setting", side_effect=setting),
            mock.patch.object(self.plugin, "_skill_catalog", return_value=catalog),
            mock.patch.object(self.plugin.skillpick, "pick_optimized", return_value=self.decision()) as picker,
            mock.patch.object(self.plugin, "_log"),
        ):
            self.plugin._on_pre_llm_call(session_id="s", turn_id=1, user_message="analyze my portfolio")

        picker.assert_called_once_with("analyze my portfolio", catalog, top_k=2, strategy="fast")

    def test_effectiveness_hooks_correlate_without_forwarding_sensitive_payloads(self):
        catalog = [{"name": "one", "description": "first", "path": "one"}]
        recorder = mock.Mock()
        secret = "private-prompt-and-result"
        with (
            mock.patch.object(self.plugin, "_setting", side_effect=lambda name, default: "on" if name == "skills" else default),
            mock.patch.object(self.plugin, "_skill_catalog", return_value=catalog),
            mock.patch.object(self.plugin.skillpick, "pick", return_value=self.decision()),
            mock.patch.object(self.plugin, "_effectiveness", return_value=recorder),
            mock.patch.object(self.plugin, "_log"),
        ):
            self.plugin._on_pre_llm_call(
                session_id="s", task_id="task", turn_id=7, user_message="analyze my portfolio"
            )
            self.plugin._on_post_tool_call(
                tool_name="skill_view", args={"name": "one", "secret": secret},
                result=json.dumps({"success": True, "name": "one", "content": secret}),
                status="ok", error_message=secret, duration_ms=15,
                session_id="s", task_id="task", turn_id=7, tool_call_id="call",
            )
            self.plugin._on_pre_api_request(
                provider="openai-codex", model="gpt-test", retry_count=0,
                session_id="s", task_id="task", turn_id=7, api_request_id="request",
                user_message=secret,
            )
            self.plugin._on_post_api_request(
                provider="openai-codex", model="gpt-test", api_duration=1.2,
                usage={"input_tokens": 10, "output_tokens": 4},
                session_id="s", task_id="task", turn_id=7, api_request_id="request",
                assistant_message=secret,
            )
            self.plugin._on_session_end(
                completed=True, failed=False, interrupted=False,
                session_id="s", task_id="task", turn_id=7,
            )

        recorder.decision.assert_called_once()
        self.assertEqual(recorder.skill_advice.call_count, 2)
        recorder.skill_view_loaded.assert_called_once()
        recorder.tool_outcome.assert_called_once()
        recorder.api_attempt.assert_called_once()
        recorder.api_outcome.assert_called_once()
        recorder.turn_outcome.assert_called_once()
        self.assertNotIn(secret, repr(recorder.mock_calls))

    def test_effectiveness_observers_ignore_untracked_turn(self):
        recorder = mock.Mock()
        with mock.patch.object(self.plugin, "_effectiveness", return_value=recorder):
            self.plugin._on_post_tool_call(
                tool_name="terminal", args={}, result="{}", status="ok",
                session_id="other", turn_id="not-tracked", tool_call_id="call",
            )
        recorder.assert_not_called()

    def test_turn_tracker_keeps_only_ephemeral_digests(self):
        session_id = "raw-session-id"
        turn_id = "raw-turn-id"
        self.plugin._track_turn(session_id, turn_id)

        self.assertTrue(self.plugin._is_tracked(session_id, turn_id))
        self.assertNotIn(session_id, repr(self.plugin._TRACKED_TURNS))
        self.assertNotIn(turn_id, repr(self.plugin._TRACKED_TURNS))

    def test_observers_use_unique_event_ids_when_host_ids_are_missing(self):
        recorder = mock.Mock()
        with mock.patch.object(self.plugin, "_observer_recorder", return_value=recorder):
            self.plugin._on_post_tool_call(tool_name="terminal", status="ok", session_id="s", turn_id="t")
            self.plugin._on_post_tool_call(tool_name="terminal", status="ok", session_id="s", turn_id="t")
            self.plugin._on_pre_api_request(session_id="s", turn_id="t")
            self.plugin._on_pre_api_request(session_id="s", turn_id="t")

        tool_ids = [call.kwargs["event_id"] for call in recorder.tool_outcome.call_args_list]
        api_ids = [call.kwargs["event_id"] for call in recorder.api_attempt.call_args_list]
        self.assertEqual(len(set(tool_ids)), 2)
        self.assertEqual(len(set(api_ids)), 2)
        self.assertNotIn("None", "".join(tool_ids + api_ids))

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

    def test_native_catalog_uses_hermes_parser_scan_order_and_eligibility(self):
        root = Path(self.temp.name) / "skills"
        eligible = root / "eligible" / "SKILL.md"
        unsupported = root / "unsupported" / "SKILL.md"
        eligible.parent.mkdir(parents=True)
        unsupported.parent.mkdir(parents=True)
        eligible.write_text(
            "---\nname: eligible\ndescription: >-\n  Folded description\n  from Hermes.\nplatforms: [windows]\n---\n\nBody.\n",
            encoding="utf-8",
        )
        unsupported.write_text(
            "---\nname: unsupported\ndescription: macOS only.\nplatforms: [macos]\n---\n\nBody.\n",
            encoding="utf-8",
        )

        def iter_files(directory, filename):
            return sorted(directory.rglob(filename))

        def parse_frontmatter(content):
            if "name: eligible" in content:
                return ({"name": "eligible", "description": "Folded description from Hermes.",
                         "platforms": ["windows"]}, "Body.")
            return ({"name": "unsupported", "description": "macOS only.",
                     "platforms": ["macos"]}, "Body.")

        skill_utils = types.ModuleType("agent.skill_utils")
        skill_utils.get_project_skills_dirs = lambda: []
        skill_utils.get_all_skills_dirs = lambda: [root]
        skill_utils.iter_skill_index_files = iter_files
        skill_utils.parse_frontmatter = parse_frontmatter
        skill_utils.skill_matches_platform = lambda fields: fields.get("platforms") == ["windows"]
        skill_utils.skill_matches_environment = lambda fields: True
        skill_utils.get_disabled_skill_names = lambda: set()
        agent = types.ModuleType("agent")
        agent.skill_utils = skill_utils
        with mock.patch.dict(sys.modules, {"agent": agent, "agent.skill_utils": skill_utils}):
            catalog = self.plugin._skill_catalog()

        self.assertEqual(catalog, [{
            "name": "eligible",
            "description": "Folded description from Hermes.",
            "path": str(eligible),
        }])

    def test_catalog_cache_reuses_scan_and_invalidates_on_disabled_change(self):
        root = Path(self.temp.name) / "skills"
        skill_file = root / "eligible" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("---\nname: eligible\ndescription: Cached.\n---\n", encoding="utf-8")
        disabled = set()
        parse = mock.Mock(return_value=({"name": "eligible", "description": "Cached."}, ""))
        skill_utils = types.ModuleType("agent.skill_utils")
        skill_utils.get_project_skills_dirs = lambda: []
        skill_utils.get_all_skills_dirs = lambda: [root]
        skill_utils.iter_skill_index_files = lambda directory, filename: [skill_file]
        skill_utils.parse_frontmatter = parse
        skill_utils.skill_matches_platform = lambda fields: True
        skill_utils.skill_matches_environment = lambda fields: True
        skill_utils.get_disabled_skill_names = lambda: set(disabled)
        agent = types.ModuleType("agent")
        agent.skill_utils = skill_utils

        with mock.patch.dict(sys.modules, {"agent": agent, "agent.skill_utils": skill_utils}):
            self.assertEqual(len(self.plugin._skill_catalog()), 1)
            self.assertEqual(len(self.plugin._skill_catalog()), 1)
            self.assertEqual(parse.call_count, 1)
            disabled.add("eligible")
            self.assertEqual(self.plugin._skill_catalog(), [])
            self.assertEqual(parse.call_count, 2)

    def test_catalog_cache_invalidates_when_environment_value_changes(self):
        root = Path(self.temp.name) / "skills"
        skill_file = root / "eligible" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("---\nname: eligible\ndescription: Cached.\n---\n", encoding="utf-8")
        parse = mock.Mock(return_value=({"name": "eligible", "description": "Cached."}, ""))
        skill_utils = types.ModuleType("agent.skill_utils")
        skill_utils.get_project_skills_dirs = lambda: []
        skill_utils.get_all_skills_dirs = lambda: [root]
        skill_utils.iter_skill_index_files = lambda directory, filename: [skill_file]
        skill_utils.parse_frontmatter = parse
        skill_utils.skill_matches_platform = lambda fields: True
        skill_utils.skill_matches_environment = lambda fields: os.environ.get("JEV_TEST_CAPABILITY") == "enabled"
        skill_utils.get_disabled_skill_names = lambda: set()
        agent = types.ModuleType("agent")
        agent.skill_utils = skill_utils

        with mock.patch.dict(sys.modules, {"agent": agent, "agent.skill_utils": skill_utils}), \
             mock.patch.dict(os.environ, {"JEV_TEST_CAPABILITY": "enabled"}, clear=False):
            self.assertEqual(len(self.plugin._skill_catalog()), 1)
            self.assertEqual(len(self.plugin._skill_catalog()), 1)
            os.environ["JEV_TEST_CAPABILITY"] = "disabled"
            self.assertEqual(self.plugin._skill_catalog(), [])
        self.assertEqual(parse.call_count, 2)

    def test_native_catalog_failure_does_not_broaden_to_portable_fallback(self):
        skill_utils = types.ModuleType("agent.skill_utils")
        skill_utils.get_project_skills_dirs = mock.Mock(return_value=[])
        skill_utils.get_all_skills_dirs = mock.Mock(side_effect=RuntimeError("broken host"))
        skill_utils.iter_skill_index_files = mock.Mock()
        skill_utils.parse_frontmatter = mock.Mock()
        skill_utils.skill_matches_platform = mock.Mock()
        skill_utils.skill_matches_environment = mock.Mock()
        skill_utils.get_disabled_skill_names = mock.Mock(return_value=set())
        agent = types.ModuleType("agent")
        agent.skill_utils = skill_utils
        with (
            mock.patch.dict(sys.modules, {"agent": agent, "agent.skill_utils": skill_utils}),
            mock.patch.object(self.plugin.skillpick, "discover") as discover,
        ):
            catalog = self.plugin._skill_catalog()

        self.assertEqual(catalog, [])
        discover.assert_not_called()

    def test_register_exposes_skill_and_effectiveness_hooks_plus_status_command(self):
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
        self.assertEqual(context.hooks, [
            "pre_llm_call", "post_tool_call", "pre_api_request", "post_api_request",
            "api_request_error", "on_session_end",
        ])
        self.assertEqual(context.commands, ["jev"])
        self.assertEqual(set(context.tools), {"jev_memory_filter", "jev_compact_select", "jev_choose_action", "jev_route_model"})
        self.assertEqual(context.middleware, [])
        self.assertEqual(context.prompt_sections, [])

    def test_unknown_command_reports_real_capability_limits(self):
        with mock.patch.object(self.plugin, "_read", return_value={}):
            text = self.plugin._jev_command("gateway-routing on")
        self.assertIn("skills shadow|on|off", text)
        self.assertIn("automatic gateway switching is not supported", text)

    def test_command_preserves_other_explicit_modes(self):
        home = Path(self.temp.name) / "hermes"
        state = home / "jev" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"routing": "on", "memory": "on", "notice": "on"}', encoding="utf-8")
        with mock.patch("hermes_constants.get_hermes_home", return_value=home):
            self.plugin._jev_command("skills shadow")
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), {"skills": "shadow", "routing": "on", "memory": "on"})


if __name__ == "__main__":
    unittest.main()
