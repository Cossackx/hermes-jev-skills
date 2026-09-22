#!/usr/bin/env python3
"""Read and apply Hermes model-routing config across the default profile and every
profile, with comment-preserving edits, backups, and verified read-back.

Design rules:
  * Never rewrite the whole YAML file. Edit only the exact scalar lines, so
    comments, ordering, and unrelated settings survive untouched.
  * Read values back with a real YAML parse after writing; a write that does not
    verify is reported as failed.
  * Reject anything that could inject YAML (newlines / odd characters).
  * Applying never restarts a gateway and never touches a remote. It returns a
    receipt that says a reload is required for the change to take effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------- use cases

#: Auxiliary task slots Hermes routes independently, with human labels.
AUX_SLOTS: list[tuple[str, str, str]] = [
    ("compression", "Compression", "context"),
    ("flush_memories", "Memory flush", "context"),
    ("session_search", "Session search", "context"),
    ("curator", "Curator", "context"),
    ("skills_hub", "Skills hub", "context"),
    ("title_generation", "Title generation", "housekeeping"),
    ("tts_audio_tags", "Audio tags", "housekeeping"),
    ("profile_describer", "Profile describer", "housekeeping"),
    ("web_extract", "Web extract", "knowledge"),
    ("vision", "Vision", "knowledge"),
    ("triage_specifier", "Triage / specifier", "judgment"),
    ("approval", "Approval judgment", "judgment"),
    ("mcp", "MCP calling", "judgment"),
    ("monitor", "Monitor", "judgment"),
    ("kanban_decomposer", "Kanban decomposer", "judgment"),
    ("background_review", "Background review", "judgment"),
]
SLOT_KEYS = [s[0] for s in AUX_SLOTS]
SLOT_LABELS = {s[0]: s[1] for s in AUX_SLOTS}
SLOT_GROUPS = {s[0]: s[2] for s in AUX_SLOTS}

MAIN_LABEL = "Main model (user-facing responses)"

JEV_MODE = {
    "key": "jev/state.json",
    "installed": True,
    "intent": "Jev routing is advisory: it can recommend a configured same-provider model and support a "
              "separate fresh CLI launch. It never switches the active Hermes gateway model.",
    "desired_default": "off",
    "note": "The switch gates the advisory routing tool and fresh-launch workflow. Inside Hermes the same "
            "switch is /jev routing on|off; neither setting changes an active conversation's model.",
}

_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9._\-]*$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9._\-/:@+]*$")


def _check_safe(value: str, kind: str) -> str:
    value = (value or "").strip()
    if "\n" in value or "\r" in value:
        raise ValueError(f"{kind} must not contain newlines")
    pat = _SAFE_PROVIDER if kind == "provider" else _SAFE_MODEL
    if not pat.match(value):
        raise ValueError(f"{kind} contains unsupported characters: {value!r}")
    return value


@dataclass
class Target:
    """One configurable config file."""

    name: str
    path: str
    kind: str  # 'default' | 'profile'

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)


def discover_targets(hermes_home: str) -> list[Target]:
    """Default profile first, then every profile dir holding a config.yaml."""
    targets: list[Target] = []
    root_cfg = os.path.join(hermes_home, "config.yaml")
    if os.path.isfile(root_cfg):
        targets.append(Target("default", root_cfg, "default"))
    pdir = os.path.join(hermes_home, "profiles")
    if os.path.isdir(pdir):
        for name in sorted(os.listdir(pdir)):
            cfg = os.path.join(pdir, name, "config.yaml")
            if os.path.isfile(cfg):
                targets.append(Target(name, cfg, "profile"))
    return targets


# ------------------------------------------------------------- text editing


def _bounds(lines: list[str], section: str, indent: int = 0) -> Optional[tuple[int, int]]:
    """Line bounds of a block starting at `section` with given indent."""
    start = None
    prefix = " " * indent
    for i, line in enumerate(lines):
        if re.match(r"^%s%s\s*:" % (re.escape(prefix), re.escape(section)), line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        if ind <= indent:
            end = j
            break
    return (start, end)


def _find_scalar(lines: list[str], bnd: tuple[int, int], key: str, indent: int) -> Optional[int]:
    for i in range(bnd[0] + 1, bnd[1]):
        if re.match(r"^%s%s\s*:" % (" " * indent, re.escape(key)), lines[i]):
            return i
    return None


def _fmt(value: str) -> str:
    if value == "":
        return "''"
    return value


def _set_scalar(text: str, section: str, key: str, value: str, *, sub: Optional[str] = None,
                section_indent: int = 0) -> str:
    """Set `section[.sub].key` to value, preserving every other character."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"

    top = _bounds(lines, section, section_indent)
    if top is None:
        # Append a whole new block.
        body = "%s%s:\n" % (" " * section_indent, section)
        if sub:
            body += "%s  %s:\n" % (" " * section_indent, sub)
            body += "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value))
        else:
            body += "%s  %s: %s\n" % (" " * section_indent, key, _fmt(value))
        return text + ("" if text.endswith("\n") or not text else "\n") + body

    if sub is None:
        idx = _find_scalar(lines, top, key, section_indent + 2)
        if idx is None:
            insert_at = top[1]
            line = "%s  %s: %s\n" % (" " * section_indent, key, _fmt(value))
            lines.insert(insert_at, line)
        else:
            lines[idx] = "%s  %s: %s\n" % (" " * section_indent, key, _fmt(value))
        return "".join(lines)

    # two-level: section -> sub -> key
    sub_bnd = None
    prefix = " " * (section_indent + 2)
    for i in range(top[0] + 1, top[1]):
        if re.match(r"^%s%s\s*:" % (re.escape(prefix), re.escape(sub)), lines[i]):
            sub_bnd = (i, top[1])
            break
        ind = len(lines[i]) - len(lines[i].lstrip(" ")) if lines[i].strip() else None
        if ind is not None and ind <= section_indent + 2:
            sub_bnd = None
    if sub_bnd is None:
        insert_at = top[1]
        lines.insert(insert_at, "%s  %s:\n" % (" " * section_indent, sub))
        lines.insert(insert_at + 1, "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value)))
        return "".join(lines)

    # bound the sub block properly
    end = sub_bnd[1]
    for j in range(sub_bnd[0] + 1, sub_bnd[1]):
        if not lines[j].strip():
            continue
        ind = len(lines[j]) - len(lines[j].lstrip(" "))
        if ind <= section_indent + 2:
            end = j
            break
    sub_bnd = (sub_bnd[0], end)

    idx = _find_scalar(lines, sub_bnd, key, section_indent + 4)
    if idx is None:
        lines.insert(sub_bnd[1], "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value)))
    else:
        lines[idx] = "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value))
    return "".join(lines)


# ------------------------------------------------------------------- reading


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read_config(path: str) -> dict[str, Any]:
    """Current main + auxiliary routing for one config file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    model = data.get("model") or {}
    aux = data.get("auxiliary") or {}
    slots: dict[str, dict[str, str]] = {}
    for key in SLOT_KEYS:
        entry = aux.get(key) or {}
        if isinstance(entry, dict) and (entry.get("provider") or entry.get("model")):
            slots[key] = {
                "provider": str(entry.get("provider") or ""),
                "model": str(entry.get("model") or ""),
            }
    return {
        "main": {
            "provider": str(model.get("provider") or ""),
            "model": str(model.get("default") or ""),
            "base_url": str(model.get("base_url") or ""),
        },
        "slots": slots,
        "fallback_providers": data.get("fallback_providers") or [],
        "jev": _jev_state(data),
    }


def _jev_state(data: dict[str, Any]) -> dict[str, Any]:
    routing = data.get("model_routing") or {}
    jev = (routing.get("jev") or {}) if isinstance(routing, dict) else {}
    mode = str(jev.get("mode") or "") if isinstance(jev, dict) else ""
    return {"mode": mode or "not-configured", "key": JEV_MODE["key"]}


def snapshot(hermes_home: str) -> dict[str, Any]:
    targets = discover_targets(hermes_home)
    profiles = []
    for t in targets:
        try:
            cfg = read_config(t.path)
            err = None
        except Exception as exc:  # a broken profile must not blank the dashboard
            cfg = {"main": {"provider": "", "model": ""}, "slots": {}, "jev": {"mode": "unknown"}}
            err = f"{type(exc).__name__}: {exc}"
        profiles.append({
            "name": t.name,
            "kind": t.kind,
            "path": t.path,
            "main": cfg["main"],
            "slots": cfg["slots"],
            "jev": cfg.get("jev", {}),
            "error": err,
        })
    return {
        "hermes_home": hermes_home,
        "generated_at": time.time(),
        "profiles": profiles,
        "use_cases": [
            {"key": "__main__", "label": MAIN_LABEL, "group": "main"},
            *[{"key": k, "label": SLOT_LABELS[k], "group": SLOT_GROUPS[k]} for k in SLOT_KEYS],
        ],
        "jev_mode": JEV_MODE,
    }


# models.dev provider id -> the provider name Hermes uses in config.yaml, where they differ.
_HERMES_PROVIDER_NAME = {"xai": "xai", "google": "gemini", "openai": "openai-codex", "github-copilot": "copilot",
                         "kimi-for-coding": "kimi-coding", "moonshotai": "moonshot"}
_CATALOG_CACHE: dict[str, Any] = {}


def _accessible_models(hermes_home: str) -> list[tuple[str, str]]:
    """(hermes provider, model id) for every provider this install holds a key or login for.

    Only the NAMES of keys are read. The models.dev cache is JSON; parsing it as YAML took ~8 s.
    """
    cache = os.path.join(hermes_home, "models_dev_cache.json")
    try:
        stamp = os.path.getmtime(cache)
    except OSError:
        return []
    if _CATALOG_CACHE.get("stamp") == stamp:
        return _CATALOG_CACHE["rows"]
    try:
        with open(cache, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return []
    have: set[str] = set()
    try:
        with open(os.path.join(hermes_home, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                name, sep, value = line.partition("=")
                if sep and value.strip():
                    have.add(name.strip())
    except OSError:
        pass
    logins: set[str] = set()
    try:
        with open(os.path.join(hermes_home, "auth.json"), "r", encoding="utf-8") as fh:
            auth = json.load(fh)
        for section in ("credential_pool", "providers"):
            if isinstance(auth.get(section), dict):
                logins.update(auth[section])
    except (OSError, ValueError):
        pass
    reverse = {v: k for k, v in _HERMES_PROVIDER_NAME.items()}
    login_ids = {reverse.get(name, name) for name in logins} | {"xai" if "xai-oauth" in logins else ""}
    rows: list[tuple[str, str]] = []
    for provider_id, entry in (blob.items() if isinstance(blob, dict) else []):
        if not isinstance(entry, dict) or provider_id == "cloudflare-ai-gateway":
            continue
        if provider_id not in login_ids and not any(n in have for n in (entry.get("env") or [])):
            continue
        name = _HERMES_PROVIDER_NAME.get(provider_id, provider_id)
        for mid, spec in (entry.get("models") or {}).items():
            if isinstance(spec, dict) and spec.get("status") not in ("deprecated", "retired"):
                rows.append((name, mid))
    _CATALOG_CACHE.update(stamp=stamp, rows=rows)
    return rows


def model_catalog(hermes_home: str, limit: int = 4000) -> list[dict[str, str]]:
    """Model ids worth offering: current values, then any local model cache."""
    seen: dict[str, str] = {}

    def add(mid: str, provider: str) -> None:
        mid = (mid or "").strip()
        if not mid or len(seen) >= limit:
            return
        seen.setdefault(mid, provider or "")

    for t in discover_targets(hermes_home):
        try:
            cfg = read_config(t.path)
        except Exception:
            continue
        add(cfg["main"]["model"], cfg["main"]["provider"])
        for slot in cfg["slots"].values():
            add(slot.get("model", ""), slot.get("provider", ""))

    for provider, mid in _accessible_models(hermes_home):
        add(mid, provider)

    return [{"id": k, "provider": v} for k, v in sorted(seen.items())]


# ------------------------------------------------------------------- writing


def plan(config_path: str, changes: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    """Compute before/after rows for the UI preview without writing anything."""
    before = read_config(config_path)
    rows: list[dict[str, str]] = []
    main = changes.get("__main__")
    if main:
        for field_name, key, old in (
            ("provider", "provider", before["main"]["provider"]),
            ("model", "default", before["main"]["model"]),
        ):
            new = _check_safe(main.get(field_name, old), field_name)
            if new != old:
                rows.append({"scope": "__main__", "field": key, "before": old, "after": new})
    for slot_key, spec in changes.items():
        if slot_key == "__main__":
            continue
        if slot_key not in SLOT_KEYS:
            raise ValueError(f"unknown use case: {slot_key}")
        cur = before["slots"].get(slot_key, {"provider": "", "model": ""})
        for field_name in ("provider", "model"):
            if field_name not in spec:
                continue
            new = _check_safe(spec[field_name], field_name)
            old = cur.get(field_name, "")
            if new != old:
                rows.append({"scope": slot_key, "field": field_name, "before": old, "after": new})
    return rows


def apply_changes(hermes_home: str, config_path: str, changes: dict[str, dict[str, str]],
                  backup_root: Optional[str] = None) -> dict[str, Any]:
    """Apply routing changes to one config file, with backup + verified read-back."""
    rp = os.path.realpath(config_path)
    allowed = {os.path.realpath(t.path) for t in discover_targets(hermes_home)}
    if rp not in allowed:
        raise ValueError("config path is not a live Hermes config")

    rows = plan(rp, changes)
    if not rows:
        return {"ok": True, "changed": 0, "rows": [], "backup": None,
                "verified": True, "reload_required": False, "mismatches": [],
                "message": "nothing to change"}

    before_hash = _sha256(rp)
    backup_root = backup_root or os.path.join(hermes_home, "backups", "model-routing-dashboard")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bdir = os.path.join(backup_root, stamp)
    os.makedirs(bdir, exist_ok=True)
    backup = os.path.join(bdir, os.path.basename(rp) + "." + hashlib.sha256(rp.encode()).hexdigest()[:8])
    shutil.copy2(rp, backup)

    with open(rp, "r", encoding="utf-8") as fh:
        text = fh.read()

    main = changes.get("__main__")
    if main:
        if "provider" in main:
            text = _set_scalar(text, "model", "provider", _check_safe(main["provider"], "provider"))
        if "model" in main:
            text = _set_scalar(text, "model", "default", _check_safe(main["model"], "model"))
    for slot_key, spec in changes.items():
        if slot_key == "__main__":
            continue
        if slot_key not in SLOT_KEYS:
            raise ValueError(f"unknown use case: {slot_key}")
        if "provider" in spec:
            text = _set_scalar(text, "auxiliary", "provider", _check_safe(spec["provider"], "provider"),
                               sub=slot_key)
        if "model" in spec:
            text = _set_scalar(text, "auxiliary", "model", _check_safe(spec["model"], "model"),
                               sub=slot_key)

    tmp = rp + ".dashboard-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, rp)

    after = read_config(rp)  # raises if we produced invalid YAML
    mismatches = []
    for row in rows:
        if row["scope"] == "__main__":
            got = after["main"]["provider" if row["field"] == "provider" else "model"]
        else:
            got = after["slots"].get(row["scope"], {}).get(row["field"], "")
        if got != row["after"]:
            mismatches.append({**row, "read_back": got})

    return {
        "ok": not mismatches,
        "changed": len(rows),
        "rows": rows,
        "backup": backup,
        "before_sha256": before_hash,
        "after_sha256": _sha256(rp),
        "verified": not mismatches,
        "mismatches": mismatches,
        "reload_required": bool(rows),
        "message": ("applied and verified; gateway restart required for running sessions"
                    if not mismatches else "write did not verify — restore from backup"),
    }


# ------------------------------------------------------------------ jev live


def _jev_homes(hermes_home: str) -> list[tuple[str, str]]:
    homes = [("default", hermes_home)]
    pdir = os.path.join(hermes_home, "profiles")
    if os.path.isdir(pdir):
        homes += [(n, os.path.join(pdir, n)) for n in sorted(os.listdir(pdir))
                  if os.path.isdir(os.path.join(pdir, n))]
    return homes


def _tail_lines(path: str, max_bytes: int = 96_000) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    return lines[1:] if size > max_bytes else lines  # first line may be cut in half


def jev_live(hermes_home: str, since: float = 0.0, limit: int = 200) -> dict[str, Any]:
    """Recent Jev decisions across every profile, newest first.

    The hermes-jev plugin writes decisions only (tier, model, confidence, latency)
    to <profile home>/logs/jev-decisions.jsonl; prompt text is never in that file.
    """
    events: list[dict[str, Any]] = []
    switches: dict[str, dict[str, str]] = {}
    for name, home in _jev_homes(hermes_home):
        try:
            with open(os.path.join(home, "jev", "state.json"), "r", encoding="utf-8") as fh:
                state = json.load(fh)
            if isinstance(state, dict) and state:
                switches[name] = {k: str(v) for k, v in state.items()}
        except (OSError, ValueError):
            pass
        for line in _tail_lines(os.path.join(home, "logs", "jev-decisions.jsonl")):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and float(row.get("ts") or 0) > since:
                row.setdefault("profile", name)
                events.append(row)
    events.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    routes = [e for e in events if e.get("kind") == "route"]
    by_model: dict[str, int] = {}
    for e in routes:
        if e.get("model"):
            by_model[str(e["model"])] = by_model.get(str(e["model"]), 0) + 1
    return {"now": time.time(), "events": events[:limit], "switches": switches,
            "plugin_installed": os.path.isdir(os.path.join(hermes_home, "plugins", "hermes-jev")),
            "by_model": sorted(by_model.items(), key=lambda kv: -kv[1])[:12]}


def _jev_effectiveness_rollup(rows: list[dict[str, Any]]) -> dict[str, Any]:
    events = Counter(str(row.get("event") or "unknown") for row in rows)
    advised = [row for row in rows if row.get("event") == "skill_advice"]
    loaded = [row for row in rows if row.get("event") == "skill_view_loaded"]

    def join(row: dict[str, Any]) -> tuple[str, str, str, str] | None:
        values = (row.get("_profile_name"), row.get("session_id"), row.get("turn_id"), row.get("skill"))
        return tuple(str(value) for value in values) if all(values) else None

    latest_load: dict[tuple[str, str, str, str], float] = {}
    for row in loaded:
        key = join(row)
        if key is not None:
            latest_load[key] = max(latest_load.get(key, 0.0), float(row.get("ts") or 0.0))
    decisions = [row for row in rows if row.get("event") == "decision"]
    tool_rows = [row for row in rows if row.get("event") == "tool_outcome"]
    tool_groups = Counter(
        (row.get("_profile_name"), row.get("session_id"), row.get("turn_id"), row.get("tool_name"))
        for row in tool_rows if row.get("session_id") and row.get("turn_id") and row.get("tool_name")
    )
    missing = Counter(
        name for row in rows for name in ("session_id", "turn_id", "task_id", "request_id", "call_id")
        if row.get(name) is None
    )
    api_rows = [row for row in rows if row.get("event") in ("api_attempt", "api_outcome")]
    api_outcomes = [row for row in rows if row.get("event") == "api_outcome"]
    return {
        "schema": "jev.effectiveness.v1", "total_events": len(rows), "events": dict(events),
        "coverage": {
            "joinable_advice": sum(join(row) is not None for row in advised),
            "joinable_loads": sum(join(row) is not None for row in loaded),
            "unjoined_events": sum(row.get("join_status") == "unjoined" for row in rows),
        },
        "missing_ids": dict(missing),
        "advice_to_load": {
            "advised": len(advised), "loaded": len(loaded),
            "matched": sum(latest_load.get(join(row), -1) >= float(row.get("ts") or 0)
                           for row in advised if join(row) is not None),
            "unjoinable": sum(join(row) is None for row in advised),
        },
        "decisions": {
            "total": len(decisions),
            "statuses": dict(Counter(str(row.get("outcome") or "unknown") for row in decisions)),
            "strategies": dict(Counter(str(row.get("strategy") or "unknown") for row in decisions)),
            "modes": dict(Counter(str(row.get("mode") or "unknown") for row in decisions)),
            "candidate_counts": dict(Counter(str(row.get("candidate_count", "unknown")) for row in decisions)),
            "latency_buckets": dict(Counter(str(row.get("latency_bucket") or "unknown") for row in decisions)),
            "request_bytes_buckets": dict(Counter(str(row.get("request_bytes_bucket") or "unknown") for row in decisions)),
        },
        "tool_outcomes": dict(Counter(str(row.get("outcome") or "unknown") for row in tool_rows)),
        "subsequent_calls": sum(max(0, count - 1) for count in tool_groups.values()),
        "loaded_context_buckets": dict(Counter(str(row.get("content_bucket") or "unknown") for row in loaded)),
        "api": dict(Counter("attempt" if row.get("event") == "api_attempt" else str(row.get("outcome") or "unknown")
                            for row in api_rows)),
        "api_routes": dict(Counter(f"{row.get('provider', 'unknown')}:{row.get('model', 'unknown')}"
                                   for row in api_outcomes)),
        "turn_outcomes": dict(Counter(str(row.get("outcome") or "unknown") for row in rows
                                      if row.get("event") == "turn_outcome")),
        "feedback": dict(Counter(str(row.get("outcome") or "unknown") for row in rows
                                 if row.get("event") == "feedback")),
        "token_buckets": {
            direction: dict(Counter(str(row.get(f"{direction}_bucket") or "unknown") for row in api_outcomes))
            for direction in ("input", "output", "cache_read", "cache_write", "reasoning")
        },
    }


def jev_effectiveness(hermes_home: str, since: float = 0.0, profile: str | None = None) -> dict[str, Any]:
    """Aggregate profile-local Jev effectiveness events without returning correlation IDs or raw rows."""
    homes = dict(_jev_homes(hermes_home))
    if profile is not None and profile not in homes:
        raise ValueError(f"unknown profile: {profile!r}")
    selected = [(profile, homes[profile])] if profile is not None else list(homes.items())
    all_rows: list[dict[str, Any]] = []
    by_profile: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, Any]] = {}
    for name, home in selected:
        directory = os.path.join(home, "jev", "effectiveness")
        rows: list[dict[str, Any]] = []
        bytes_read = 0
        segments_read = 0
        rejected_segments = 0
        omitted_segments = 0
        try:
            paths = [os.path.join(directory, item) for item in os.listdir(directory)
                     if re.fullmatch(r"events\.jsonl(?:\.\d+)?", item)]
        except OSError:
            paths = []
        for path in sorted(paths):
            fd = None
            try:
                if os.path.islink(path):
                    rejected_segments += 1
                    continue
                base = os.path.realpath(directory)
                resolved = os.path.realpath(path)
                if os.path.commonpath((base, resolved)) != base:
                    rejected_segments += 1
                    continue
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    rejected_segments += 1
                    continue
                if info.st_size > 16_000_000 or bytes_read + info.st_size > 64_000_000:
                    omitted_segments += 1
                    continue
                with os.fdopen(fd, "rb") as stream:
                    fd = None
                    data = stream.read(16_000_001)
                if len(data) > 16_000_000:
                    omitted_segments += 1
                    continue
                lines = data.decode("utf-8", errors="replace").splitlines()
                bytes_read += len(data)
                segments_read += 1
            except (OSError, ValueError):
                rejected_segments += 1
                continue
            finally:
                if fd is not None:
                    os.close(fd)
            for line in lines:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if (isinstance(row, dict) and row.get("schema") == "jev.effectiveness.v1"
                        and isinstance(row.get("ts"), (int, float)) and float(row["ts"]) > since):
                    rows.append({**row, "_profile_name": name})
        by_profile[name] = _jev_effectiveness_rollup(rows)
        sources[name] = {
            "complete": omitted_segments == 0 and rejected_segments == 0,
            "segments_read": segments_read,
            "bytes_read": bytes_read,
            "omitted_segments": omitted_segments,
            "rejected_segments": rejected_segments,
        }
        all_rows.extend(rows)
    return {"now": time.time(), "since": since, "profiles": by_profile, "sources": sources,
            "overall": _jev_effectiveness_rollup(all_rows)}


JEV_SWITCHES = {
    "routing": ("off", "on"),
    "skills": ("off", "shadow", "on"),
    "selector": ("control", "fast", "auto"),
    "notice": ("off", "on"),
}
JEV_SWITCH_DEFAULTS = {name: ("control" if name == "selector" else "off") for name in JEV_SWITCHES}


def jev_switch_state(hermes_home: str) -> dict[str, Any]:
    """Report each profile's own state; Hermes profiles do not inherit default state."""
    def read(home: str) -> dict[str, str]:
        try:
            with open(os.path.join(home, "jev", "state.json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {
                k: str(v) for k, v in data.items()
                if k in JEV_SWITCHES and str(v) in JEV_SWITCHES[k]
            } if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    profiles = {}
    for name, home in _jev_homes(hermes_home):
        own = read(home)
        profiles[name] = {
            "own": own,
            "effective": {k: own.get(k, JEV_SWITCH_DEFAULTS[k]) for k in JEV_SWITCHES},
        }
    default_state = profiles.get("default", {"effective": dict(JEV_SWITCH_DEFAULTS)})["effective"]
    return {"shared": dict(default_state), "profiles": profiles,
            "plugin_installed": os.path.isdir(os.path.join(hermes_home, "plugins", "hermes-jev"))}


def set_jev_switch(hermes_home: str, scope: str, name: str, value: str) -> dict[str, Any]:
    """Scope ``__all__`` writes every profile home because profiles do not inherit state.

    The hermes-jev plugin reads these files on every turn, so this takes effect at once: no restart.
    """
    if name not in JEV_SWITCHES or value not in JEV_SWITCHES[name]:
        raise ValueError(f"{name} must be one of {JEV_SWITCHES.get(name)}")
    homes = dict(_jev_homes(hermes_home))
    if scope != "__all__" and scope not in homes:
        raise ValueError(f"unknown profile: {scope!r}")

    def write(home: str, mutate) -> None:
        path = os.path.join(home, "jev", "state.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        before = dict(data)
        mutate(data)
        if data == before:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".dashboard-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)

    if scope == "__all__":
        for home in homes.values():
            write(home, lambda d: d.__setitem__(name, value))
    else:
        write(homes[scope], lambda d: d.__setitem__(name, value))
    return {"ok": True, "scope": scope, "switch": name, "value": value, **jev_switch_state(hermes_home)}
