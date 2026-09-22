"""Jev skill advice and explicit bounded decision tools.

No request rewriting, automatic raw-retrieval export, transcript mutation or
GUI execution. Model routing advice and fresh-session CLI routing are separate
from the live Hermes model, which this plugin never silently changes.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .effectiveness import EffectivenessTelemetry
from .jevkit import keystore, privacy, skillpick

_CTX: Any = None
_CATALOG_CACHE_TTL_SECONDS = 5.0
_CATALOG_CACHE: Dict[Any, tuple[float, list[Dict[str, str]]]] = {}
_CATALOG_CACHE_LOCK = threading.RLock()
_EFFECTIVENESS: Dict[str, tuple[frozenset[str], EffectivenessTelemetry]] = {}
_EFFECTIVENESS_LOCK = threading.RLock()
_TRACKED_TURNS: Dict[tuple[str, str], float] = {}
_PROCESS_DIGEST_KEY = secrets.token_bytes(32)
_CONTEXT_ONLY_FOLLOWUPS = {"continue", "make it so", "proceed", "do it"}
_HERMES_CONTROL_PREFIXES = (
    "[ASYNC DELEGATION BATCH COMPLETE",
    "[IMPORTANT: Background process",
    "[CONTEXT COMPACTION",
    "[PRIOR CONTEXT",
    "[Your active task list was preserved across context compression]",
    "[Continuing toward your standing goal]",
)


def _home() -> Path:
    from hermes_constants import get_hermes_home  # type: ignore

    return get_hermes_home()


def _state_path() -> Path:
    return _home() / "jev" / "state.json"


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _state() -> Dict[str, Any]:
    """Read only the active profile's switches."""
    return _read(_state_path())


def _setting(name: str, default: str) -> str:
    """A ``/jev`` switch wins, then plugin config, then the default."""
    value = _state().get(name)
    if value is None:
        try:
            from hermes_cli.config import load_config_readonly
            entries = (load_config_readonly().get("plugins") or {}).get("entries") or {}
            entry = entries.get("hermes-jev") or {}
            value = (entry.get("settings") or {}).get(name)
            if value is None:
                value = (entry.get("config") or {}).get(name)
        except Exception:
            value = None
    return str(value if value is not None else default).lower()


def _profile() -> str:
    home = _home()
    return home.name if home.parent.name == "profiles" else "default"


def _turn_key(session_id: Any, turn_id: Any) -> Optional[tuple[str, str]]:
    if session_id in (None, "") or turn_id in (None, "") or isinstance(turn_id, bool):
        return None
    return _process_digest("session", session_id), _process_digest("turn", turn_id)


def _process_digest(kind: str, value: Any) -> str:
    """Keep raw identifiers and environment values out of process-global caches."""
    return hmac.new(
        _PROCESS_DIGEST_KEY,
        (kind + "\0" + str(value)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _event_id(prefix: str, *stable_parts: Any) -> str:
    """Use a stable host identity when present, otherwise a unique occurrence."""
    if stable_parts and all(part not in (None, "") and not isinstance(part, bool) for part in stable_parts):
        return ":".join([prefix, *(str(part) for part in stable_parts)])
    return f"{prefix}:occ:{secrets.token_hex(16)}"


def _environment_signature() -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, _process_digest("environment:" + name, value))
        for name, value in sorted(os.environ.items())
    )


def _track_turn(session_id: Any, turn_id: Any) -> None:
    key = _turn_key(session_id, turn_id)
    if key is None:
        return
    now = time.monotonic()
    with _EFFECTIVENESS_LOCK:
        _TRACKED_TURNS[key] = now
        stale = [item for item, seen in _TRACKED_TURNS.items() if now - seen > 86400]
        for item in stale:
            _TRACKED_TURNS.pop(item, None)
        while len(_TRACKED_TURNS) > 2048:
            _TRACKED_TURNS.pop(next(iter(_TRACKED_TURNS)))


def _is_tracked(session_id: Any, turn_id: Any) -> bool:
    key = _turn_key(session_id, turn_id)
    with _EFFECTIVENESS_LOCK:
        return key is not None and key in _TRACKED_TURNS


