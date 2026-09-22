"""Offline contract tests for explicit Jev decision tools."""
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
KEY = "apikey_" + "a1" * 30


def load_tools(work: Path):
    package_dir = work / "hermes_jev_plugin"
    package_dir.mkdir()
    shutil.copy2(PLUGIN_SOURCE / "__init__.py", package_dir / "__init__.py")
    shutil.copy2(PLUGIN_SOURCE / "decision_tools.py", package_dir / "decision_tools.py")
    shutil.copytree(ROOT / "jevkit", package_dir / "jevkit")
    name = f"_hermes_jev_tools_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name, package_dir / "decision_tools.py", submodule_search_locations=[str(package_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return name, module


class DecisionToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module_name, self.tools = load_tools(Path(self.temp.name))
        self.addCleanup(sys.modules.pop, self.module_name, None)
        agent = types.ModuleType("agent")
        scope = types.ModuleType("agent.secret_scope")
        scope.get_secret = lambda name: KEY if name == "TYPESAFE_API_KEY" else None
        agent.secret_scope = scope
        self.agent_patch = mock.patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": scope})
        self.agent_patch.start()
        self.addCleanup(self.agent_patch.stop)

    @staticmethod
    def memory_args():
        return {"query": "what is the current plan?", "candidates": [
            {"id": "a", "text": "The plan is to run offline tests."},
            {"id": "b", "text": "The old plan was replaced."},
        ]}

    @staticmethod
    def compact_args():
        return {"messages": [
            {"role": "system", "content": "retain safety rules"},
            {"role": "user", "content": "build a local plugin"},
            {"role": "assistant", "content": "working on it"},
        ], "keep_last": 1}

    @staticmethod
    def action_args():
        return {"request": {
            "schema": "jev.action_choice_request_v1", "goal": "continue safely", "observation_id": "screen-1",
            "regions": [{"id": "button", "role": "button", "label": "Continue", "interactive": True}],
            "candidates": [
                {"id": "reobserve", "description": "Observe the page again."},
                {"id": "abstain", "description": "Do not take an action."},
            ],
        }}

    def test_registers_three_profile_gated_explicit_tools(self):
        class Context:
            def __init__(self):
                self.calls = []

            def register_tool(self, **kwargs):
                self.calls.append(kwargs)

        ctx = Context()
        self.tools.register_tools(ctx, lambda name, default="off": "on" if name == "memory" else default)
        self.assertEqual([call["name"] for call in ctx.calls], [
            "jev_memory_filter", "jev_compact_select", "jev_choose_action",
        ])
        self.assertTrue(ctx.calls[0]["check_fn"]())
        self.assertFalse(ctx.calls[1]["check_fn"]())
        for call in ctx.calls:
            self.assertEqual(call["toolset"], "jev")
            self.assertFalse(call["schema"]["parameters"].get("additionalProperties", True))

    def test_registered_handlers_recheck_profile_mode(self):
        class Context:
            def __init__(self): self.calls = []
            def register_tool(self, **kwargs): self.calls.append(kwargs)
        ctx = Context()
        enabled = {"memory": "on"}
        self.tools.register_tools(ctx, lambda name, default="off": enabled.get(name, default))
        self.assertTrue(ctx.calls[0]["check_fn"]())
        enabled.clear()
        with mock.patch.object(self.tools.rerank, "rerank") as decide:
            result = json.loads(ctx.calls[0]["handler"](self.memory_args()))
        decide.assert_not_called()
        self.assertEqual(result["status"], "disabled")

    def test_duplicate_passage_ids_and_coerced_action_types_are_rejected(self):
        args = self.memory_args()
        args["candidates"][1]["id"] = "a"
        self.assertEqual(json.loads(self.tools._memory_handler(args))["status"], "refused")
        for value in (1, "false", [], {}):
            args = self.action_args()
            args["request"]["regions"][0]["interactive"] = value
            with mock.patch.object(self.tools.choose, "choose") as decide:
                self.assertEqual(json.loads(self.tools._action_handler(args))["status"], "refused")
            decide.assert_not_called()

    def test_memory_uses_only_active_profile_secret_scope(self):
        seen = {}

        def decide(query, candidates, *, top_k):
            seen["key"] = self.tools.keystore.resolve()
            seen["query"] = query
            return {"status": "ok", "selected_ids": ["b"], "dropped_injection_ids": [], "scores": {}}

        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ambient-key"}, clear=False), \
             mock.patch.object(self.tools.rerank, "rerank", side_effect=decide):
            result = json.loads(self.tools._memory_handler(self.memory_args()))

        self.assertEqual(result["selected_ids"], ["b"])
        self.assertEqual(seen["key"], KEY)
        self.assertEqual(seen["query"], "what is the current plan?")

    def test_missing_profile_key_never_falls_back_to_ambient_key(self):
        sys.modules["agent.secret_scope"].get_secret = lambda name: None
        seen = {}

        def decide(*_args, **_kwargs):
            seen["key"] = self.tools.keystore.resolve()
            return {"status": "ok", "selected_ids": [], "dropped_injection_ids": [], "scores": {}}

        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ambient-key"}, clear=False), \
             mock.patch.object(self.tools.rerank, "rerank", side_effect=decide):
            self.tools._memory_handler(self.memory_args())

        self.assertIsNone(seen["key"])

    def test_sensitive_memory_never_calls_jev_and_preserves_baseline(self):
        args = self.memory_args()
        args["candidates"][1]["text"] = "password is not sent"
        with mock.patch.object(self.tools.rerank, "rerank") as decide:
            result = json.loads(self.tools._memory_handler(args))
        decide.assert_not_called()
        self.assertEqual(result["status"], "fail_open")
        self.assertEqual(result["selected_ids"], ["a", "b"])
        self.assertNotIn("password", json.dumps(result))

    def test_invalid_memory_types_are_refused_without_library_call(self):
        args = self.memory_args()
        args["candidates"][0]["text"] = {"not": "plain text"}
        with mock.patch.object(self.tools.rerank, "rerank") as decide:
            result = json.loads(self.tools._memory_handler(args))
        decide.assert_not_called()
        self.assertEqual(result, {"reason": "invalid_arguments", "status": "refused"})

    def test_sensitive_compaction_returns_all_local_fates_without_calling_jev(self):
        args = self.compact_args()
        args["messages"][1]["content"] = "api key = no export"
        with mock.patch.object(self.tools.compact, "select") as select:
            result = json.loads(self.tools._compaction_handler(args))
        select.assert_not_called()
        self.assertEqual(result["status"], "fail_open")
        self.assertEqual(result["fates"], {"0": "keep", "1": "summarize", "2": "keep"})
        self.assertEqual(result["counts"], {"drop": 0, "keep": 2, "summarize": 1})

    def test_action_is_advisory_and_never_executes(self):
        response = {"schema": "jev.action_choice_v1", "selected_id": "abstain", "confidence": 0.96,
                    "reason": "chosen", "observation_id": "screen-1", "probabilities": {"abstain": 0.96}}
        with mock.patch.object(self.tools.choose, "choose", return_value=response) as decide:
            result = json.loads(self.tools._action_handler(self.action_args()))
        decide.assert_called_once()
        self.assertEqual(result["selected_id"], "abstain")
        self.assertIn("advisory-only", self.tools.ACTION_SCHEMA["description"].lower())
        self.assertEqual(self.tools.ACTION_SCHEMA["name"], "jev_choose_action")

    def test_sensitive_action_never_calls_jev_and_returns_reobserve(self):
        args = self.action_args()
        args["request"]["goal"] = "use my password to continue"
        with mock.patch.object(self.tools.choose, "choose") as decide:
            result = json.loads(self.tools._action_handler(args))
        decide.assert_not_called()
        self.assertEqual(result, {"reason": "invalid_arguments", "status": "refused"})


if __name__ == "__main__":
    unittest.main()
