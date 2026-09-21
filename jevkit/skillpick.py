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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import client, privacy

MAX_SKILLS = 400
BATCH = 120
FINALISTS = 5
LEXICAL_FINALISTS = 5
DESCRIPTION_CHARS = 200

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "how",
    "i", "in", "is", "it", "my", "of", "on", "or", "the", "this", "to", "use", "with",
    "agent", "skill", "skills",
}


def _front_matter(text: str) -> Dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    fields: Dict[str, str] = {}
    if match:
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep and not key.startswith((" ", "\t")):
                fields[key.strip()] = value.strip().strip("'\"")
    return fields


def discover(roots: Iterable[Path], disabled: Iterable[str] = ()) -> List[Dict[str, str]]:
    """Find SKILL.md files. Works for Hermes, Claude Code and Codex skill folders alike."""
    seen: Dict[str, Dict[str, str]] = {}
    skip = set(disabled)
    for root in roots:
        if not root.is_dir():
            continue
        for skill_file in sorted(root.rglob("SKILL.md")):
            if any(part.startswith(".") or part in ("quarantine", "node_modules") for part in skill_file.parts[len(root.parts):]):
                continue
            try:
                fields = _front_matter(skill_file.read_text(encoding="utf-8", errors="replace")[:4000])
            except OSError:
                continue
            name = fields.get("name") or skill_file.parent.name
            if name not in seen and name not in skip and fields.get("description"):
                seen[name] = {"name": name, "description": fields["description"], "path": str(skill_file)}
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


def pick(
    turn: str, skills: List[Dict[str, str]], *, top_k: int = 3, need_threshold: float = 0.5,
    match_threshold: float = 0.5, timeout: float = 5.0, transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    if not skills or privacy.is_sensitive(turn):
        return {"status": "fail_open", "reason": "no skills" if not skills else "turn looks sensitive; not sent", "skills": []}
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
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(starts))) as pool:
            replies = list(pool.map(shortlist, starts))
    except client.JevError as error:
        return {"status": "fail_open", "reason": f"Jev unavailable ({error.code})", "skills": []}
    latency = max(reply["latency_ms"] for reply in replies)
    ranked: List[tuple] = []
    for reply in replies:
        for option, probability in reply["answers"]["pick"]["probabilities"].items():
            if option != "none":
                ranked.append((probability, int(option[1:])))
    ranked.sort(reverse=True)
    finalists = [index for probability, index in ranked[:FINALISTS] if probability >= 0.02]
    finalists.extend(index for index in _lexical_finalists(turn_text, catalog) if index not in finalists)
    if not finalists:
        return {"status": "ok", "needs_skill": 0.0, "skills": [], "latency_ms": latency}

    # Stage 2: read the finalists properly, each judged on its own, and allow "none of them".
    state = {"turn": turn_text, "skills": {f"S{i}": f"{catalog[i]['name']}: {catalog[i]['description'][:600]}" for i in finalists}}
    questions: Dict[str, Any] = {
        "needs_skill": client.noul("Doing this turn well requires the specialised instructions of one of these skills")}
    for i in finalists:
        questions[f"s{i}"] = client.noul(f"Skill S{i} is the right specialised procedure for this turn")
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return {"status": "fail_open", "reason": f"Jev unavailable ({error.code})", "skills": []}
    answers = reply["answers"]
    need = answers["needs_skill"]["noul"]
    verified = sorted(((answers[f"s{i}"]["noul"], i) for i in finalists), reverse=True)
    selected = [] if need < need_threshold else [
        (p, i) for p, i in verified if p >= match_threshold
    ][:top_k]
    if need >= need_threshold and top_k > 0:
        query_tokens = _tokens(turn_text)
        exact = [
            (p, i)
            for p, i in verified
            if (name_tokens := _tokens(catalog[i]["name"].replace("-", " ")))
            and name_tokens <= query_tokens
        ]
        if exact:
            exact_match = max(exact)
            if exact_match[1] not in {i for _, i in selected}:
                if len(selected) < top_k:
                    selected.append(exact_match)
                else:
                    selected[-1] = exact_match
    chosen = [
        {"name": catalog[i]["name"], "path": catalog[i]["path"], "match": round(p, 3)}
        for p, i in selected
    ]
    return {"status": "ok", "needs_skill": round(need, 3), "skills": chosen, "latency_ms": latency + reply["latency_ms"]}
