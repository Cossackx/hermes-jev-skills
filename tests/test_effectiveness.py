"""Offline privacy and lifecycle contract for profile-local Jev telemetry."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "hermes" / "plugin" / "hermes-jev" / "effectiveness.py"
spec = importlib.util.spec_from_file_location("jev_effectiveness_test", SOURCE)
telemetry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telemetry)

SECRET = "secret-password-PRIVATE-123"
URL = "https://private.example/path?token=SECRET"
PATH = "C:/Us" + "ers/private/customer.txt"


class EffectivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "profiles" / "alpha"
        self.rec = telemetry.EffectivenessTelemetry(self.home, profile_id="alpha", approved_skills={"ticket-ship"})

    def rows(self, rec=None):
        rec = rec or self.rec
        return [json.loads(line) for path in sorted((rec.home / "jev" / "effectiveness").glob("events.jsonl*"))
                for line in path.read_text(encoding="utf-8").splitlines() if line]

    def assert_private(self, rec=None):
        rec = rec or self.rec
        disk = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in
                         (rec.home / "jev" / "effectiveness").iterdir() if path.is_file() and path.name != "key")
        aggregate = json.dumps(rec.rollup(), sort_keys=True)
        for value in (SECRET, URL, PATH, "alpha", "session-" + SECRET, "turn-" + SECRET):
            self.assertNotIn(value, disk)
            self.assertNotIn(value, aggregate)
        self.assertNotIn("key", aggregate)

    def test_private_allowlist_and_advice_to_successful_load_only(self):
        ids = dict(session_id="session-" + SECRET, turn_id="turn-" + SECRET, task_id=PATH)
        self.rec.skill_advice("ticket-ship", request_id=URL, event_id="advice-1", **ids)
        self.rec.skill_advice(SECRET, request_id=URL, event_id="advice-1", **ids)
        self.rec.skill_view_loaded("ticket-ship", success=False, call_id="failed", **ids)
        self.rec.skill_view_loaded("ticket-ship", success=True, call_id="view-1", **ids)
        self.rec.skill_view_loaded("ticket-ship", success=True, call_id="view-1", **ids)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["schema"] for row in rows}, {"jev.effectiveness.v1"})
        self.assertEqual(rows[0]["skill"], "ticket-ship")
        self.assertNotEqual(rows[1]["skill"], SECRET)
        self.assertEqual(self.rec.rollup()["advice_to_load"], {"advised": 2, "loaded": 1, "matched": 1, "unjoinable": 0})
        self.assert_private()

    def test_missing_ids_unjoined_and_distinct_subsequent_calls_vs_retries(self):
        self.rec.skill_advice("ticket-ship", session_id="s")
        self.rec.skill_view_loaded("ticket-ship", success=True, session_id="s", call_id="one")
        for call, retry in (("first", None), ("second", None), ("retry", "first")):
            self.rec.tool_outcome("success", call_id=call, retry_of_call_id=retry,
                                  session_id="s", turn_id="t", tool_kind="other")
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(self.rec.rollup()["advice_to_load"]["matched"], 0)
        self.assertEqual(self.rec.rollup()["advice_to_load"]["unjoinable"], 1)
        self.assertEqual(self.rec.rollup()["tool_outcomes"], {"success": 3})
        self.assertEqual(self.rec.rollup()["retries"], 1)
        self.assertGreaterEqual(self.rec.rollup()["missing_ids"]["turn_id"], 2)
        self.assertTrue(all(row["turn_id"] is None for row in self.rows()[:2]))
        self.assertTrue(all(row["join_status"] == "unjoined" for row in self.rows()[:2]))
        self.assertEqual(self.rec.rollup()["coverage"]["unjoined_events"], 2)
        self.assertNotEqual(self.rows()[2]["call_id"], self.rows()[3]["call_id"])

    def test_api_missing_usage_and_turns_are_not_task_acceptance(self):
        self.rec.api_attempt(call_id="a", session_id="s", turn_id="t", request_id="r", event_id="same-call")
        self.rec.api_outcome("success", call_id="a", session_id="s", turn_id="t", request_id="r", event_id="same-call",
                             input_tokens=0, output_tokens=2450)
        self.rec.api_outcome("error", call_id="b", input_tokens=None, output_tokens=None,
                             error_text=SECRET, prompt=SECRET, url=URL)
        self.rec.turn_outcome("interrupted", session_id="s", turn_id="t")
        self.rec.turn_outcome("completed", session_id="s", turn_id="u")
        summary = self.rec.rollup()
        self.assertEqual(summary["api"], {"attempt": 1, "success": 1, "error": 1})
        self.assertEqual(summary["token_buckets"]["input"], {"unknown": 1, "0": 1})
        self.assertEqual(summary["token_buckets"]["output"], {"unknown": 1, "1k-4k": 1})
        self.assertEqual(summary["turn_outcomes"], {"interrupted": 1, "completed": 1})
        self.assertEqual(summary["feedback"], {})
        self.assert_private()

    def test_decision_cost_model_tool_and_all_usage_buckets_are_allowlisted(self):
        self.rec.decision(
            "ok", strategy="one-stage", candidate_count=2, needs_skill=0.83,
            latency_ms=420, request_bytes=2048, jev_calls=1,
            session_id="s", turn_id=7, event_id="decision-7", prompt=SECRET,
        )
        self.rec.skill_advice(
            "ticket-ship", match=0.91, rank=1, session_id="s", turn_id=7,
            event_id="advice-7", prompt=SECRET,
        )
        self.rec.skill_view_loaded(
            "ticket-ship", success=True, content_chars=9000, session_id="s", turn_id=7,
            call_id="load-7", result=SECRET,
        )
        self.rec.tool_outcome(
            "error", tool_kind="other", tool_name="terminal", duration_ms=1250,
            error_type="TimeoutError", session_id="s", turn_id=7, call_id="tool-1",
            error_message=SECRET,
        )
        self.rec.tool_outcome(
            "success", tool_kind="other", tool_name="terminal", duration_ms=20,
            session_id="s", turn_id=7, call_id="tool-2",
        )
        self.rec.api_attempt(
            provider="openai-codex", model="gpt-test", retry_count=1,
            session_id="s", turn_id=7, request_id="request-7", base_url=URL,
        )
        self.rec.api_outcome(
            "success", provider="openai-codex", model="gpt-test", duration_ms=2300,
            input_tokens=10, output_tokens=2000, cache_read_tokens=5000,
            cache_write_tokens=0, reasoning_tokens=None,
            session_id="s", turn_id=7, request_id="request-7", response=SECRET,
        )

        rows = self.rows()
        self.assertTrue(all(row["turn_id"] is not None for row in rows))
        summary = self.rec.rollup()
        self.assertEqual(summary["decisions"]["strategies"], {"one-stage": 1})
        self.assertEqual(summary["subsequent_calls"], 1)
        self.assertEqual(summary["loaded_context_buckets"], {"4k-16k": 1})
        self.assertEqual(summary["token_buckets"]["cache_read"], {"4k-16k": 1})
        self.assertEqual(summary["token_buckets"]["cache_write"], {"0": 1})
        self.assertEqual(summary["token_buckets"]["reasoning"], {"unknown": 1})
        self.assert_private()

    def test_explicit_feedback_only_and_invalid_enums_fail_closed(self):
        self.rec.feedback("accepted", session_id="s", turn_id="t", task_id="task", event_id="f")
        self.rec.feedback("accepted", session_id="s", turn_id="t", task_id="task", event_id="f")
        self.assertEqual(self.rec.rollup()["feedback"], {"accepted": 1})
        with self.assertRaises(ValueError):
            self.rec.feedback(SECRET)
        with self.assertRaises(ValueError):
            self.rec.tool_outcome(SECRET)
        with self.assertRaises(ValueError):
            self.rec.api_outcome("success", input_tokens=-1)
        self.assertEqual(len(self.rows()), 1)

    def test_load_before_advice_is_not_counted_as_conversion(self):
        with mock.patch.object(telemetry.time, "time", return_value=10000):
            self.rec.skill_view_loaded("ticket-ship", success=True, session_id="s", turn_id="t", call_id="old")
        with mock.patch.object(telemetry.time, "time", return_value=10001):
            self.rec.skill_advice("ticket-ship", session_id="s", turn_id="t", request_id="r")
        with mock.patch.object(telemetry.time, "time", return_value=10002):
            self.assertEqual(self.rec.rollup()["advice_to_load"]["matched"], 0)

    def test_parallel_turns_and_profile_isolation(self):
        other = telemetry.EffectivenessTelemetry(Path(self.temp.name) / "profiles" / "beta",
                                                  profile_id="beta", approved_skills={"ticket-ship"})
        def emit(item):
            rec, i = item
            rec.skill_advice("ticket-ship", session_id="s", turn_id=f"t{i}", request_id=f"r{i}")
            rec.skill_view_loaded("ticket-ship", success=True, session_id="s", turn_id=f"t{i}", call_id=f"v{i}")
            rec.turn_outcome("completed", session_id="s", turn_id=f"t{i}")
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(emit, [(self.rec if i % 2 else other, i) for i in range(80)]))
        for rec in (self.rec, other):
            self.assertEqual(len(self.rows(rec)), 120)
            self.assertEqual(rec.rollup()["advice_to_load"]["matched"], 40)
            self.assert_private(rec)
        self.assertNotEqual(self.rows()[0]["session_id"], self.rows(other)[0]["session_id"])

    def test_rotation_retention_caps_and_reopen_dedup(self):
        rec = telemetry.EffectivenessTelemetry(self.home, profile_id="alpha", approved_skills=set(),
                                               max_bytes=1100, max_files=2, retention_seconds=3600)
        rec.tool_outcome("success", call_id="original", event_id="one")
        reopened = telemetry.EffectivenessTelemetry(self.home, profile_id="alpha", approved_skills=set(),
                                                    max_bytes=1100, max_files=2, retention_seconds=3600)
        reopened.tool_outcome("success", call_id="original", event_id="one")
        self.assertEqual(len(self.rows(rec)), 1)
        for i in range(35):
            rec.tool_outcome("success", call_id=f"call{i}", event_id=f"event{i}")
        files = [p for p in (rec.home / "jev" / "effectiveness").iterdir() if p.name.startswith("events.jsonl")]
        self.assertLessEqual(len(files), 2)
        self.assertTrue(all(p.stat().st_size <= 1100 for p in files))
        self.assertLessEqual(len(self.rows(rec)), 35)
        self.assert_private(rec)

    def test_mixed_age_segment_is_pruned_even_when_recently_appended(self):
        rec = telemetry.EffectivenessTelemetry(self.home, profile_id="alpha", approved_skills=set(),
                                               retention_seconds=3)
        with mock.patch.object(telemetry.time, "time", return_value=10000):
            rec.tool_outcome("success", call_id="old")
        with mock.patch.object(telemetry.time, "time", return_value=10002):
            rec.tool_outcome("success", call_id="new")
        self.assertEqual(len(self.rows(rec)), 2)
        with mock.patch.object(telemetry.time, "time", return_value=10004):
            self.assertEqual(rec.rollup()["tool_outcomes"], {"success": 1})
        self.assertEqual(len(self.rows(rec)), 1)

    def test_linked_segment_fails_before_read_or_mutation(self):
        self.rec.tool_outcome("success", call_id="safe")
        before = self.rec._active.read_bytes()
        with mock.patch.object(
            telemetry,
            "_is_link",
            side_effect=lambda path: Path(path) == self.rec._active,
            create=True,
        ):
            with self.assertRaises(ValueError):
                self.rec.rollup()
        self.assertEqual(self.rec._active.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
