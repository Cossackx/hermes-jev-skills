"""Deterministic offline contracts for fresh Jev-routed Hermes launches."""
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
sys.path.insert(0, str(ROOT))
from jevkit import launch  # noqa: E402

KEY = "synthetic-profile-key"
MODELS = [
    {"provider": "openai-codex", "model": "gpt-5.6-terra-900k", "context": 128000, "vision": False},
    {"provider": "openai-codex", "model": "gpt-5.6-sol-900k", "context": 128000, "vision": False},
    {"provider": "openai-codex", "model": "gpt-6-astra", "context": 256000, "vision": True},
]
TIERS = {
    "simple": {"general": ["gpt-5.6-terra-900k"]},
    "medium": {"coding": ["gpt-5.6-sol-900k"]},
    "hard": {"general": ["gpt-6-astra"]},
}


def write_routing(home: Path, *, tiers=TIERS, models=MODELS) -> None:
    path = home / "jev" / "routing.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"tiers": tiers, "models": models}), encoding="utf-8")


def load_routing_tool(work: Path):
    package = work / "routing_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "hermes" / "plugin" / "hermes-jev" / "routing_tool.py", package / "routing_tool.py")
    shutil.copytree(ROOT / "jevkit", package / "jevkit")
    name = f"_routing_tool_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, package / "routing_tool.py", submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return name, module


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "hermes" / "profiles" / "builder"
        write_routing(self.home)
        self.env = {"HERMES_HOME": str(self.home)}

    def test_active_profile_file_is_the_only_config_source_and_rows_are_operator_metadata(self):
        root = self.home.parents[1]
        write_routing(root, tiers={"simple": {"general": ["root-only"]}}, models=[])
        with mock.patch.dict(os.environ, self.env, clear=False):
            config = launch.load_active_config("openai-codex")
            rows = launch.model_rows(config, "openai-codex")
        self.assertEqual(config["tiers"]["simple"]["general"], ["openai-codex:gpt-5.6-terra-900k"])
        self.assertEqual([row["model"] for row in rows], [
            "gpt-5.6-sol-900k", "gpt-5.6-terra-900k", "gpt-6-astra",
        ])
        self.assertEqual({row["provider"] for row in rows}, {"openai-codex"})

    def test_route_is_same_provider_and_never_uses_catalog_discovery(self):
        captured = {}

        def decide(prompt, **kwargs):
            captured.update(prompt=prompt, **kwargs)
            return {"routed": True, "model": "openai-codex:gpt-5.6-sol-900k", "model_id": "gpt-5.6-sol-900k"}

        with (
            mock.patch.dict(os.environ, self.env, clear=False),
            mock.patch.object(launch.route, "decide", side_effect=decide),
            mock.patch.object(launch.route.catalog_mod, "models", side_effect=AssertionError("catalog must not be read")),
        ):
            result = launch.route_model("implement a bounded test", provider="openai-codex", current="gpt-6-astra")

        self.assertTrue(result["routed"])
        self.assertEqual(captured["only_provider"], "openai-codex")
        self.assertEqual(captured["rows"][0]["provider"], "openai-codex")
        self.assertEqual(captured["config"]["tiers"]["hard"]["general"], ["openai-codex:gpt-6-astra"])

    def test_explicit_model_bypasses_jev_and_is_preserved(self):
        with mock.patch.object(launch, "route_model", side_effect=AssertionError("must bypass Jev")):
            with mock.patch("jevkit.launch.subprocess.run") as run:
                run.return_value.returncode = 0
                code = launch.main(["--prompt", "hello", "--model", "gpt-5.6-terra-900k"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [
            "hermes", "--provider", "openai-codex", "--model", "gpt-5.6-terra-900k", "chat", "--query", "hello",
        ])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_launches_a_fresh_approval_preserving_argument_vector(self):
        decision = {"routed": True, "model_id": "gpt-5.6-sol-900k"}
        with mock.patch.object(launch, "route_model", return_value=decision), mock.patch("jevkit.launch.subprocess.run") as run:
            run.return_value.returncode = 7
            code = launch.main(["--prompt", "fix the test", "--provider", "openai-codex", "--current", "gpt-6-astra"])
        self.assertEqual(code, 7)
        self.assertEqual(run.call_args.args[0], [
            "hermes", "--provider", "openai-codex", "--model", "gpt-5.6-sol-900k", "chat", "--query", "fix the test",
        ])
        command = run.call_args.args[0]
        self.assertFalse(any(item in {"--resume", "--continue", "--safe-mode", "--yolo"} for item in command))

    def test_dry_run_never_starts_hermes(self):
        with mock.patch.object(launch, "route_model", return_value={"routed": False, "model": "gpt-6-astra"}), mock.patch("jevkit.launch.subprocess.run") as run:
            code = launch.main(["--prompt", "inspect", "--dry-run"])
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_cli_scope_uses_environment_only_when_host_context_is_absent(self):
        seen = {}

        def decide(_prompt, **_kwargs):
            seen["key"] = launch.keystore.resolve()
            return {"routed": False, "model": "gpt-6-astra"}

        with (
            mock.patch.dict(os.environ, {**self.env, "TYPESAFE_API_KEY": "synthetic-ambient"}, clear=False),
            mock.patch.object(launch.route, "decide", side_effect=decide),
            mock.patch.object(launch.keystore, "_from_file", side_effect=AssertionError("must not read files")),
            mock.patch.object(launch.keystore, "_from_keychain", side_effect=AssertionError("must not read OS store")),
        ):
            launch.route_model("offline check")
        self.assertEqual(seen["key"], "synthetic-ambient")

    def test_resume_and_continue_are_not_launch_arguments(self):
        with self.assertRaises(SystemExit):
            launch.main(["--prompt", "hello", "--resume", "old-session"])
        with self.assertRaises(SystemExit):
            launch.main(["--prompt", "hello", "--continue"])


class RoutingToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "hermes" / "profiles" / "researcher"
        write_routing(self.home)
        self.name, self.tool = load_routing_tool(Path(self.temp.name))
        self.addCleanup(sys.modules.pop, self.name, None)

    def test_registers_one_gated_advisory_tool(self):
        class Context:
            calls = []

            def register_tool(self, **kwargs):
                self.calls.append(kwargs)

        ctx = Context()
        self.tool.register_routing_tool(ctx, lambda name, default="off": "on" if name == "routing" else default)
        self.assertEqual([call["name"] for call in ctx.calls], ["jev_route_model"])
        self.assertEqual(ctx.calls[0]["toolset"], "jev")
        self.assertTrue(ctx.calls[0]["check_fn"]())
        self.assertTrue(ctx.calls[0]["schema"]["description"].lower().startswith("advisory-only"))
        self.assertFalse(ctx.calls[0]["schema"]["parameters"]["additionalProperties"])

    def test_active_tool_uses_profile_secret_scope_and_provider_bound_rows(self):
        agent = types.ModuleType("agent")
        scope = types.ModuleType("agent.secret_scope")
        scope.get_secret = lambda name: KEY if name == "TYPESAFE_API_KEY" else None
        agent.secret_scope = scope
        captured = {}

        def decide(_prompt, **kwargs):
            captured["key"] = self.tool.keystore.resolve()
            captured.update(kwargs)
            return {"routed": True, "model": "openai-codex:gpt-5.6-sol-900k", "model_id": "gpt-5.6-sol-900k"}

        with (
            mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home), "TYPESAFE_API_KEY": "ambient-must-not-win"}, clear=False),
            mock.patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": scope}),
            mock.patch.object(self.tool, "_active_home", return_value=self.home),
            mock.patch.object(self.tool.route, "decide", side_effect=decide),
        ):
            output = json.loads(self.tool._handler({
                "prompt": "prepare a fresh session", "provider": "openai-codex", "current": "gpt-6-astra",
            }))

        self.assertTrue(output["advisory_only"])
        self.assertEqual(captured["key"], KEY)
        self.assertEqual(captured["only_provider"], "openai-codex")
        self.assertEqual({row["provider"] for row in captured["rows"]}, {"openai-codex"})
        self.assertEqual(captured["profile"], "researcher")

    def test_invalid_tool_arguments_refuse_without_decision_call(self):
        with mock.patch.object(self.tool.route, "decide") as decide:
            output = json.loads(self.tool._handler({"prompt": "x", "provider": "openai-codex", "current": "gpt-6-astra", "extra": True}))
        self.assertEqual(output, {"reason": "invalid_arguments", "status": "refused"})
        decide.assert_not_called()


if __name__ == "__main__":
    unittest.main()
