"""Explicit, advisory Jev decision tools for a Hermes plugin.

These handlers do not observe conversations or execute actions.  A model must
call one of the registered tools explicitly; the result is advice only.  Inputs
are deliberately small, plain-text records, and a whole request that resembles
a secret is retained locally and never sent to Jev.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Mapping, Sequence

from .jevkit import choose, compact, keystore, privacy, rerank

_TOOLSET = "jev"
_MEMORY_MODE = "memory"
_COMPACTION_MODE = "compaction"
_ACTIONS_MODE = "actions"
_MAX_MESSAGES = 120
_MAX_QUERY_CHARS = 2_000
_MAX_ID_CHARS = 128
_MAX_PASSAGE_CHARS = 900
_MAX_MESSAGE_CHARS = 700


def _schema(name: str, description: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "description": description, "parameters": parameters}


MEMORY_SCHEMA = _schema(
    "jev_memory_filter",
    "Advisory-only: rank an already retrieved, plain-text shortlist. It never retrieves, stores, or deletes memory.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["query", "candidates"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": _MAX_QUERY_CHARS},
            "candidates": {
                "type": "array", "minItems": 1, "maxItems": rerank.MAX_CANDIDATES,
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "text"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": _MAX_ID_CHARS},
                        "text": {"type": "string", "minLength": 1, "maxLength": _MAX_PASSAGE_CHARS},
                    },
                },
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
    },
)

COMPACTION_SCHEMA = _schema(
    "jev_compact_select",
    "Advisory-only: label bounded plain-text turns keep, summarize, or drop. It does not compact or rewrite the transcript.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["messages"],
        "properties": {
            "messages": {
                "type": "array", "minItems": 1, "maxItems": _MAX_MESSAGES,
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["role", "content"],
                    "properties": {
                        "role": {"type": "string", "enum": ["system", "user", "assistant", "tool"]},
                        "content": {"type": "string", "minLength": 1, "maxLength": _MAX_MESSAGE_CHARS},
                    },
                },
            },
            "keep_last": {"type": "integer", "minimum": 0, "maximum": 12, "default": 6},
        },
    },
)

ACTION_SCHEMA = _schema(
    "jev_choose_action",
    "Advisory-only: choose one opaque ID from prevalidated actions. It cannot execute an action, invent selectors, coordinates, or text.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["request"],
        "properties": {
            "request": {
                "type": "object", "additionalProperties": False,
                "required": ["schema", "goal", "observation_id", "candidates"],
                "properties": {
                    "schema": {"type": "string", "enum": [choose.REQUEST_SCHEMA]},
                    "goal": {"type": "string", "minLength": 1, "maxLength": 2_000},
                    "observation_id": {"type": "string", "maxLength": 256},
                    "regions": {
                        "type": "array", "maxItems": choose.MAX_REGIONS,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["id", "label"],
                            "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 128},
                                "role": {"type": "string", "minLength": 1, "maxLength": 64},
                                "label": {"type": "string", "minLength": 1, "maxLength": 300},
                                "interactive": {"type": "boolean"},
                            },
                        },
                    },
                    "history": {
                        "type": "array", "maxItems": choose.MAX_HISTORY,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "selected_id": {"type": "string", "minLength": 1, "maxLength": 160},
                                "outcome": {"type": "string", "minLength": 1, "maxLength": 160},
                            },
                        },
                    },
                    "candidates": {
                        "type": "array", "minItems": 2, "maxItems": choose.MAX_CANDIDATES,
                        "items": {
                            "type": "object", "additionalProperties": False, "required": ["id", "description"],
                            "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 64},
                                "description": {"type": "string", "minLength": 1, "maxLength": 600},
                            },
                        },
                    },
                },
            },
        },
    },
)


def _json(value: Mapping[str, Any]) -> str:
    """Tool handlers return the JSON strings required by Hermes' registry."""
    return json.dumps(dict(value), separators=(",", ":"), sort_keys=True)


