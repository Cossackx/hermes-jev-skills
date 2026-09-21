#!/usr/bin/env python3
"""Bounded browser-use + Jev runner for Jev Ultrafast.

Jev Ultrafast chooses an operation and an observed target; the executor performs
exactly that one action. Nothing here lets the model emit selectors, code, or
free-form text: the operation and target must map to elements that were observed
on the live page.

Safety properties enforced by this runner
-----------------------------------------
* Credentials come from the process environment, else macOS Keychain. They are
  never written to disk, never placed in config, and never printed.
* Live runs require an explicit ``--allow-hosts`` allowlist. The loop aborts
  mid-run if the page's host leaves the allowlist, so an agent cannot wander off
  the approved site.
* ``--max-ticks`` hard-caps the number of Jev model calls.
* Exactly one browser context is used, and it is always closed.
* Success is an independently verified page/postcondition check, never the
  agent's own DONE choice.
* Refuses to start without a TypeSafe credential.

Self-locating: if ``jev_ultrafast`` is not importable in the current interpreter,
this script re-execs itself with the vendored repo's virtualenv python.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from urllib.parse import urlparse

KEYCHAIN_TYPESAFE = ("Hermes TypeSafe API", "TYPESAFE_API_KEY")
DEFAULT_TEXT_MODEL = "google/gemini-2.5-flash"
DEFAULT_TEXT_BASE = "https://openrouter.ai/api/v1"
ACTIVATION_SETTLE_SECONDS = 0.15


# ─ credentials ──────────────────────────────────────────────────────────────

def keychain_get(service: str, account: str) -> str | None:
    """Read one generic-password item. Returns None when absent."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def resolve_credentials(env: dict, lookup=None) -> dict:
    """Environment first, then Keychain. Missing values are simply absent.

    ``lookup`` is resolved at call time, so tests (and any caller) can
    substitute a deterministic lookup without patching the default binding.
    """
    if lookup is None:
        lookup = keychain_get
    typesafe = env.get("TYPESAFE_API_KEY") or lookup(*KEYCHAIN_TYPESAFE)
    text_key = env.get("TEXT_MODEL_API_KEY") or env.get("OPENROUTER_API_KEY")
    resolved = {}
    if typesafe:
        resolved["TYPESAFE_API_KEY"] = typesafe
    if text_key:
        resolved["TEXT_MODEL_API_KEY"] = text_key
    resolved["TYPESAFE_MODEL"] = env.get("TYPESAFE_MODEL", "jev-latest")
    resolved["TEXT_MODEL"] = env.get("TEXT_MODEL", DEFAULT_TEXT_MODEL)
    resolved["TEXT_MODEL_BASE_URL"] = env.get("TEXT_MODEL_BASE_URL", DEFAULT_TEXT_BASE)
    return resolved


def redact(secret: str | None) -> str:
    if not secret:
        return "(absent)"
    return f"present(len={len(secret)})"


# ── host allowlist ───────────────────────────────────────────────────────────

def host_allowed(url: str, allow_hosts: list[str]) -> bool:
    """True when the URL's host is the allowlisted host or a subdomain of it."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    for allowed in allow_hosts:
        a = allowed.strip().lower().lstrip(".")
        if a and (host == a or host.endswith("." + a)):
            return True
    return False


# ── outcome verification ─────────────────────────────────────────────────────

def outcome_verified(title: str, heading: str, url: str, expect: str) -> bool:
    """Independent check against live page state, not the agent's DONE choice."""
    if not expect:
        return False
    haystack = " ".join([title or "", heading or "", url or ""]).lower()
    return expect.lower() in haystack


# ── runner ───────────────────────────────────────────────────────────────────