def _effectiveness(catalog: Optional[list[Dict[str, str]]] = None) -> Optional[EffectivenessTelemetry]:
    """Return a profile-local recorder without discovering a catalog from observer hooks."""
    home = _home()
    key = str(home.resolve())
    names = frozenset(item["name"] for item in (catalog or []) if isinstance(item.get("name"), str))
    with _EFFECTIVENESS_LOCK:
        current = _EFFECTIVENESS.get(key)
        if current is not None and (catalog is None or current[0] == names):
            return current[1]
        if catalog is None:
            return None
        try:
            recorder = EffectivenessTelemetry(home, profile_id=_profile(), approved_skills=names)
        except (OSError, ValueError):
            return None
        _EFFECTIVENESS[key] = (names, recorder)
        return recorder


def _record_decision(
    picked: Dict[str, Any], candidates: list[Dict[str, Any]], *, mode: str,
    session_id: Any, turn_id: Any, task_id: Any, catalog: list[Dict[str, str]],
) -> None:
    _track_turn(session_id, turn_id)
    recorder = _effectiveness(catalog)
    if recorder is None:
        return
    ids = {"session_id": session_id, "turn_id": turn_id, "task_id": task_id}
    try:
        status = str(picked.get("status") or "error")
        recorder.decision(
            status if status in {"ok", "fail_open", "defer_native"} else "error",
            strategy=str(picked.get("strategy") or "unknown"), mode=mode,
            candidate_count=len(candidates),
            needs_skill=picked.get("needs_skill"), latency_ms=picked.get("latency_ms"),
            request_bytes=picked.get("request_bytes"), jev_calls=int(picked.get("jev_calls") or 0),
            event_id=_event_id("decision", session_id, turn_id), **ids,
        )
        if mode == "on":
            for rank, skill in enumerate(candidates, 1):
                recorder.skill_advice(
                    skill["name"], match=skill.get("match"), rank=rank,
                    event_id=_event_id("advice", session_id, turn_id, skill["name"]), **ids,
                )
    except (OSError, ValueError, TypeError):
        pass


def _log(entry: Dict[str, Any]) -> None:
    """Append decision metadata only: never prompt text or model output."""
    try:
        path = _home() / "logs" / "jev-decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": round(time.time(), 3), "profile": _profile(), **entry}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _disabled_skills() -> set[str]:
    try:
        from agent.skill_utils import get_disabled_skill_names  # type: ignore

        return set(get_disabled_skill_names())
    except Exception:  # noqa: BLE001
        return set()


