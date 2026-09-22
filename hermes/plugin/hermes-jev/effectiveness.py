"""Profile-local, metadata-only Jev effectiveness events (no runtime wiring).

Construct with the active Hermes home and a trusted approved skill catalog. Only
explicit methods below can emit rows; unrecognized keyword arguments are ignored,
not serialized. Do not pass transcript content into identifiers: they are opaque
join handles and are HMACed, but equality within a profile remains observable.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = "jev.effectiveness.v1"
_IDENTIFIERS = ("profile_id", "session_id", "turn_id", "task_id", "request_id", "call_id")
_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_PUBLIC_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}\Z")
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _is_link(path: Path) -> bool:
    """Treat every symlink or Windows reparse point as an unsafe storage hop."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _assert_no_link_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for candidate in [*reversed(absolute.parents), absolute]:
        if _is_link(candidate):
            raise ValueError("telemetry storage must not traverse links")


def _local_lock(path: Path) -> threading.RLock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(str(path.resolve()), threading.RLock())


@contextlib.contextmanager
def _process_lock(path: Path):
    """Serialize append/rotation/read across processes, including on Windows."""
    _assert_no_link_components(path.parent)
    if _is_link(path):
        raise ValueError("telemetry lock must not be a link")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("telemetry lock must be a regular file")
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class EffectivenessTelemetry:
    """Append allowlisted metadata under one explicitly provided profile home.

    ``max_files`` includes the active segment. Retention is enforced on access;
    there is no background service to purge an idle home. Deduplication covers
    retained rows only; evicted events cannot be joined or deduplicated.
    """

    def __init__(self, home: str | Path, *, profile_id: str,
                 approved_skills: Iterable[str] | Callable[[str], bool],
                 max_bytes: int = 256_000, max_files: int = 4,
                 retention_seconds: int = 30 * 86400):
        if not isinstance(profile_id, str) or not profile_id or not 256 <= max_bytes <= 16_000_000:
            raise ValueError("invalid profile or size limit")
        if not 1 <= max_files <= 32 or not 1 <= retention_seconds <= 366 * 86400:
            raise ValueError("invalid retention limit")
        self.home = Path(os.path.abspath(Path(home).expanduser()))
        _assert_no_link_components(self.home)
        self.directory = self.home / "jev" / "effectiveness"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _assert_no_link_components(self.directory)
        if not self.directory.is_dir():
            raise ValueError("telemetry directory must be a real directory")
        if os.name != "nt":
            os.chmod(self.directory, 0o700, follow_symlinks=False)
        self._lock = _local_lock(self.directory / "write.lock")
        self._lock_path = self.directory / "write.lock"
        self._active = self.directory / "events.jsonl"
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._retention = retention_seconds
        self._approved = approved_skills if callable(approved_skills) else frozenset(approved_skills)
        with self._lock, _process_lock(self._lock_path):
            self._key = self._get_key()
        self._profile = self._hash("profile", profile_id)

    def _get_key(self) -> bytes:
        path = self.directory / "key"
        if _is_link(path):
            raise ValueError("key must not be a link")
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
        if _is_link(path):
            raise ValueError("key must not be a link")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("key must be a regular file")
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                key = handle.read(33)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(key) != 32:
            raise ValueError("invalid telemetry key")
        return key

    def _hash(self, kind: str, value: Any) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
            return None
        return hmac.new(self._key, (kind + "\0" + str(value)).encode("utf-8"), hashlib.sha256).hexdigest()

    def _skill(self, name: Any) -> str:
        if not isinstance(name, str) or not name:
            raise ValueError("skill name required")
        try:
            approved = bool(self._approved(name) if callable(self._approved) else name in self._approved)
        except Exception:
            approved = False
        if approved and _SKILL_NAME.fullmatch(name):
            return name
        return "opaque:" + self._hash("skill", name)

    @staticmethod
    def _tokens(value: Any) -> str:
        if value is None:
            return "unknown"
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("token count must be a nonnegative integer or None")
        if value == 0:
            return "0"
        if value < 1000:
            return "1-999"
        if value < 4000:
            return "1k-4k"
        if value < 16000:
            return "4k-16k"
        return "16k+"

    @staticmethod
    def _label(value: Any) -> str:
        return value if isinstance(value, str) and _PUBLIC_LABEL.fullmatch(value) else "unknown"

    @staticmethod
    def _duration(value: Any) -> str:
        if value is None:
            return "unknown"
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("duration must be nonnegative or None")
        if value < 250:
            return "<250ms"
        if value < 1000:
            return "250ms-1s"
        if value < 5000:
            return "1s-5s"
        return "5s+"

    @staticmethod
    def _score(value: Any) -> str:
        if value is None:
            return "unknown"
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError("score must be in [0, 1] or None")
        lower = min(9, int(value * 10))
        return f"{lower / 10:.1f}-{(lower + 1) / 10:.1f}"

    @staticmethod
    def _small_count(value: Any, *, maximum: int = 1000) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError("count is out of range")
        return value

    def _segments(self) -> list[Path]:
        return [self._active, *(self.directory / f"events.jsonl.{i}" for i in range(1, self._max_files))]

    def _read_segment(self, path: Path) -> bytes | None:
        _assert_no_link_components(self.directory)
        if path.parent != self.directory or _is_link(path):
            raise ValueError("telemetry segment must be a confined regular file")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("telemetry segment must be a regular file")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return handle.read()
        finally:
            if fd >= 0:
                os.close(fd)

    def _prune(self, now: float) -> None:
        for path in self._segments():
            raw = self._read_segment(path)
            if raw is None:
                continue
            lines = raw.splitlines(keepends=True)
            kept = []
            for line in lines:
                try:
                    ts = json.loads(line).get("ts")
                    valid = isinstance(ts, (int, float)) and not isinstance(ts, bool)
                except (ValueError, AttributeError, TypeError):
                    valid = False
                if valid and now - self._retention <= ts <= now + 60:
                    kept.append(line)
            if len(kept) == len(lines):
                continue
            if not kept:
                if _is_link(path):
                    raise ValueError("telemetry segment became a link")
                path.unlink()
                continue
            # Replace under the same writer lock: even a mixed-age segment
            # cannot retain expired rows merely because it was appended today.
            temp = self.directory / (".prune-" + secrets.token_hex(8))
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.writelines(kept)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)

    def _rotate(self) -> None:
        segments = self._segments()
        for path in segments:
            self._read_segment(path)
        if len(segments) == 1:
            self._active.unlink(missing_ok=True)
            return
        segments[-1].unlink(missing_ok=True)
        for i in range(len(segments) - 2, -1, -1):
            if segments[i].exists():
                os.replace(segments[i], segments[i + 1])

    def _rows(self, now: float) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self._segments():
            raw = self._read_segment(path)
            if raw is None:
                continue
            for line in raw.decode("utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if (isinstance(row, dict) and row.get("schema") == SCHEMA
                    and isinstance(row.get("ts"), (int, float))
                    and now - self._retention <= row["ts"] <= now + 60):
                    rows.append(row)
        return rows

    def _emit(self, event: str, *, session_id: Any = None, turn_id: Any = None,
              task_id: Any = None, request_id: Any = None, call_id: Any = None,
              event_id: Any = None, retry_of_call_id: Any = None,
              **fields: Any) -> bool:
        now = time.time()
        row = {"schema": SCHEMA, "ts": now, "event": event,
               "profile_id": self._profile,
               "session_id": self._hash("session", session_id),
               "turn_id": self._hash("turn", turn_id),
               "task_id": self._hash("task", task_id),
               "request_id": self._hash("request", request_id),
               "call_id": self._hash("call", call_id),
               "retry_of_call_id": self._hash("call", retry_of_call_id), **fields}
        row["join_status"] = "joinable" if row["session_id"] and row["turn_id"] else "unjoined"
        identity_scope = event + ":" + str(row.get("skill", "")) + ":" + str(row.get("outcome", ""))
        identity = self._hash("event:" + identity_scope, event_id)
        if identity is None:
            # No fallback to content or wall time for identity. A call is a
            # separate occurrence; a retry points back to its predecessor.
            basis = row["call_id"] or row["request_id"] or row["turn_id"]
            identity = self._hash("event-derived", identity_scope + ":" + (basis or secrets.token_hex(16)))
        row["event_identity"] = identity
        line = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        if len(line) > self._max_bytes:
            raise ValueError("event exceeds segment cap")
        with self._lock, _process_lock(self._lock_path):
            self._prune(now)
            if any(existing.get("event_identity") == identity for existing in self._rows(now)):
                return False
            if self._active.exists() and self._active.stat().st_size + len(line) > self._max_bytes:
                self._rotate()
            if _is_link(self._active):
                raise ValueError("event file must not be a link")
            fd = os.open(
                self._active,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("event file must be a regular file")
                if os.name != "nt":
                    os.chmod(self._active, 0o600)
                if os.write(fd, line) != len(line):
                    raise OSError("incomplete event append")
            finally:
                os.close(fd)
        return True

    def decision(self, status: str, *, strategy: str = "unknown", mode: str = "unknown",
                 candidate_count: int = 0,
                 needs_skill: float | None = None, latency_ms: int | float | None = None,
                 request_bytes: int | None = None, jev_calls: int = 0, **kwargs: Any) -> bool:
        if status not in ("ok", "fail_open", "defer_native", "error"):
            raise ValueError("invalid decision status")
        if strategy not in ("control", "two-stage", "one-stage", "fast", "auto", "unknown"):
            raise ValueError("invalid decision strategy")
        if mode not in ("on", "shadow", "unknown"):
            raise ValueError("invalid decision mode")
        return self._emit(
            "decision", outcome=status, strategy=strategy, mode=mode,
            candidate_count=self._small_count(candidate_count, maximum=20),
            needs_skill_bucket=self._score(needs_skill), latency_bucket=self._duration(latency_ms),
            request_bytes_bucket=self._tokens(request_bytes),
            jev_calls=self._small_count(jev_calls, maximum=20), **self._ids(kwargs),
        )

    def skill_advice(self, skill_name: str, *, match: float | None = None,
                     rank: int = 0, **kwargs: Any) -> bool:
        """One advice row per candidate, not an assertion that it was loaded."""
        return self._emit(
            "skill_advice", skill=self._skill(skill_name), match_bucket=self._score(match),
            rank=self._small_count(rank, maximum=20), **self._ids(kwargs),
        )

    def skill_view_loaded(self, skill_name: str, *, success: bool,
                          content_chars: int | None = None, **kwargs: Any) -> bool:
        """Only confirmed successful skill_view calls are recorded."""
        if success is not True:
            return False
        return self._emit(
            "skill_view_loaded", skill=self._skill(skill_name),
            content_bucket=self._tokens(content_chars), **self._ids(kwargs),
        )

    def tool_outcome(self, outcome: str, *, tool_kind: str = "other", tool_name: str = "unknown",
                     duration_ms: int | float | None = None, error_type: Any = None,
                     **kwargs: Any) -> bool:
        if outcome not in ("success", "error", "refused", "unknown") or tool_kind not in ("skill_view", "jev", "other"):
            raise ValueError("invalid tool outcome or kind")
        error = str(error_type or "").casefold()
        error_class = next((name for name in ("timeout", "permission", "validation", "blocked", "tool_error")
                            if name in error), "other" if error else "none")
        return self._emit(
            "tool_outcome", outcome=outcome, tool_kind=tool_kind,
            tool_name=self._label(tool_name), duration_bucket=self._duration(duration_ms),
            error_class=error_class, **self._ids(kwargs),
        )

    def api_attempt(self, *, provider: str = "unknown", model: str = "unknown",
                    retry_count: int = 0, **kwargs: Any) -> bool:
        return self._emit(
            "api_attempt", provider=self._label(provider), model=self._label(model),
            retry_count=self._small_count(retry_count, maximum=100), **self._ids(kwargs),
        )

    def api_outcome(self, outcome: str, *, input_tokens: int | None = None,
                    output_tokens: int | None = None, cache_read_tokens: int | None = None,
                    cache_write_tokens: int | None = None, reasoning_tokens: int | None = None,
                    provider: str = "unknown", model: str = "unknown",
                    duration_ms: int | float | None = None, **kwargs: Any) -> bool:
        if outcome not in ("success", "error", "timeout", "unknown"):
            raise ValueError("invalid API outcome")
        token_values = {
            "input_bucket": self._tokens(input_tokens), "output_bucket": self._tokens(output_tokens),
            "cache_read_bucket": self._tokens(cache_read_tokens),
            "cache_write_bucket": self._tokens(cache_write_tokens),
            "reasoning_bucket": self._tokens(reasoning_tokens),
        }
        return self._emit(
            "api_outcome", outcome=outcome, provider=self._label(provider), model=self._label(model),
            duration_bucket=self._duration(duration_ms), **token_values, **self._ids(kwargs),
        )

    def turn_outcome(self, outcome: str, **kwargs: Any) -> bool:
        if outcome not in ("completed", "interrupted", "error", "unknown"):
            raise ValueError("invalid turn outcome")
        return self._emit("turn_outcome", outcome=outcome, **self._ids(kwargs))

    def feedback(self, outcome: str, **kwargs: Any) -> bool:
        """Only explicit task feedback; turn completion never implies acceptance."""
        if outcome not in ("accepted", "rejected", "needs_work"):
            raise ValueError("invalid explicit feedback")
        return self._emit("feedback", outcome=outcome, **self._ids(kwargs))

    @staticmethod
    def _ids(kwargs: dict[str, Any]) -> dict[str, Any]:
        # Never forward unexpected kwargs (prompt, history, errors, paths...).
        return {name: kwargs[name] for name in (*_IDENTIFIERS[1:], "event_id", "retry_of_call_id")
                if name in kwargs}

    def rollup(self) -> dict[str, Any]:
        """Aggregate retained events; never return event rows or join handles."""
        now = time.time()
        with self._lock, _process_lock(self._lock_path):
            self._prune(now)
            rows = self._rows(now)
        events = Counter(row["event"] for row in rows)
        missing = Counter(name for row in rows for name in _IDENTIFIERS if row.get(name) is None)
        advised = [row for row in rows if row["event"] == "skill_advice"]
        loaded = [row for row in rows if row["event"] == "skill_view_loaded"]
        def join(row: dict[str, Any]) -> tuple[str, str, str] | None:
            if row.get("session_id") and row.get("turn_id") and row.get("skill"):
                return row["session_id"], row["turn_id"], row["skill"]
            return None
        latest_load: dict[tuple[str, str, str], float] = {}
        for row in loaded:
            key = join(row)
            if key is not None:
                latest_load[key] = max(latest_load.get(key, 0), row["ts"])
        api = Counter("attempt" if row["event"] == "api_attempt" else row.get("outcome", "unknown")
                      for row in rows if row["event"] in ("api_attempt", "api_outcome"))
        tool_groups = Counter(
            (row.get("session_id"), row.get("turn_id"), row.get("tool_name"))
            for row in rows if row["event"] == "tool_outcome" and row.get("session_id")
            and row.get("turn_id") and row.get("tool_name")
        )
        decisions = [row for row in rows if row["event"] == "decision"]
        return {
            "schema": SCHEMA, "total_events": len(rows), "events": dict(events),
            "coverage": {"joinable_advice": sum(join(row) is not None for row in advised),
                         "joinable_loads": sum(join(row) is not None for row in loaded),
                         "unjoined_events": sum(row.get("join_status") == "unjoined" for row in rows)},
            "missing_ids": dict(missing),
            "advice_to_load": {"advised": len(advised), "loaded": len(loaded),
                               "matched": sum(latest_load.get(join(row), -1) >= row["ts"]
                                              for row in advised if join(row) is not None),
                               "unjoinable": sum(join(row) is None for row in advised)},
            "tool_outcomes": dict(Counter(row["outcome"] for row in rows if row["event"] == "tool_outcome")),
            "subsequent_calls": sum(max(0, count - 1) for count in tool_groups.values()),
            "retries": sum(row.get("retry_of_call_id") is not None for row in rows),
            "api": dict(api),
            "api_routes": dict(Counter(f"{row.get('provider', 'unknown')}:{row.get('model', 'unknown')}"
                                       for row in rows if row["event"] == "api_outcome")),
            "turn_outcomes": dict(Counter(row["outcome"] for row in rows if row["event"] == "turn_outcome")),
            "feedback": dict(Counter(row["outcome"] for row in rows if row["event"] == "feedback")),
            "decisions": {
                "total": len(decisions),
                "statuses": dict(Counter(row["outcome"] for row in decisions)),
                "strategies": dict(Counter(row["strategy"] for row in decisions)),
                "modes": dict(Counter(row["mode"] for row in decisions)),
                "candidate_counts": dict(Counter(str(row["candidate_count"]) for row in decisions)),
                "latency_buckets": dict(Counter(row["latency_bucket"] for row in decisions)),
                "request_bytes_buckets": dict(Counter(row["request_bytes_bucket"] for row in decisions)),
            },
            "loaded_context_buckets": dict(Counter(row["content_bucket"] for row in loaded)),
            "token_buckets": {
                direction: dict(Counter(row[f"{direction}_bucket"] for row in rows
                                        if row["event"] == "api_outcome"))
                for direction in ("input", "output", "cache_read", "cache_write", "reasoning")},
        }
