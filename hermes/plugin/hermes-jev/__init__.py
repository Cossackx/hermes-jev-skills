"""Hermes Jev skill observer.

``skills=shadow`` evaluates eligible turns and logs decision metadata without
changing model input or output. ``skills=on`` adds at most two candidates as
advisory context. The plugin contains no model-routing, memory-filtering,
compaction, action-selection, tool, or middleware surface.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .jevkit import keystore, privacy, skillpick

_CTX: Any = None
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
    if value is None and _CTX is not None:
        try:
            value = _CTX.get_config(name, None)
        except Exception:  # noqa: BLE001
            value = None
    return str(value if value is not None else default).lower()


def _profile() -> str:
    home = _home()
    return home.name if home.parent.name == "profiles" else "default"


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


def _is_context_only_followup(text: str) -> bool:
    normalized = re.sub(r"[^\w]+", " ", text.casefold()).strip()
    return normalized in _CONTEXT_ONLY_FOLLOWUPS


def _on_pre_llm_call(
    session_id: str = "", turn_id: Any = None, user_message: Any = "", **_: Any
) -> Any:
    del session_id, turn_id
    full_text = user_message if isinstance(user_message, str) else json.dumps(user_message, default=str)
    mode = _setting("skills", "off")
    if mode not in ("on", "shadow") or not full_text.strip():
        return None
    if full_text.lstrip().startswith(_HERMES_CONTROL_PREFIXES):
        _log({
            "kind": "skill",
            "mode": mode,
            "status": "defer_native",
            "reason": "hermes_control_message",
            "picked": [],
            "matches": [],
            "latency_ms": 0,
        })
        return None
    if _is_context_only_followup(full_text):
        _log({
            "kind": "skill",
            "mode": mode,
            "status": "defer_native",
            "reason": "context_only_followup",
            "picked": [],
            "matches": [],
            "latency_ms": 0,
        })
        return None
    if privacy.is_sensitive(full_text):
        _log({
            "kind": "skill",
            "mode": mode,
            "status": "fail_open",
            "reason": "turn looks sensitive; not sent",
            "picked": [],
            "matches": [],
            "latency_ms": 0,
        })
        return None

    text = full_text[:6000]
    catalog = skillpick.discover(_skill_roots(), disabled=_disabled_skills())
    picked = skillpick.pick(text, catalog, top_k=2)
    candidates = picked.get("skills", [])[:2]
    _log({
        "kind": "skill",
        "mode": mode,
        "status": picked.get("status"),
        "needs_skill": picked.get("needs_skill"),
        "picked": [skill["name"] for skill in candidates],
        "matches": [skill.get("match") for skill in candidates],
        "latency_ms": picked.get("latency_ms"),
    })
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


def _jev_command(raw_args: str = "") -> str:
    words = (raw_args or "").split()
    if len(words) == 2 and words[0] == "skills" and words[1] in ("on", "off", "shadow"):
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"skills": words[1]}, indent=2), encoding="utf-8")
        return f"Jev skills = {words[1]}, set for {_profile()}."
    key = keystore.describe()
    return "\n".join([
        f"Jev key: {'present' if key['present'] else 'MISSING (run `jev setup-key` on this machine)'}",
        f"skills: {_setting('skills', 'off')}",
        "model routing, memory filtering, compaction, and action selection: not present",
        "usage: /jev skills shadow|on|off",
    ])


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_command(
        "jev",
        _jev_command,
        description="Jev skill-selection status and mode",
        args_hint="[skills shadow|on|off]",
    )
