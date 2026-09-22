"""Fresh-session Hermes launcher with fail-open Jev model advice.

This module deliberately owns no Hermes configuration.  It reads just the active
profile's routing file, asks Jev under the host profile's secret scope, then
starts a new one-shot Hermes CLI session with an argument vector.  It never
resumes a session, changes an approval setting, or enables safe mode.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import keystore, route

DEFAULT_PROVIDER = "openai-codex"
DEFAULT_CURRENT = "gpt-6-astra"
DEFAULT_TIMEOUT = 2.5
MAX_PROMPT_CHARS = 6_000
MAX_CONTEXT_TOKENS = 10_000_000


def active_routing_path(home: Optional[Path] = None) -> Optional[Path]:
    """Use an explicit host home or the standalone process home; never inherit."""
    home = home or os.environ.get("HERMES_HOME")
    return Path(home).expanduser() / "jev" / "routing.json" if home else None


def _default_config() -> Dict[str, Any]:
    return json.loads(json.dumps(route.DEFAULT_CONFIG))


def _read_active_config(home: Optional[Path] = None) -> Dict[str, Any]:
    path = active_routing_path(home)
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _qualified(ref: Any, provider: str) -> Optional[str]:
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    return ref if ":" in ref else f"{provider}:{ref}"


def _normalise_tiers(raw: Any, provider: str) -> Dict[str, Dict[str, List[str]]]:
    if not isinstance(raw, Mapping):
        return {}
    tiers: Dict[str, Dict[str, List[str]]] = {}
    for tier, pools in raw.items():
        if tier not in route.TIERS or not isinstance(pools, Mapping):
            continue
        clean_pools: Dict[str, List[str]] = {}
        for specialty, refs in pools.items():
            if specialty not in route.SPECIALTIES or not isinstance(refs, list):
                continue
            clean = [qualified for ref in refs if (qualified := _qualified(ref, provider))]
            if clean:
                clean_pools[str(specialty)] = clean
        if clean_pools:
            tiers[str(tier)] = clean_pools
    return tiers


def load_active_config(provider: str, home: Optional[Path] = None) -> Dict[str, Any]:
    """Load only the active profile; unknown models cannot bypass metadata checks."""
    config = _default_config()
    raw = _read_active_config(home)
    config.update(raw)
    known = {f"{row['provider']}:{row['model']}" for row in model_rows(config, provider)}
    config["tiers"] = {
        tier: {specialty: [ref for ref in refs if ref in known]
               for specialty, refs in pools.items()}
        for tier, pools in _normalise_tiers(raw.get("tiers"), provider).items()
    }
    return config


def _row(model: Any, metadata: Any, provider: str) -> Optional[Dict[str, Any]]:
    if not isinstance(model, str) or not model.strip() or not isinstance(metadata, Mapping):
        return None
    model = model.strip()
    metadata_provider = metadata.get("provider")
    row_provider = metadata_provider.strip() if isinstance(metadata_provider, str) and metadata_provider.strip() else provider
    if ":" in model:
        embedded_provider, model = model.split(":", 1)
        if not metadata_provider:
            row_provider = embedded_provider
    if not model:
        return None
    context = metadata.get("context", 0)
    if isinstance(context, bool) or not isinstance(context, int) or context < 0:
        return None
    price = metadata.get("price", 0.0)
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return {
        "provider": row_provider,
        "model": model,
        "name": str(metadata.get("name") or model),
        "price": float(price),
        "context": context,
        "vision": bool(metadata.get("vision", False)),
        "reasoning": bool(metadata.get("reasoning", False)),
        "released": str(metadata.get("released") or ""),
    }


def model_rows(config: Mapping[str, Any], provider: str) -> List[Dict[str, Any]]:
    """Use only operator-declared ``models`` metadata, never a provider catalog."""
    models = config.get("models")
    entries: Iterable[tuple[Any, Any]]
    if isinstance(models, list):
        entries = ((item.get("model"), item) for item in models if isinstance(item, Mapping))
    elif isinstance(models, Mapping):
        entries = ((model, metadata) for model, metadata in models.items())
    else:
        return []
    rows = [row for model, metadata in entries if (row := _row(model, metadata, provider))]
    # Deterministic ordering and de-duplication keep tests and advice stable.
    unique = {(row["provider"], row["model"]): row for row in rows}
    return [unique[key] for key in sorted(unique)]


def _profile_secret() -> Optional[str]:
    """Prefer a loaded host profile; standalone CLI may use its existing env key only."""
    try:
        from agent.secret_scope import get_secret  # type: ignore
    except ImportError:
        # Standalone launcher uses only its inherited credential, never a store.
        value = os.environ.get("TYPESAFE_API_KEY")
    else:
        try:
            value = get_secret("TYPESAFE_API_KEY")
        except Exception:
            return None  # A host scope failure must not borrow ambient credentials.
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fail_open(current: str, reason: str) -> Dict[str, Any]:
    return {
        "routed": False,
        "model": current,
        "reason": reason,
        "policy": route.POLICY_VERSION,
        "notice": f"[Jev] kept {current} · {reason}",
    }


def route_model(
    prompt: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    current: str = DEFAULT_CURRENT,
    context_tokens: int = 0,
    has_images: bool = False,
    profile: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Return advisory routing for one fresh launch, constrained to its provider."""
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        return _fail_open(current, "invalid prompt")
    if not isinstance(provider, str) or not provider.strip() or not isinstance(current, str) or not current.strip():
        return _fail_open(current if isinstance(current, str) else DEFAULT_CURRENT, "invalid runtime metadata")
    if isinstance(context_tokens, bool) or not isinstance(context_tokens, int) or not 0 <= context_tokens <= MAX_CONTEXT_TOKENS:
        return _fail_open(current, "invalid runtime metadata")
    provider = provider.strip()
    current = current.strip()
    config = load_active_config(provider)
    rows = model_rows(config, provider)
    # This scope prevents fallback to credential files or OS stores. A launcher
    # may use an existing process key; a missing key still fails open.
    try:
        with keystore.credential_scope(_profile_secret):
            return route.decide(
                prompt,
                current=current if ":" in current else f"{provider}:{current}",
                context_tokens=context_tokens,
                has_images=has_images,
                profile=profile,
                config=config,
                rows=rows,
                timeout=timeout,
                only_provider=provider,
            )
    except Exception:
        return _fail_open(current, "Jev unavailable")


