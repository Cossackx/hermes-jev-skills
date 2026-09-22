"""Where the TypeSafe key lives, and how to read or store it without showing it.

Resolution order (first hit wins):
  1. ``TYPESAFE_API_KEY`` in the process environment
  2. the OS secret store (macOS Keychain, or ``secret-tool`` on Linux)
  3. ``~/.config/jev/credentials`` (mode 0600), the portable fallback

Nothing in this module prints, logs, or returns the key to a caller that did not
ask for it by name, and ``describe()`` only ever reports presence and length.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional
from contextlib import contextmanager
from contextvars import ContextVar

# Hosts bind credentials per invocation; no ambient fallback inside that scope.
_RESOLVER: ContextVar[Optional[Callable[[], Optional[str]]]] = ContextVar("jev_credential_resolver", default=None)


@contextmanager
def credential_scope(resolver: Callable[[], Optional[str]]):
    token = _RESOLVER.set(resolver)
    try:
        yield
    finally:
        _RESOLVER.reset(token)

ENV_VAR = "TYPESAFE_API_KEY"
KEYCHAIN_SERVICE = "Hermes TypeSafe API"
KEYCHAIN_ACCOUNT = ENV_VAR
_SECURITY = "/usr/bin/security"


def credentials_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jev" / "credentials"


def looks_like_key(value: str) -> bool:
    """Cheap shape check so an obvious paste mistake is caught before storing."""
    value = value.strip()
    return 20 <= len(value) <= 512 and not any(c.isspace() for c in value) and value.isprintable()


# ── read ─────────────────────────────────────────────────────────────────────

def _from_keychain() -> Optional[str]:
    if sys.platform == "darwin" and os.path.exists(_SECURITY):
        cmd = [_SECURITY, "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT]
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE, "account", KEYCHAIN_ACCOUNT]
    else:
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except Exception:  # noqa: BLE001 - a broken secret store must never crash a caller
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def _from_file() -> Optional[str]:
    path = credentials_file()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(ENV_VAR + "="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def resolve() -> Optional[str]:
    resolver = _RESOLVER.get()
    if resolver is not None:
        try:
            return (resolver() or "").strip() or None
        except Exception:
            return None
    return (os.environ.get(ENV_VAR) or "").strip() or _from_keychain() or _from_file()


def source() -> str:
    if _RESOLVER.get() is not None:
        return "host-profile" if resolve() else "absent"
    if (os.environ.get(ENV_VAR) or "").strip():
        return "environment"
    if _from_keychain():
        return "os-secret-store"
    if _from_file():
        return "credentials-file"
    return "absent"


def describe() -> Dict[str, object]:
    key = resolve()
    return {"present": bool(key), "source": source(), "length": len(key) if key else 0}


# ── write ────────────────────────────────────────────────────────────────────

def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a crash can never leave a half-written .env.
    temp = path.with_name(path.name + ".jev-tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, path)


def upsert_env_file(path: Path, value: str) -> None:
    """Set ENV_VAR in a dotenv file, leaving every other line untouched."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    entry = f"{ENV_VAR}={value}"
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(ENV_VAR + "="):
            lines[index] = entry
            replaced = True
    if not replaced:
        lines.append(entry)
    _write_private(path, "\n".join(lines) + "\n")


def _store_keychain(value: str) -> bool:
    if sys.platform == "darwin" and os.path.exists(_SECURITY):
        # `security` has no stdin mode for the secret, so it is briefly an argv entry
        # of a child we own. The alternative (no secret store at all) is worse.
        cmd = [_SECURITY, "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w", value]
        stdin = None
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "store", "--label", KEYCHAIN_SERVICE, "service", KEYCHAIN_SERVICE,
               "account", KEYCHAIN_ACCOUNT]
        stdin = value
    else:
        return False
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=15, check=False)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


def hermes_env_files(hermes_home: Optional[Path] = None) -> List[Path]:
    """Every dotenv a Hermes lane reads. A lane resolves ${VAR} from its OWN .env."""
    home = hermes_home or Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if not home.is_dir():
        return []
    files = [home / ".env"]
    profiles = home / "profiles"
    if profiles.is_dir():
        files += sorted(p / ".env" for p in profiles.iterdir() if p.is_dir() and not p.name.startswith("."))
    return files


def store(value: str, hermes: bool = True, hermes_home: Optional[Path] = None) -> Dict[str, object]:
    """Persist the key. Returns where it went, never the key itself."""
    value = value.strip()
    if not looks_like_key(value):
        raise ValueError("that does not look like an API key")
    written: List[str] = []
    if _store_keychain(value):
        written.append("os-secret-store")
    else:
        upsert_env_file(credentials_file(), value)
        written.append(str(credentials_file()))
    lanes = 0
    if hermes:
        for env_file in hermes_env_files(hermes_home):
            upsert_env_file(env_file, value)
            lanes += 1
    return {"stored_in": written, "hermes_env_files": lanes, "length": len(value)}
