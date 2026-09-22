"""Skill selection: load the one skill a turn needs, or none.

An agent with a hundred skills either reads a hundred descriptions every turn or
guesses. One Jev request ranks the whole catalog against the turn and also asks
whether any skill is needed at all, so most turns load nothing and the rest load
the right one.
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from . import client, privacy

MAX_SKILLS = 400
BATCH = 120
FINALISTS = 5
LEXICAL_FINALISTS = 5
FAST_FINALISTS = 10
DESCRIPTION_CHARS = 200

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "how",
    "i", "in", "is", "it", "my", "of", "on", "or", "the", "this", "to", "use", "with",
    "agent", "skill", "skills",
}
_COMPOUND_CUES = {"and", "then", "also", "plus", "into", "cite", "cited", "citation", "citations", "against"}
_AUTO_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|without|avoid|never|no|not|nothing|cannot|can['’]?t|unavailable|disabled|merely)\b",
    re.IGNORECASE,
)


def _front_matter(text: str) -> Dict[str, Any]:
    """Parse the top-level scalars needed outside a native host.

    Hermes supplies its own YAML parser to :func:`discover`.  This fallback is
    deliberately small but understands the folded/literal descriptions used by
    portable SKILL.md files instead of exposing the ``>-`` marker as prose.
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    fields: Dict[str, Any] = {}
    if not match:
        return fields
    lines = match.group(1).splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        key, sep, value = line.partition(":")
        if not sep or key.startswith((" ", "\t")):
            index += 1
            continue
        key = key.strip()
        value = value.strip()
        if value in {">", ">-", ">+", "|", "|-", "|+"}:
            block = []
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith((" ", "\t"))):
                block.append(lines[index].strip())
                index += 1
            if value.startswith(">"):
                fields[key] = " ".join(part for part in block if part).strip()
            else:
                fields[key] = "\n".join(block).strip()
            continue
        fields[key] = value.strip("'\"")
        index += 1
    return fields


def discover(
    roots: Iterable[Path], disabled: Iterable[str] = (), *,
    parser: Optional[Callable[[str], Mapping[str, Any]]] = None,
    iter_files: Optional[Callable[[Path], Iterable[Path]]] = None,
    eligible: Optional[Callable[[Mapping[str, Any]], bool]] = None,
) -> List[Dict[str, str]]:
    """Find SKILL.md files. Works for Hermes, Claude Code and Codex skill folders alike."""
    seen: Dict[str, Dict[str, str]] = {}
    skip = set(disabled)
    parse = parser or _front_matter
    for root in roots:
        if not root.is_dir():
            continue
        candidates = iter_files(root) if iter_files else sorted(root.rglob("SKILL.md"))
        for skill_file in candidates:
            if any(part.startswith(".") or part in ("quarantine", "node_modules") for part in skill_file.parts[len(root.parts):]):
                continue
            try:
                fields = parse(skill_file.read_text(encoding="utf-8", errors="replace")[:4000])
            except OSError:
                continue
            if eligible is not None and not eligible(fields):
                continue
            name = fields.get("name") or skill_file.parent.name
            description = fields.get("description")
            if name not in seen and name not in skip and isinstance(description, str) and description.strip():
                seen[name] = {"name": str(name), "description": description.strip(), "path": str(skill_file)}
    return list(seen.values())[:MAX_SKILLS]


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(text.casefold()) if token not in _STOP_WORDS and len(token) > 1}


def _lexical_finalists(turn: str, catalog: List[Dict[str, str]], limit: int = LEXICAL_FINALISTS) -> List[int]:
    """Find rare exact term overlaps so stage-one model misses still reach verification."""
    query = _tokens(turn)
    if not query:
        return []
    documents = [
        (_tokens(skill["name"].replace("-", " ")), _tokens(skill.get("description", "")))
        for skill in catalog
    ]
    document_frequency = {
        token: sum(token in names or token in description for names, description in documents)
        for token in query
    }
    scored: List[tuple[float, str, int]] = []
    total = len(catalog)
    for index, (name_tokens, description_tokens) in enumerate(documents):
        name_overlap = query & name_tokens
        description_overlap = query & description_tokens
        if not name_overlap and not description_overlap:
            continue
        score = 0.0
        for token in name_overlap | description_overlap:
            rarity = math.log((total + 1) / (document_frequency[token] + 1)) + 1.0
            score += rarity * (3.0 if token in name_overlap else 1.0)
        scored.append((score, catalog[index]["name"], index))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [index for _, _, index in scored[:limit]]


def _auto_finalists(turn: str, catalog: List[Dict[str, str]], limit: int = FAST_FINALISTS) -> List[int]:
    """Use the fast path only for one explicit, non-compound skill request."""
    query_tokens = _tokens(turn)
    raw_tokens = set(_TOKEN_RE.findall(turn.casefold()))
    explicit = [
        index for index, skill in enumerate(catalog)
        if (name_tokens := _tokens(skill["name"].replace("-", " ")))
        and name_tokens <= query_tokens
    ]
    if len(explicit) != 1 or raw_tokens & _COMPOUND_CUES or _AUTO_NEGATION_RE.search(turn):
        return []
    lexical = _lexical_finalists(turn, catalog, limit)
    return [*explicit, *(index for index in lexical if index not in explicit)][:limit]