def selected_model(decision: Mapping[str, Any], current: str) -> str:
    model = decision.get("model_id") or decision.get("model") or current
    if not isinstance(model, str) or not model.strip():
        return current
    return model.split(":", 1)[-1]


def hermes_command(provider: str, model: str, prompt: str) -> List[str]:
    """The only launched form: a fresh, approval-preserving one-shot session."""
    return ["hermes", "--provider", provider, "--model", model, "chat", "--query", prompt]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jevkit.launch",
        description="Route one fresh Hermes CLI launch without changing profile configuration.",
    )
    parser.add_argument("--prompt", required=True, help="Prompt for the fresh Hermes session and advisory routing.")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--current", default=DEFAULT_CURRENT, help="Current model ID used on Jev fail-open.")
    parser.add_argument("--model", help="Explicit model override; bypasses Jev entirely.")
    parser.add_argument("--context-tokens", type=int, default=0)
    parser.add_argument("--has-images", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the argument vector; do not start Hermes.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.model:
        decision: Dict[str, Any] = {
            "routed": False,
            "model": args.model,
            "reason": "explicit --model override",
            "policy": route.POLICY_VERSION,
        }
        model = args.model
    else:
        decision = route_model(
            args.prompt,
            provider=args.provider,
            current=args.current,
            context_tokens=args.context_tokens,
            has_images=args.has_images,
        )
        model = selected_model(decision, args.current)
    command = hermes_command(args.provider, model, args.prompt)
    if args.dry_run:
        sys.stdout.write(json.dumps({"decision": decision, "command": command}, separators=(",", ":")) + "\n")
        return 0
    try:
        return int(subprocess.run(command, check=False).returncode)
    except FileNotFoundError:
        sys.stderr.write("hermes executable not found; Jev made no configuration changes.\n")
        return 127


if __name__ == "__main__":
    sys.exit(main())