def _plain_string(value: Any, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _whole_payload_is_sensitive(value: Any) -> bool:
    # Validation has made this a bounded, JSON-native tree.  Do not log it.
    return privacy.is_sensitive(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _profile_secret() -> str | None:
    """Resolve only through Hermes' active profile secret scope, never environment."""
    try:
        from agent.secret_scope import get_secret  # type: ignore
        value = get_secret("TYPESAFE_API_KEY")
    except Exception:  # Missing or unscoped profile secret is intentionally a missing key.
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _with_profile_key(call: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    # client.ask -> keystore.resolve() is now fail-closed to this active profile.
    with keystore.credential_scope(_profile_secret):
        return call()


def _enabled(settings_getter: Callable[..., Any], name: str) -> bool:
    try:
        return str(settings_getter(name, default="off")).strip().lower() == "on"
    except Exception:
        return False


def _memory_arguments(args: Any) -> tuple[str, Sequence[Mapping[str, Any]], int] | None:
    if not isinstance(args, Mapping) or set(args) - {"query", "candidates", "top_k"}:
        return None
    query, candidates = args.get("query"), args.get("candidates")
    top_k = args.get("top_k", 8)
    if not _plain_string(query, _MAX_QUERY_CHARS) or not isinstance(candidates, list) or not candidates:
        return None
    if len(candidates) > rerank.MAX_CANDIDATES or isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        return None
    clean: list[Mapping[str, Any]] = []
    for item in candidates:
        if not isinstance(item, Mapping) or set(item) != {"id", "text"}:
            return None
        if not _plain_string(item.get("id"), _MAX_ID_CHARS) or not _plain_string(item.get("text"), _MAX_PASSAGE_CHARS):
            return None
        clean.append({"id": item["id"], "text": item["text"]})
    if len({item["id"] for item in clean}) != len(clean):
        return None
    return query, clean, top_k


def _memory_handler(args: Any, **_: Any) -> str:
    parsed = _memory_arguments(args)
    if parsed is None:
        return _json({"status": "refused", "reason": "invalid_arguments"})
    query, candidates, top_k = parsed
    baseline = [str(item["id"]) for item in candidates][:top_k]
    if _whole_payload_is_sensitive({"query": query, "candidates": candidates, "top_k": top_k}):
        return _json({"status": "fail_open", "reason": "sensitive_payload_not_sent", "selected_ids": baseline,
                      "dropped_injection_ids": [], "scores": {}})
    try:
        return _json(_with_profile_key(lambda: rerank.rerank(query, candidates, top_k=top_k)))
    except Exception:
        return _json({"status": "fail_open", "reason": "decision_unavailable", "selected_ids": baseline,
                      "dropped_injection_ids": [], "scores": {}})


def _compaction_arguments(args: Any) -> tuple[Sequence[Mapping[str, str]], int] | None:
    if not isinstance(args, Mapping) or set(args) - {"messages", "keep_last"}:
        return None
    messages, keep_last = args.get("messages"), args.get("keep_last", 6)
    if not isinstance(messages, list) or not messages or len(messages) > _MAX_MESSAGES:
        return None
    if isinstance(keep_last, bool) or not isinstance(keep_last, int) or not 0 <= keep_last <= 12:
        return None
    clean: list[Mapping[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            return None
        if message.get("role") not in {"system", "user", "assistant", "tool"} or not _plain_string(message.get("content"), _MAX_MESSAGE_CHARS):
            return None
        clean.append({"role": message["role"], "content": message["content"]})
    return clean, keep_last


def _safe_compaction(messages: Sequence[Mapping[str, str]], keep_last: int, reason: str) -> Dict[str, Any]:
    total = len(messages)
    fates = {str(i): "keep" if message["role"] == "system" or i >= total - keep_last else "summarize"
             for i, message in enumerate(messages)}
    counts = {fate: sum(value == fate for value in fates.values()) for fate in ("keep", "summarize", "drop")}
    return {"status": "fail_open", "reason": reason, "fates": fates, "counts": counts,
            "jev_calls": 0, "errors": [], "latency_ms": 0}


def _compaction_handler(args: Any, **_: Any) -> str:
    parsed = _compaction_arguments(args)
    if parsed is None:
        return _json({"status": "refused", "reason": "invalid_arguments"})
    messages, keep_last = parsed
    if _whole_payload_is_sensitive({"messages": messages, "keep_last": keep_last}):
        return _json(_safe_compaction(messages, keep_last, "sensitive_payload_not_sent"))
    try:
        return _json(_with_profile_key(lambda: compact.select(messages, keep_last=keep_last)))
    except Exception:
        return _json(_safe_compaction(messages, keep_last, "decision_unavailable"))


def _action_fallback(observation_id: str = "", reason: str = "decision_unavailable") -> Dict[str, Any]:
    return {"schema": choose.RESPONSE_SCHEMA, "selected_id": "reobserve", "confidence": 0.0,
            "reason": reason, "observation_id": observation_id, "probabilities": {}}


def _action_handler(args: Any, **_: Any) -> str:
    if not isinstance(args, Mapping) or set(args) != {"request"} or not isinstance(args.get("request"), Mapping):
        return _json({"status": "refused", "reason": "invalid_arguments"})
    request = dict(args["request"])
    observation_id = request.get("observation_id")
    if not isinstance(observation_id, str) or len(observation_id) > 256:
        return _json({"status": "refused", "reason": "invalid_arguments"})
    if any(not isinstance(request.get(key, []), list) for key in ("regions", "history", "candidates")):
        return _json({"status": "refused", "reason": "invalid_arguments"})
    if any(not isinstance(item, Mapping) or
           ("interactive" in item and not isinstance(item["interactive"], bool))
           for item in request.get("regions", [])):
        return _json({"status": "refused", "reason": "invalid_arguments"})
    try:
        valid = choose.validate(request)
    except (TypeError, ValueError):
        return _json({"status": "refused", "reason": "invalid_arguments"})
    if _whole_payload_is_sensitive(request):
        return _json(_action_fallback(valid["observation_id"], "sensitive_payload_not_sent"))
    try:
        return _json(_with_profile_key(lambda: choose.choose(request)))
    except Exception:
        return _json(_action_fallback(valid["observation_id"]))


def register_tools(ctx: Any, settings_getter: Callable[..., Any]) -> None:
    """Register explicit tools; each profile's ``on``/``off`` setting gates exposure."""
    entries = (
        ("jev_memory_filter", MEMORY_SCHEMA, _MEMORY_MODE, _memory_handler),
        ("jev_compact_select", COMPACTION_SCHEMA, _COMPACTION_MODE, _compaction_handler),
        ("jev_choose_action", ACTION_SCHEMA, _ACTIONS_MODE, _action_handler),
    )
    for name, schema, setting, handler in entries:
        def gated_handler(args, _handler=handler, _setting=setting, **kwargs):
            if not _enabled(settings_getter, _setting):
                return _json({"status": "disabled", "reason": "disabled_in_active_profile"})
            return _handler(args, **kwargs)
        ctx.register_tool(
            name=name,
            toolset=_TOOLSET,
            schema=schema,
            handler=gated_handler,
            description=schema["description"],
            emoji="🔎",
            check_fn=lambda setting=setting: _enabled(settings_getter, setting),
        )
