"""Offline coverage for the bounded Jev Ultrafast browser runner."""
from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


RUNNER_PATH = Path(__file__).resolve().parents[1] / "skills" / "jev-browser-use" / "scripts" / "jev_browser_agent.py"
SPEC = importlib.util.spec_from_file_location("jev_browser_agent", RUNNER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load runner at {RUNNER_PATH}")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class VenvSelectionTests(unittest.TestCase):
    def test_selects_platform_native_venv_python(self):
        repo = Path("checkout")
        self.assertEqual(
            runner.venv_python(repo, platform="nt"),
            repo / ".venv" / "Scripts" / "python.exe",
        )
        self.assertEqual(
            runner.venv_python(repo, platform="posix"),
            repo / ".venv" / "bin" / "python",
        )

    def test_explicit_repository_override_wins(self):
        self.assertEqual(
            runner.resolve_ultrafast_repo({"JEV_ULTRAFAST_REPO": "portable-checkout"}),
            Path("portable-checkout"),
        )


class EndpointTests(unittest.TestCase):
    def test_maps_http_and_websocket_endpoints_to_the_correct_environment(self):
        env = {"BU_CDP_WS": "ws://daily-browser.example/devtools/browser/old"}
        self.assertEqual(
            runner.configure_cdp_endpoint("http://automation.example:9222", env),
            ("BU_CDP_URL", "http://automation.example:9222"),
        )
        self.assertEqual(env, {"BU_CDP_URL": "http://automation.example:9222"})

        self.assertEqual(
            runner.configure_cdp_endpoint("wss://automation.example/devtools/browser/new", env),
            ("BU_CDP_WS", "wss://automation.example/devtools/browser/new"),
        )
        self.assertEqual(env, {"BU_CDP_WS": "wss://automation.example/devtools/browser/new"})

    def test_uses_environment_endpoint_and_refuses_an_implicit_browser(self):
        self.assertEqual(
            runner.resolve_cdp_endpoint("", {"BU_CDP_URL": "https://automation.example:9222"}),
            ("BU_CDP_URL", "https://automation.example:9222"),
        )
        with self.assertRaisesRegex(ValueError, "no dedicated CDP endpoint"):
            runner.resolve_cdp_endpoint("", {})
        with self.assertRaisesRegex(ValueError, "must start"):
            runner.resolve_cdp_endpoint("localhost:9222", {})

    def test_main_refuses_before_credential_or_browser_fallback_without_endpoint(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            result = runner.main([
                "--url", "https://example.test", "--goal", "Read the heading",
                "--allow-hosts", "example.test",
            ])
        self.assertEqual(result, 2)
        self.assertIn("no dedicated CDP endpoint", output.getvalue())


class OwnedTargetActivationTests(unittest.TestCase):
    def test_activates_only_the_agents_owned_target_then_settles(self):
        calls = []
        waits = []

        class Agent:
            class browser:
                target = "owned-target-id"

        def cdp_call(method, **params):
            calls.append((method, params))

        self.assertEqual(
            runner.activate_owned_target(Agent(), cdp_call=cdp_call, sleep=waits.append),
            "owned-target-id",
        )
        self.assertEqual(calls, [("Target.activateTarget", {"targetId": "owned-target-id"})])
        self.assertEqual(waits, [runner.ACTIVATION_SETTLE_SECONDS])

    def test_refuses_to_activate_when_the_agent_has_no_owned_target(self):
        with self.assertRaisesRegex(RuntimeError, "owned browser target"):
            runner.activate_owned_target(object(), cdp_call=lambda *_args, **_kwargs: None, sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()