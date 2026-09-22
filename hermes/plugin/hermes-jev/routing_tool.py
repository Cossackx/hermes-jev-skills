"""One explicit, bounded, advisory Jev model-routing tool for Hermes.

The tool never changes the active runtime model.  Its caller may use the returned
advice only when it is constructing a separate, fresh launch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .jevkit import keystore, route
from .jevkit import launch

_TOOLSET = "jev"
_MODE = "routing"
_MAX_PROVIDER_CHARS = 128
_MAX_MODEL_CHARS = 256


def _schema(name: str, description: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "description": description, "parameters": parameters}


ROUTING_SCHEMA = _schema(
    "jev_route_model",
    "Advisory-only: select a configured same-provider model for one prospective fresh session. It never changes the active model, creates a session, or alters approvals.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["prompt", "provider", "current"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": launch.MAX_PROMPT_CHARS},
            "provider": {"type": "string", "minLength": 1, "maxLength": _MAX_PROVIDER_CHARS},
            "current": {"type": "string", "minLength": 1, "maxLength": _MAX_MODEL_CHARS},
            "context_tokens": {"type": "integer", "minimum": 0, "maximum": launch.MAX_CONTEXT_TOKENS, "default": 0},
            "has_images": {"type": "boolean", "default": False},
        },
    },
)


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), separators=(",", ":"), sort_keys=True)


def _profile_secret() -> Optional[str]:
    """Resolve only through Hermes' active profile secret context, never ambient env."""
    try:
        from agent.secret_scope import get_secret  # type: ignore

        value = get_secret("TYPESAFE_API_KEY")
    except Exception:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _active_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _active_profile() -> str:
    path = _active_home()
    return path.name if path.parent.name == "profiles" else "default"


def _arguments(args: Any) -> Optional[tuple[str, str, str, int, bool]]:
    if not isinstance(args, Mapping) or set(args) - {"prompt", "provider", "current", "context_tokens", "has_images"}:
        return None
    prompt, provider, current = args.get("prompt"), args.get("provider"), args.get("current")
    context_tokens = args.get("context_tokens", 0)
    has_images = args.get("has_images", False)
    if (
        not isinstance(prompt, str) or not prompt.strip() or len(prompt) > launch.MAX_PROMPT_CHARS
        or not isinstance(provider, str) or not provider.strip() or len(provider) > _MAX_PROVIDER_CHARS
        or not isinstance(current, str) or not current.strip() or len(current) > _MAX_MODEL_CHARS
        or isinstance(context_tokens, bool) or not isinstance(context_tokens, int)
        or not 0 <= context_tokens <= launch.MAX_CONTEXT_TOKENS
        or not isinstance(has_images, bool)
    ):
        return None
    return prompt, provider.strip(), current.strip(), context_tokens, has_images


def _handler(args: Any, **_: Any) -> str:
    parsed = _arguments(args)
    if parsed is None:
        return _json({"status": "refused", "reason": "invalid_arguments"})
    prompt, provider, current, context_tokens, has_images = parsed
    try:
        config = launch.load_active_config(provider, _active_home())
        rows = launch.model_rows(config, provider)
        # Explicit scope forbids keystore's environment/file/OS-store fallback.
        with keystore.credential_scope(_profile_secret):
            decision = route.decide(
                prompt,
                current=current if ":" in current else f"{provider}:{current}",
                context_tokens=context_tokens,
                has_images=has_images,
                profile=_active_profile(),
                config=config,
                rows=rows,
                timeout=launch.DEFAULT_TIMEOUT,
                only_provider=provider,
            )
    except Exception:
        decision = launch._fail_open(current, "Jev unavailable")
    decision["advisory_only"] = True
    return _json(decision)


def _enabled(settings_getter: Callable[..., Any]) -> bool:
    try:
        return str(settings_getter(_MODE, "off")).strip().lower() == "on"
    except Exception:
        return False


def register_routing_tool(ctx: Any, settings_getter: Callable[..., Any]) -> None:
    """Register only the explicit advisory tool, gated by ``routing: on``."""
    def gated_handler(args, **kwargs):
        if not _enabled(settings_getter):
            return _json({"status": "disabled", "reason": "disabled_in_active_profile"})
        return _handler(args, **kwargs)
    ctx.register_tool(
        name="jev_route_model",
        toolset=_TOOLSET,
        schema=ROUTING_SCHEMA,
        handler=gated_handler,
        description=ROUTING_SCHEMA["description"],
        emoji="🔎",
        check_fn=lambda: _enabled(settings_getter),
    )