def _merge_usage(replies: Iterable[Mapping[str, Any]]) -> Dict[str, float]:
    merged: Dict[str, float] = {}
    for reply in replies:
        usage = reply.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for name, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                merged[str(name)] = merged.get(str(name), 0.0) + float(value)
    return {name: int(value) if value.is_integer() else value for name, value in merged.items()}


def _select_verified(
    turn_text: str, catalog: List[Dict[str, str]], verified: List[tuple[float, int]], *,
    need: float, top_k: int, need_threshold: float, match_threshold: float,
) -> List[Dict[str, Any]]:
    selected = [] if need < need_threshold else [
        (probability, index)
        for probability, index in verified
        if probability >= match_threshold
    ][:top_k]
    if need >= need_threshold and top_k > 0:
        query_tokens = _tokens(turn_text)
        exact = [
            (probability, index)
            for probability, index in verified
            if probability >= match_threshold
            if (name_tokens := _tokens(catalog[index]["name"].replace("-", " ")))
            and name_tokens <= query_tokens
        ]
        if exact:
            exact_match = max(exact)
            if exact_match[1] not in {index for _, index in selected}:
                if len(selected) < top_k:
                    selected.append(exact_match)
                else:
                    selected[-1] = exact_match
    return [
        {"name": catalog[index]["name"], "path": catalog[index]["path"],
         "match": round(probability, 3)}
        for probability, index in selected
    ]