def resolve_ultrafast_repo(env: Mapping[str, str] | None = None, cwd: Path | None = None,
                           home: Path | None = None) -> Path:
    """Find a local Jev Ultrafast checkout without embedding a machine path."""
    source_env = os.environ if env is None else env
    override = source_env.get("JEV_ULTRAFAST_REPO", "").strip()
    if override:
        return Path(override).expanduser()

    cwd = (cwd or Path.cwd()).expanduser()
    home = (home or Path.home()).expanduser()
    candidates = [cwd / "jev-ultrafast", home / "jev-ultrafast"]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def venv_python(repo: Path, platform: str | None = None) -> Path:
    """Return the platform-native Python in the Jev Ultrafast virtualenv."""
    platform = os.name if platform is None else platform
    if platform == "nt":
        return repo / ".venv" / "Scripts" / "python.exe"
    return repo / ".venv" / "bin" / "python"


def resolve_cdp_endpoint(cdp: str, env: Mapping[str, str]) -> tuple[str, str]:
    """Return the Browser Harness variable for an explicitly supplied endpoint."""
    endpoint = cdp.strip() or env.get("BU_CDP_URL", "").strip() or env.get("BU_CDP_WS", "").strip()
    if not endpoint:
        raise ValueError(
            "no dedicated CDP endpoint; use a maintained automation-browser launcher or "
            "supply --cdp, BU_CDP_URL, or BU_CDP_WS"
        )
    scheme = urlparse(endpoint).scheme.lower()
    if scheme in {"http", "https"}:
        return "BU_CDP_URL", endpoint
    if scheme in {"ws", "wss"}:
        return "BU_CDP_WS", endpoint
    raise ValueError("CDP endpoint must start with http://, https://, ws://, or wss://")


def configure_cdp_endpoint(cdp: str, env: MutableMapping[str, str]) -> tuple[str, str]:
    """Install exactly one explicit Browser Harness CDP endpoint in ``env``."""
    name, endpoint = resolve_cdp_endpoint(cdp, env)
    env.pop("BU_CDP_URL", None)
    env.pop("BU_CDP_WS", None)
    env[name] = endpoint
    return name, endpoint


def activate_owned_target(agent, cdp_call=None, sleep=None) -> str:
    """Activate only the page target created by this Agent, then let it settle."""
    target = getattr(getattr(agent, "browser", None), "target", None)
    if not isinstance(target, str) or not target:
        raise RuntimeError("Jev Ultrafast did not expose its owned browser target")
    if cdp_call is None:
        from browser_harness.helpers import cdp as cdp_call  # type: ignore[import-not-found]
    cdp_call("Target.activateTarget", targetId=target)
    (time.sleep if sleep is None else sleep)(ACTIVATION_SETTLE_SECONDS)
    return target


