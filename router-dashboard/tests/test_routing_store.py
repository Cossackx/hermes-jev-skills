"""Tests for routing_store: safe, comment-preserving, verified config edits."""
import os
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing_store as rs  # noqa: E402

FIXTURE = """\
# Hermes profile config (fixture)
model:
  default: deepseek/deepseek-v4.1-flash
  provider: openrouter
  context_length: 1048576
  aliases:
    qwen27: local-qwen/pocketaihub-qwen3.8-27b

auxiliary:
  compression:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
    timeout: 60
  vision:
    provider: openrouter
    model: qwen/qwen3-vl-235b-a22b-instruct
  approval:
    provider: auto
    model: ''

agent:
  max_turns: 120
plugins:
  enabled:
  - resource-lifecycle
"""


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "profiles", "wiki"), exist_ok=True)
        self.root_cfg = os.path.join(self.home, "config.yaml")
        self.wiki_cfg = os.path.join(self.home, "profiles", "wiki", "config.yaml")
        for p in (self.root_cfg, self.wiki_cfg):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_targets_default_first(self):
        names = [t.name for t in rs.discover_targets(self.home)]
        self.assertEqual(names, ["default", "wiki"])

    def test_read_config_reports_main_and_slots(self):
        cfg = rs.read_config(self.root_cfg)
        self.assertEqual(cfg["main"]["provider"], "openrouter")
        self.assertEqual(cfg["main"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(cfg["slots"]["compression"]["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(cfg["slots"]["approval"]["provider"], "auto")

    def test_edit_preserves_comments_order_and_unrelated_keys(self):
        text = Path(self.root_cfg).read_text(encoding="utf-8")
        out = rs._set_scalar(text, "model", "default", "z-ai/glm-5.3")
        self.assertIn("# Hermes profile config (fixture)", out)
        self.assertIn("    qwen27: local-qwen/pocketaihub-qwen3.8-27b", out)
        self.assertIn("  default: z-ai/glm-5.3\n", out)
        self.assertNotIn("deepseek/deepseek-v4.1-flash", out)
        self.assertIn("agent:\n  max_turns: 120", out)

    def test_edit_nested_slot_scalar(self):
        text = Path(self.root_cfg).read_text(encoding="utf-8")
        out = rs._set_scalar(text, "auxiliary", "model", "google/gemini-2.5-flash", sub="compression")
        self.assertIn("  compression:\n    provider: openrouter\n    model: google/gemini-2.5-flash\n", out)
        # untouched sibling
        self.assertIn("    model: qwen/qwen3-vl-235b-a22b-instruct", out)

    def test_edit_adds_missing_slot_without_disturbing_others(self):
        text = Path(self.root_cfg).read_text(encoding="utf-8")
        out = rs._set_scalar(text, "auxiliary", "provider", "openrouter", sub="web_extract")
        out = rs._set_scalar(out, "auxiliary", "model", "deepseek/deepseek-v4-flash-0731",
                             sub="web_extract")
        self.assertIn("  web_extract:\n    provider: openrouter\n"
                      "    model: deepseek/deepseek-v4-flash-0731\n", out)
        self.assertIn("  approval:\n    provider: auto\n    model: ''\n", out)

    def test_edit_appends_section_when_absent(self):
        text = "model:\n  provider: openrouter\n  default: x\n"
        out = rs._set_scalar(text, "auxiliary", "model", "m", sub="compression")
        self.assertIn("auxiliary:\n  compression:\n    model: m\n", out)

    def test_plan_reports_diffs_and_rejects_bad_input(self):
        rows = rs.plan(self.root_cfg, {"compression": {"model": "google/gemini-2.5-flash"}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["before"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(rows[0]["after"], "google/gemini-2.5-flash")
        with self.assertRaises(ValueError):
            rs.plan(self.root_cfg, {"not_a_slot": {"model": "m"}})
        with self.assertRaises(ValueError):
            rs.plan(self.root_cfg, {"compression": {"model": "evil\n  injected: true"}})
        with self.assertRaises(ValueError):
            rs.plan(self.root_cfg, {"compression": {"model": "bad'quote"}})

    def test_apply_writes_backs_up_and_verifies(self):
        rec = rs.apply_changes(self.home, self.root_cfg,
                               {"__main__": {"model": "z-ai/glm-5.3", "provider": "openrouter"},
                                "compression": {"model": "google/gemini-2.5-flash"}})
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["changed"], 2)
        self.assertTrue(os.path.isfile(rec["backup"]))
        self.assertTrue(rec["reload_required"])
        after = rs.read_config(self.root_cfg)
        self.assertEqual(after["main"]["model"], "z-ai/glm-5.3")
        self.assertEqual(after["slots"]["compression"]["model"], "google/gemini-2.5-flash")
        # backup holds the pre-change content
        self.assertIn("deepseek/deepseek-v4.1-flash", Path(rec["backup"]).read_text(encoding="utf-8"))

    def test_apply_noop_is_honest(self):
        rec = rs.apply_changes(self.home, self.root_cfg, {"compression": {"provider": "openrouter"}})
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["changed"], 0)
        self.assertFalse(rec["reload_required"])

    def test_apply_refuses_foreign_path(self):
        with self.assertRaises(ValueError):
            rs.apply_changes(self.home, "/etc/passwd", {"compression": {"model": "m"}})

    def test_apply_leaves_valid_yaml_for_every_profile(self):
        for cfg in (self.root_cfg, self.wiki_cfg):
            rec = rs.apply_changes(self.home, cfg, {"web_extract": {"provider": "openrouter",
                                                                   "model": "deepseek/deepseek-v4-flash-0731"}})
            self.assertTrue(rec["verified"], rec)

    def test_snapshot_shape(self):
        snap = rs.snapshot(self.home)
        self.assertEqual([p["name"] for p in snap["profiles"]], ["default", "wiki"])
        keys = [u["key"] for u in snap["use_cases"]]
        self.assertEqual(keys[0], "__main__")
        self.assertIn("compression", keys)
        self.assertEqual(snap["jev_mode"]["desired_default"], "off")
        self.assertIn("intent", snap["jev_mode"])

    def test_model_catalog_includes_configured_ids(self):
        ids = {m["id"] for m in rs.model_catalog(self.home)}
        self.assertIn("deepseek/deepseek-v4.1-flash", ids)
        self.assertIn("qwen/qwen3-vl-235b-a22b-instruct", ids)

    def test_jev_state_reports_not_configured_then_value(self):
        self.assertEqual(rs.read_config(self.root_cfg)["jev"]["mode"], "not-configured")
        rec = rs.apply_changes(self.home, self.root_cfg, {})
        self.assertTrue(rec["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class JevSwitchTests(unittest.TestCase):
    def test_profile_override_and_all_resets_everyone(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("alpha", "beta"):
                os.makedirs(os.path.join(tmp, "profiles", name))
            rs.set_jev_switch(tmp, "__all__", "routing", "off")
            rs.set_jev_switch(tmp, "alpha", "routing", "on")
            state = rs.jev_switch_state(tmp)
            self.assertEqual(state["profiles"]["alpha"]["effective"]["routing"], "on")
            self.assertEqual(state["profiles"]["beta"]["effective"]["routing"], "off")
            with open(os.path.join(tmp, "profiles", "alpha", "jev", "state.json"), "w") as fh:
                json.dump({"routing": "on", "notice": "on"}, fh)
            state = rs.set_jev_switch(tmp, "__all__", "routing", "off")
            self.assertTrue(all(p["effective"]["routing"] == "off" for p in state["profiles"].values()))
            with open(os.path.join(tmp, "profiles", "alpha", "jev", "state.json")) as fh:
                self.assertEqual(json.load(fh), {"routing": "off", "notice": "on"})
            with open(os.path.join(tmp, "profiles", "beta", "jev", "state.json")) as fh:
                self.assertEqual(json.load(fh), {"routing": "off"})

    def test_rejects_bad_values_and_unknown_profiles(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for scope, name, value in (("__all__", "routing", "shadow"), ("__all__", "routing", "maybe"), ("__all__", "model", "on"), ("ghost", "routing", "on")):
                with self.assertRaises(ValueError):
                    rs.set_jev_switch(tmp, scope, name, value)

    def test_invalid_persisted_routing_value_is_not_reported_as_effective(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "jev", "state.json")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"routing": "shadow"}), encoding="utf-8")
            state = rs.jev_switch_state(tmp)
            self.assertEqual(state["shared"]["routing"], "off")
            self.assertEqual(state["profiles"]["default"]["effective"]["routing"], "off")

    def test_named_profile_does_not_inherit_default_profile_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "profiles", "alpha").mkdir(parents=True)
            path = Path(tmp, "jev", "state.json")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"routing": "on"}), encoding="utf-8")
            state = rs.jev_switch_state(tmp)
            self.assertEqual(state["profiles"]["default"]["effective"]["routing"], "on")
            self.assertEqual(state["profiles"]["alpha"]["effective"]["routing"], "off")


class JevEffectivenessTests(unittest.TestCase):
    def test_rollup_correlates_without_exposing_ids_or_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = os.path.join(tmp, "jev", "effectiveness")
            os.makedirs(directory)
            now = time.time()
            base = {"schema": "jev.effectiveness.v1", "ts": now,
                    "profile_id": "profile-secret", "session_id": "session-secret",
                    "turn_id": "turn-secret", "task_id": None, "request_id": None,
                    "call_id": None, "retry_of_call_id": None,
                    "join_status": "joinable", "event_identity": "event-secret"}
            rows = [
                {**base, "event": "decision", "outcome": "ok", "strategy": "one-stage",
                 "mode": "on", "candidate_count": 1, "latency_bucket": "250ms-1s",
                 "request_bytes_bucket": "1k-4k"},
                {**base, "event": "skill_advice", "skill": "ticket-ship"},
                {**base, "event": "skill_view_loaded", "skill": "ticket-ship",
                 "content_bucket": "4k-16k", "ts": now + .1},
                {**base, "event": "tool_outcome", "outcome": "success",
                 "tool_name": "skill_view", "call_id": "call-1"},
                {**base, "event": "turn_outcome", "outcome": "completed"},
            ]
            with open(os.path.join(directory, "events.jsonl"), "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
            result = rs.jev_effectiveness(tmp)
            encoded = json.dumps(result)
            self.assertEqual(result["overall"]["advice_to_load"]["matched"], 1)
            self.assertEqual(result["overall"]["decisions"]["strategies"], {"one-stage": 1})
            self.assertNotIn("session-secret", encoded)
            self.assertNotIn("event-secret", encoded)
            self.assertNotIn("rows", result)

    def test_rollup_reads_complete_bounded_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp, "jev", "effectiveness")
            directory.mkdir(parents=True)
            now = time.time()
            with (directory / "events.jsonl").open("w", encoding="utf-8") as stream:
                for index in range(4000):
                    stream.write(json.dumps({
                        "schema": "jev.effectiveness.v1", "ts": now,
                        "event": "tool_outcome", "outcome": "success",
                        "profile_id": "p", "session_id": "s", "turn_id": "t",
                        "task_id": None, "request_id": None, "call_id": f"c{index}",
                        "retry_of_call_id": None, "join_status": "joinable",
                        "event_identity": f"e{index}", "tool_name": "terminal",
                    }) + "\n")

            result = rs.jev_effectiveness(tmp)
            self.assertEqual(result["overall"]["total_events"], 4000)
            self.assertTrue(result["sources"]["default"]["complete"])

    def test_rollup_ignores_symlinked_segments(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            directory = Path(tmp, "jev", "effectiveness")
            directory.mkdir(parents=True)
            external = Path(outside, "outside.jsonl")
            external.write_text(json.dumps({
                "schema": "jev.effectiveness.v1", "ts": time.time(),
                "event": "turn_outcome", "outcome": "completed",
            }) + "\n", encoding="utf-8")
            try:
                (directory / "events.jsonl.1").symlink_to(external)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")

            result = rs.jev_effectiveness(tmp)
            self.assertEqual(result["overall"]["total_events"], 0)
            self.assertEqual(result["sources"]["default"]["rejected_segments"], 1)

    def test_rollup_rejects_segment_marked_as_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp, "jev", "effectiveness")
            directory.mkdir(parents=True)
            segment = directory / "events.jsonl.1"
            segment.write_text("{}\n", encoding="utf-8")
            real_islink = os.path.islink
            with mock.patch.object(
                rs.os.path,
                "islink",
                side_effect=lambda path: str(path).endswith("events.jsonl.1") or real_islink(path),
            ):
                result = rs.jev_effectiveness(tmp)
            self.assertEqual(result["overall"]["total_events"], 0)
            self.assertEqual(result["sources"]["default"]["rejected_segments"], 1)