def pick(
    turn: str, skills: List[Dict[str, str]], *, top_k: int = 3, need_threshold: float = 0.5,
    match_threshold: float = 0.5, timeout: float = 5.0, transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    if not skills or privacy.is_sensitive(turn):
        return {"status": "fail_open", "reason": "no skills" if not skills else "turn looks sensitive; not sent",
                "skills": [], "strategy": "two-stage", "jev_calls": 0, "request_bytes": 0, "usage": {}}
    catalog = skills[:MAX_SKILLS]
    turn_text = privacy.redact(turn, 2000)

    # Stage 1: each batch is one Choice over its skills plus "none". The probabilities rank the whole
    # catalog in one round trip, because the batches run side by side.
    def shortlist(start: int) -> Dict[str, Any]:
        group = catalog[start:start + BATCH]
        options = {f"S{start + i}": f"{s['name']}: {s['description'][:DESCRIPTION_CHARS]}" for i, s in enumerate(group)}
        options["none"] = "No listed skill is a specialised procedure for this turn"
        question = client.choice("Which skill is the specialised procedure this turn calls for?", options)
        return client.ask({"turn": turn_text}, {"pick": question}, timeout=timeout, transport=transport)

    starts = list(range(0, len(catalog), BATCH))
    with ThreadPoolExecutor(max_workers=min(8, len(starts))) as pool:
        # Each worker needs its own caller-context copy: both our credential
        # resolver and the host's profile scope are ContextVars. Never share
        # one Context across concurrent tasks or fall back to ambient keys.
        futures = [pool.submit(copy_context().run, shortlist, start) for start in starts]
        replies = []
        errors = []
        for future in futures:
            try:
                replies.append(future.result())
            except client.JevError as error:
                errors.append(error)
    if errors:
        request_bytes = sum(int(reply.get("request_bytes", 0)) for reply in replies)
        request_bytes += sum(error.request_bytes for error in errors)
        attempts = sum(int(reply.get("attempts", 1)) for reply in replies)
        attempts += sum(error.attempts for error in errors)
        first_error = errors[0]
        return {"status": "fail_open", "reason": f"Jev unavailable ({first_error.code})", "skills": [],
                "strategy": "two-stage",
                "jev_calls": len(replies) + sum(error.attempts > 0 for error in errors),
                "request_bytes": request_bytes, "attempts": attempts,
                "usage": _merge_usage(replies)}
    latency = max(reply["latency_ms"] for reply in replies)
    request_bytes = sum(int(reply.get("request_bytes", 0)) for reply in replies)
    attempts = sum(int(reply.get("attempts", 1)) for reply in replies)
    ranked: List[tuple] = []
    for reply in replies:
        for option, probability in reply["answers"]["pick"]["probabilities"].items():
            if option != "none":
                ranked.append((probability, int(option[1:])))
    ranked.sort(reverse=True)
    finalists = [index for probability, index in ranked[:FINALISTS] if probability >= 0.02]
    finalists.extend(index for index in _lexical_finalists(turn_text, catalog) if index not in finalists)
    if not finalists:
        return {"status": "ok", "needs_skill": 0.0, "skills": [], "latency_ms": latency,
                "strategy": "two-stage", "jev_calls": len(replies), "request_bytes": request_bytes,
                "attempts": attempts, "usage": _merge_usage(replies),
                "stage_latency_ms": [reply["latency_ms"] for reply in replies]}

    # Stage 2: read the finalists properly, each judged on its own, and allow "none of them".
    state = {"turn": turn_text, "skills": {f"S{i}": f"{catalog[i]['name']}: {catalog[i]['description'][:600]}" for i in finalists}}
    questions: Dict[str, Any] = {
        "needs_skill": client.noul("Doing this turn well requires the specialised instructions of one of these skills")}
    for i in finalists:
        questions[f"s{i}"] = client.noul(f"Skill S{i} is the right specialised procedure for this turn")
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return {"status": "fail_open", "reason": f"Jev unavailable ({error.code})", "skills": [],
                "strategy": "two-stage", "jev_calls": len(replies) + int(error.attempts > 0),
                "request_bytes": request_bytes + error.request_bytes,
                "attempts": attempts + error.attempts, "usage": _merge_usage(replies)}
    answers = reply["answers"]
    need = answers["needs_skill"]["noul"]
    verified = sorted(((answers[f"s{i}"]["noul"], i) for i in finalists), reverse=True)
    chosen = _select_verified(
        turn_text, catalog, verified, need=need, top_k=top_k,
        need_threshold=need_threshold, match_threshold=match_threshold,
    )
    all_replies = [*replies, reply]
    return {
        "status": "ok", "needs_skill": round(need, 3), "skills": chosen,
        "latency_ms": latency + reply["latency_ms"], "strategy": "two-stage",
        "jev_calls": len(all_replies),
        "request_bytes": request_bytes + int(reply.get("request_bytes", 0)),
        "attempts": attempts + int(reply.get("attempts", 1)),
        "usage": _merge_usage(all_replies),
        "stage_latency_ms": [max(item["latency_ms"] for item in replies), reply["latency_ms"]],
    }


def pick_one_stage(
    turn: str, skills: List[Dict[str, str]], *, top_k: int = 3, need_threshold: float = 0.5,
    match_threshold: float = 0.5, shortlist: int = FAST_FINALISTS, timeout: float = 5.0,
    transport: Optional[client.Transport] = None, _indices: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Score a deterministic local shortlist in one Jev request."""
    if not skills or privacy.is_sensitive(turn):
        return {"status": "fail_open", "reason": "no skills" if not skills else "turn looks sensitive; not sent",
                "skills": [], "strategy": "one-stage", "jev_calls": 0, "request_bytes": 0, "usage": {}}
    catalog = skills[:MAX_SKILLS]
    turn_text = privacy.redact(turn, 2000)
    indices = list(_indices if _indices is not None else _lexical_finalists(turn_text, catalog, shortlist))
    if not indices:
        return {"status": "ok", "needs_skill": 0.0, "skills": [], "latency_ms": 0,
                "strategy": "one-stage", "jev_calls": 0, "request_bytes": 0, "attempts": 0,
                "usage": {}, "shortlist_size": 0, "stage_latency_ms": []}
    state = {
        "turn": turn_text,
        "skills": {f"s{index}": f"{catalog[index]['name']}: {catalog[index]['description'][:600]}"
                   for index in indices},
    }
    questions: Dict[str, Any] = {
        "needs_skill": client.noul("Doing this turn well requires the specialised instructions of one of these skills")
    }
    questions.update({
        f"s{index}": client.noul(f"Skill s{index} is the right specialised procedure for this turn")
        for index in indices
    })
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return {"status": "fail_open", "reason": f"Jev unavailable ({error.code})", "skills": [],
                "strategy": "one-stage", "jev_calls": int(error.attempts > 0),
                "request_bytes": error.request_bytes, "attempts": error.attempts, "usage": {},
                "shortlist_size": len(indices)}
    answers = reply["answers"]
    need = answers["needs_skill"]["noul"]
    verified = sorted(((answers[f"s{index}"]["noul"], index) for index in indices), reverse=True)
    chosen = _select_verified(
        turn_text, catalog, verified, need=need, top_k=top_k,
        need_threshold=need_threshold, match_threshold=match_threshold,
    )
    return {
        "status": "ok", "needs_skill": round(need, 3), "skills": chosen,
        "latency_ms": reply["latency_ms"], "strategy": "one-stage", "jev_calls": 1,
        "request_bytes": int(reply.get("request_bytes", 0)), "attempts": int(reply.get("attempts", 1)),
        "usage": _merge_usage([reply]), "shortlist_size": len(indices),
        "stage_latency_ms": [reply["latency_ms"]],
    }


def pick_optimized(
    turn: str, skills: List[Dict[str, str]], *, strategy: str = "auto", shortlist: int = FAST_FINALISTS,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Choose the control or one-stage path without weakening no-match recall."""
    if strategy not in {"auto", "control", "fast"}:
        raise ValueError("strategy must be auto, control, or fast")
    if strategy == "control":
        return pick(turn, skills, **kwargs)
    catalog = skills[:MAX_SKILLS]
    turn_text = privacy.redact(turn, 2000)
    lexical = _lexical_finalists(turn_text, catalog, shortlist)
    if strategy == "auto":
        indices = _auto_finalists(turn_text, catalog, shortlist)
        if not indices:
            return pick(turn, skills, **kwargs)
    else:
        indices = lexical
    return pick_one_stage(turn, skills, shortlist=shortlist, _indices=indices, **kwargs)