def ensure_importable(argv: list[str] | None = None, repo: Path | None = None) -> None:
    """Re-exec into the vendored venv when jev_ultrafast is not importable.

    Forwards the arguments this run was actually invoked with. Never uses
    ``sys.argv`` when the caller supplied explicit arguments, because a
    programmatic caller's ``sys.argv`` belongs to the host process and would
    re-exec with the wrong flags.
    """
    try:
        import jev_ultrafast  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    repo = repo or resolve_ultrafast_repo()
    venv_py = venv_python(repo)
    if venv_py.exists() and Path(sys.executable).resolve() != venv_py.resolve():
        forwarded = list(argv) if argv is not None else sys.argv[1:]
        os.execv(str(venv_py), [str(venv_py), str(Path(__file__).resolve()), *forwarded])
    raise SystemExit(
        "jev_ultrafast is not importable and no vendored venv was found at "
        f"{venv_py}. Clone https://github.com/browser-use/jev-ultrafast, run `uv sync` in it, "
        "and set JEV_ULTRAFAST_REPO to that folder."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bounded browser-use + Jev runner")
    p.add_argument("--url", required=True, help="Starting URL.")
    p.add_argument("--goal", required=True, help="One narrow, non-sensitive goal.")
    p.add_argument("--allow-hosts", required=True,
                   help="Comma-separated host allowlist. The run aborts if the page leaves it.")
    p.add_argument("--max-ticks", type=int, default=10, help="Hard cap on Jev calls (default 10).")
    p.add_argument("--expect", default="", help="Substring that must appear in title/h1/url to PASS.")
    p.add_argument("--cdp", default="", metavar="ENDPOINT",
                   help="Dedicated CDP endpoint (http(s) or ws(s)); never a daily browser.")
    p.add_argument("--json", action="store_true", help="Emit a machine-readable result as the last line.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        endpoint_name, _endpoint = configure_cdp_endpoint(args.cdp, os.environ)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"dedicated browser endpoint configured via {endpoint_name}")

    creds = resolve_credentials(dict(os.environ))
    print(f"typesafe: {redact(creds.get('TYPESAFE_API_KEY'))} model={creds['TYPESAFE_MODEL']}")
    print(f"text helper: {redact(creds.get('TEXT_MODEL_API_KEY'))} model={creds['TEXT_MODEL']} "
          f"base={creds['TEXT_MODEL_BASE_URL']}")
    if "TYPESAFE_API_KEY" not in creds:
        print("FAIL: no TypeSafe credential. Run `jev setup-key`.")
        return 2
    for k, v in creds.items():
        os.environ[k] = v

    allow = [h for h in (args.allow_hosts or "").split(",") if h.strip()]
    if not allow:
        print("FAIL: --allow-hosts is required for a live run.")
        return 2
    if not host_allowed(args.url, allow):
        print(f"FAIL: start URL host is not in the allowlist {allow}.")
        return 2
    ensure_importable(argv, repo=resolve_ultrafast_repo())

    from jev_ultrafast import Agent

    ticks = 0
    left_allowlist = False
    final_url = title = heading = ""
    with Agent(args.url, args.goal) as agent:
        try:
            target = activate_owned_target(agent)
            print(f"  activated owned target: {target}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: could not activate the owned target: {type(exc).__name__}: {str(exc)[:200]}")
            return 2
        try:
            for _state in agent.run():
                ticks += 1
                page_url = agent.state["page"].get("url", "")
                if not host_allowed(page_url, allow):
                    left_allowlist = True
                    print(f"ABORT: page left the allowlist -> {page_url}")
                    break
                last = agent.state["history"][-1] if agent.state["history"] else {}
                print(f"  tick {ticks:>2}  {agent.state['elapsed_ms']:>6} ms  "
                      f"actions={len(agent.state['history'])}  status={agent.state['status']}  "
                      f"last={last.get('kind', '-')}")
                if ticks >= args.max_ticks:
                    print(f"  stopping at the {args.max_ticks}-tick budget")
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"  loop error {type(exc).__name__}: {str(exc)[:200]}")

        final_url = agent.state["page"].get("url", "")
        try:
            title = agent.browser.evaluate("document.title") or ""
            heading = agent.browser.evaluate("(document.querySelector('h1')||{}).textContent||''") or ""
        except Exception as exc:  # noqa: BLE001
            print(f"  could not read the live document: {type(exc).__name__}")
        history = list(agent.state["history"])
        text_calls = list(agent.state["text_calls"])

    verified = outcome_verified(title, heading, final_url, args.expect)
    result = {
        "schema": "hermes.browser_use_jev_run_v1",
        "goal": args.goal,
        "final_url": final_url,
        "title": title,
        "heading": heading,
        "steps": len(history),
        "text_calls": len(text_calls),
        "ticks": ticks,
        "left_allowlist": left_allowlist,
        "expected": args.expect,
        "verified": verified,
    }
    print(f"  final_url: {final_url}")
    print(f"  title: {title!r}")
    print(f"  independent check for {args.expect!r}: {'PASS' if verified else 'FAIL'}")
    if args.json:
        print(json.dumps(result))
    if left_allowlist:
        return 5
    return 0 if verified else 4


if __name__ == "__main__":
    sys.exit(main())