def _skill_roots(config: Optional[Dict[str, Any]] = None) -> list[Path]:
    """Mirror Hermes' profile skill roots: local, create_dir, external_dirs."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly  # type: ignore

            config = load_config_readonly() or {}
        except Exception:  # noqa: BLE001
            config = {}
    skills = config.get("skills") if isinstance(config, dict) else None
    skills = skills if isinstance(skills, dict) else {}
    raw_external = skills.get("external_dirs") or []
    if isinstance(raw_external, str):
        raw_external = [raw_external]
    roots = [_home() / "skills"]
    seen = {roots[0].resolve()}
    for entry in [skills.get("create_dir"), *raw_external]:
        if not entry:
            continue
        path = Path(os.path.expandvars(str(entry))).expanduser()
        if not path.is_absolute():
            path = _home() / path
        try:
            path = path.resolve()
        except OSError:
            continue
        if path.is_dir() and path not in seen:
            roots.append(path)
            seen.add(path)
    return [path.resolve() for path in roots]


def _skill_catalog() -> list[Dict[str, str]]:
    """Discover exactly the skills the active Hermes profile can load."""
    try:
        from agent.skill_utils import (  # type: ignore
            get_all_skills_dirs,
            get_disabled_skill_names,
            get_project_skills_dirs,
            iter_skill_index_files,
            parse_frontmatter,
            skill_matches_environment,
            skill_matches_platform,
        )
    except (ImportError, AttributeError):
        native = None
    else:
        native = (
            get_all_skills_dirs, get_disabled_skill_names, get_project_skills_dirs,
            iter_skill_index_files, parse_frontmatter, skill_matches_environment,
            skill_matches_platform,
        )

    try:
        if native is None:
            roots = _skill_roots()
            disabled = _disabled_skills()
        else:
            roots = [*get_project_skills_dirs(), *get_all_skills_dirs()]
            disabled = set(get_disabled_skill_names())
        root_key = tuple(
            (str(Path(root).resolve()), Path(root).stat().st_mtime_ns if Path(root).exists() else None)
            for root in roots
        )
        cache_key = (
            "native" if native is not None else "portable", _profile(), root_key,
            tuple(sorted(disabled)), _environment_signature(), os.name,
        )
        now = time.monotonic()
        with _CATALOG_CACHE_LOCK:
            cached = _CATALOG_CACHE.get(cache_key)
            if cached is not None and now - cached[0] <= _CATALOG_CACHE_TTL_SECONDS:
                return [dict(item) for item in cached[1]]

        if native is None:
            catalog = skillpick.discover(roots, disabled=disabled)
        else:
            catalog = skillpick.discover(
            roots,
            disabled=disabled,
            parser=lambda text: parse_frontmatter(text)[0],
            iter_files=lambda root: iter_skill_index_files(root, "SKILL.md"),
            eligible=lambda fields: skill_matches_platform(dict(fields))
            and skill_matches_environment(dict(fields)),
        )
        with _CATALOG_CACHE_LOCK:
            _CATALOG_CACHE.clear()
            _CATALOG_CACHE[cache_key] = (now, [dict(item) for item in catalog])
        return catalog
    except Exception:
        # A broken host catalog must not broaden eligibility through fallback.
        return []


def _is_context_only_followup(text: str) -> bool:
    normalized = re.sub(r"[^\w]+", " ", text.casefold()).strip()
    return normalized in _CONTEXT_ONLY_FOLLOWUPS


def _on_pre_llm_call(
    session_id: str = "", task_id: Any = None, turn_id: Any = None,
    user_message: Any = "", **_: Any
) -> Any:
    full_text = user_message if isinstance(user_message, str) else json.dumps(user_message, default=str)
    mode = _setting("skills", "off")
    if mode not in ("on", "shadow") or not full_text.strip():
        return None
    if full_text.lstrip().startswith(_HERMES_CONTROL_PREFIXES):
        entry = {
            "kind": "skill",
            "mode": mode,
            "status": "defer_native",
            "reason": "hermes_control_message",
            "picked": [],
            "matches": [],
            "latency_ms": 0,
        }
        _log(entry)
        _record_decision(entry, [], mode=mode, session_id=session_id, turn_id=turn_id,
                         task_id=task_id, catalog=[])
        return None
    if _is_context_only_followup(full_text):
        entry = {
            "kind": "skill",
            "mode": mode,
            "status": "defer_native",
            "reason": "context_only_followup",
            "picked": [],
            "matches": [],
            "latency_ms": 0,
        }
        _log(entry)
        _record_decision(entry, [], mode=mode, session_id=session_id, turn_id=turn_id,
                         task_id=task_id, catalog=[])
        return None
    if privacy.is_sensitive(full_text):
        entry = {
            "kind": "skill",
            "mode": mode,
            "status": "fail_open",
            "reason": "turn looks sensitive; not sent",
            "picked": [],
            "matches": [],
            "latency_ms": 0,
        }
        _log(entry)
        _record_decision(entry, [], mode=mode, session_id=session_id, turn_id=turn_id,
                         task_id=task_id, catalog=[])
        return None

    text = full_text[:6000]
    catalog = _skill_catalog()
    from .decision_tools import _with_profile_key
    selector = _setting("selector", "control")
    if selector not in {"control", "fast", "auto"}:
        selector = "control"
    if selector == "control":
        picked = _with_profile_key(lambda: skillpick.pick(text, catalog, top_k=2))
    else:
        picked = _with_profile_key(
            lambda: skillpick.pick_optimized(text, catalog, top_k=2, strategy=selector)
        )
    candidates = picked.get("skills", [])[:2]
    entry = {
        "kind": "skill",
        "mode": mode,
        "status": picked.get("status"),
        "needs_skill": picked.get("needs_skill"),
        "picked": [skill["name"] for skill in candidates],
        "matches": [skill.get("match") for skill in candidates],
        "latency_ms": picked.get("latency_ms"),
        "strategy": picked.get("strategy"),
        "jev_calls": picked.get("jev_calls"),
        "request_bytes": picked.get("request_bytes"),
        "attempts": picked.get("attempts"),
    }
    _log(entry)
    _record_decision(picked, candidates, mode=mode, session_id=session_id, turn_id=turn_id,
                     task_id=task_id, catalog=catalog)
    if mode != "on" or not candidates:
        return None
    rendered = ", ".join(
        f"`{skill['name']}` (match {skill['match']})" for skill in candidates
    )
    return {
        "context": (
            f"[Jev advisory skill candidates] {rendered}. Load each applicable procedure "
            "with skill_view before starting; ignore candidates that clearly do not apply."
        )
    }


def _observer_recorder(session_id: Any, turn_id: Any) -> Optional[EffectivenessTelemetry]:
    if not _is_tracked(session_id, turn_id):
        return None
    return _effectiveness()


def _on_post_tool_call(
    tool_name: str = "unknown", args: Any = None, result: Any = None,
    session_id: Any = None, task_id: Any = None, turn_id: Any = None,
    tool_call_id: Any = None, duration_ms: Any = None, status: Any = None,
    error_type: Any = None, **_: Any,
) -> None:
    recorder = _observer_recorder(session_id, turn_id)
    if recorder is None:
        return
    normalized = str(status or "").casefold()
    outcome = "success" if normalized in {"ok", "success"} else (
        "refused" if normalized in {"blocked", "refused", "denied"} else
        "error" if normalized in {"error", "failed", "failure"} else "unknown"
    )
    kind = "skill_view" if tool_name == "skill_view" else ("jev" if tool_name.startswith("jev_") else "other")
    ids = {"session_id": session_id, "turn_id": turn_id, "task_id": task_id, "call_id": tool_call_id}
    try:
        recorder.tool_outcome(
            outcome, tool_kind=kind, tool_name=tool_name, duration_ms=duration_ms,
            error_type=error_type, event_id=_event_id("tool", tool_call_id), **ids,
        )
        if tool_name != "skill_view" or outcome != "success" or not isinstance(args, dict) or args.get("file_path"):
            return
        payload = json.loads(result) if isinstance(result, str) else result
        if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("dedup") is True:
            return
        content = payload.get("content")
        if not isinstance(content, str):
            return
        name = payload.get("name") or args.get("name")
        if isinstance(name, str) and name:
            recorder.skill_view_loaded(
                name, success=True, content_chars=len(content),
                event_id=_event_id("load", tool_call_id), **ids,
            )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def _on_pre_api_request(
    session_id: Any = None, task_id: Any = None, turn_id: Any = None,
    api_request_id: Any = None, provider: str = "unknown", model: str = "unknown",
    retry_count: Any = 0, **_: Any,
) -> None:
    recorder = _observer_recorder(session_id, turn_id)
    if recorder is None:
        return
    try:
        retry = retry_count if isinstance(retry_count, int) and not isinstance(retry_count, bool) else 0
        recorder.api_attempt(
            provider=provider, model=model, retry_count=max(0, min(100, retry)),
            session_id=session_id, turn_id=turn_id, task_id=task_id, request_id=api_request_id,
            event_id=_event_id("attempt", api_request_id, retry),
        )
    except (OSError, ValueError, TypeError):
        pass


def _usage_value(usage: Any, *names: str) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _on_post_api_request(
    session_id: Any = None, task_id: Any = None, turn_id: Any = None,
    api_request_id: Any = None, provider: str = "unknown", model: str = "unknown",
    api_duration: Any = None, usage: Any = None, **_: Any,
) -> None:
    recorder = _observer_recorder(session_id, turn_id)
    if recorder is None:
        return
    duration_ms = api_duration * 1000 if isinstance(api_duration, (int, float)) and not isinstance(api_duration, bool) else None
    try:
        recorder.api_outcome(
            "success", provider=provider, model=model, duration_ms=duration_ms,
            input_tokens=_usage_value(usage, "input_tokens", "prompt_tokens"),
            output_tokens=_usage_value(usage, "output_tokens", "completion_tokens"),
            cache_read_tokens=_usage_value(usage, "cache_read_tokens", "cached_tokens"),
            cache_write_tokens=_usage_value(usage, "cache_write_tokens"),
            reasoning_tokens=_usage_value(usage, "reasoning_tokens"),
            session_id=session_id, turn_id=turn_id, task_id=task_id, request_id=api_request_id,
            event_id=_event_id("success", api_request_id),
        )
    except (OSError, ValueError, TypeError):
        pass


def _on_api_request_error(
    session_id: Any = None, task_id: Any = None, turn_id: Any = None,
    api_request_id: Any = None, provider: str = "unknown", model: str = "unknown",
    api_duration: Any = None, retry_count: Any = 0, reason: Any = None, **_: Any,
) -> None:
    recorder = _observer_recorder(session_id, turn_id)
    if recorder is None:
        return
    duration_ms = api_duration * 1000 if isinstance(api_duration, (int, float)) and not isinstance(api_duration, bool) else None
    retry = retry_count if isinstance(retry_count, int) and not isinstance(retry_count, bool) else 0
    outcome = "timeout" if "timeout" in str(reason or "").casefold() else "error"
    try:
        recorder.api_outcome(
            outcome, provider=provider, model=model, duration_ms=duration_ms,
            session_id=session_id, turn_id=turn_id, task_id=task_id, request_id=api_request_id,
            event_id=_event_id("error", api_request_id, retry),
        )
    except (OSError, ValueError, TypeError):
        pass


def _on_session_end(
    session_id: Any = None, task_id: Any = None, turn_id: Any = None,
    completed: Any = False, failed: Any = False, interrupted: Any = False, **_: Any,
) -> None:
    recorder = _observer_recorder(session_id, turn_id)
    key = _turn_key(session_id, turn_id)
    try:
        if recorder is not None:
            outcome = "interrupted" if interrupted else "error" if failed else "completed" if completed else "unknown"
            recorder.turn_outcome(
                outcome, session_id=session_id, turn_id=turn_id, task_id=task_id,
                event_id=_event_id("turn", session_id, turn_id),
            )
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if key is not None:
            with _EFFECTIVENESS_LOCK:
                _TRACKED_TURNS.pop(key, None)


def _jev_command(raw_args: str = "") -> str:
    words = (raw_args or "").split()
    modes = {"skills": {"on", "off", "shadow"}, "memory": {"on", "off"},
             "compaction": {"on", "off"}, "actions": {"on", "off"}, "routing": {"on", "off"},
             "selector": {"control", "fast", "auto"}}
    if len(words) == 2 and words[0] in modes and words[1] in modes[words[0]]:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {key: value for key, value in _state().items() if key in modes and value in modes[key]}
        state[words[0]] = words[1]
        import tempfile
        fd, name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return f"Jev {words[0]} = {words[1]}, set for {_profile()}. Tool exposure refreshes in a fresh session. Routing means advice and fresh CLI launch, not gateway switching."
    from .decision_tools import _with_profile_key
    key = _with_profile_key(keystore.describe)
    defaults = {name: ("control" if name == "selector" else "off") for name in modes}
    return "\n".join([
        f"Jev key: {'present' if key['present'] else 'missing in active profile'}",
        *[f"{name}: {_setting(name, defaults[name])}" for name in modes],
        "retrieval/compaction/action tools: explicit advisory calls; no automatic history rewrite or GUI execution",
        "routing: advisory tool + prelaunch CLI; automatic gateway switching is not supported",
        "usage: /jev skills shadow|on|off; /jev selector control|fast|auto; /jev memory|compaction|actions|routing on|off",
    ])


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    from .decision_tools import register_tools
    from .routing_tool import register_routing_tool
    register_tools(ctx, _setting)
    register_routing_tool(ctx, _setting)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "jev",
        _jev_command,
        description="Jev skill-selection status and mode",
        args_hint="[skills shadow|on|off | selector control|fast|auto]",
    )
