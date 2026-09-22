"""Held-out skillpick comparison; offline by default, live only with --live.

Usage:
  python -B evals/skillpick_benchmark.py --catalog snapshot.json
  python -B evals/skillpick_benchmark.py --live --output trials.jsonl --repeats 3

The bundled catalog is a frozen, synthetic description snapshot, not a scan of
installed skills. An optional JSON snapshot may be a list or {"skills": [...]}
(or {"catalog": [...]}). Each row has name and description; paths and secrets
are neither needed nor loaded. Offline recall for control is unknown because its
first stage is a model call; union offline recall is a lexical lower bound.
Live results are append-only JSONL. Only structured answer/usage objects, not
request bodies, auth headers, or transport error text, are retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from jevkit import client, privacy, skillpick

DEFAULT_DATASET = Path(__file__).with_name('skillpick_heldout.json')
ARMS = ('control', 'fast10', 'fast16', 'fast24', 'union', 'auto')
THRESHOLD = 0.5
TOP_K = 3


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def load_dataset(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('unsupported dataset schema_version')
    for case in data.get('cases', []):
        case.setdefault('profile', 'default')
        case.setdefault('disabled', [])
        case.setdefault('alternatives', [])
        case.setdefault('forbidden', [])
        case.setdefault('abstain', not bool(case['alternatives']))
    return data


def load_catalog(path: Path | None, data: dict) -> list[dict]:
    payload = json.loads(path.read_text(encoding='utf-8')) if path else data['catalog']
    if isinstance(payload, dict):
        payload = payload.get('skills', payload.get('catalog'))
    if not isinstance(payload, list) or not 1 <= len(payload) <= skillpick.MAX_SKILLS:
        raise ValueError('catalog must contain 1..400 skills')
    names = []
    for row in payload:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'].strip() or not isinstance(row.get('description'), str) or not row['description'].strip():
            raise ValueError('catalog entries require nonempty name and description')
        if any(privacy.redact(row[key]) != row[key]
               for key in ('name', 'description')):
            raise ValueError('catalog contains redactable private text')
        names.append(row['name'])
    if len(set(names)) != len(names):
        raise ValueError('duplicate catalog names')
    return [{'name': row['name'], 'description': row['description'], 'path': ''} for row in payload]


def eligible_catalog(case: dict, catalog: list[dict], data: dict) -> list[dict]:
    excluded = set(case['disabled']) | set(data['profiles'][case['profile']]['exclude'])
    return [row for row in catalog if row['name'] not in excluded]


def validate_dataset(data: dict, catalog: list[dict]) -> None:
    if not isinstance(data.get('profiles'), dict) or 'default' not in data['profiles']:
        raise ValueError('profiles must contain default')
    names = {row['name'] for row in catalog}
    for profile, spec in data['profiles'].items():
        if not isinstance(profile, str) or not isinstance(spec, dict) or not isinstance(spec.get('exclude'), list):
            raise ValueError('invalid profile')
        if len(spec['exclude']) != len(set(spec['exclude'])) or not set(spec['exclude']) <= names:
            raise ValueError('profile excludes unknown or duplicate labels')
    cases = data.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('cases must be a nonempty list')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id'] or case['id'] in seen:
            raise ValueError('case ids must be unique, nonempty strings')
        seen.add(case['id'])
        if not isinstance(case.get('turn'), str) or not case['turn'].strip() or len(case['turn']) > 2000 or privacy.is_sensitive(case['turn']) or privacy.redact(case['turn']) != case['turn']:
            raise ValueError(f"case {case['id']}: turn is invalid or not privacy-safe")
        if case.get('profile') not in data['profiles']:
            raise ValueError(f"case {case['id']}: unknown profile")
        if not isinstance(case.get('disabled'), list) or len(case['disabled']) != len(set(case['disabled'])) or not set(case['disabled']) <= names:
            raise ValueError(f"case {case['id']}: unknown or duplicate disabled label")
        tags = case.get('tags')
        if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or not t for t in tags) or len(tags) != len(set(tags)):
            raise ValueError(f"case {case['id']}: invalid tags")
        alternatives = case.get('alternatives')
        forbidden = case.get('forbidden')
        if not isinstance(alternatives, list) or not isinstance(forbidden, list) or any(not isinstance(n, str) for n in forbidden):
            raise ValueError(f"case {case['id']}: invalid labels")
        labels = []
        for group in alternatives:
            if not isinstance(group, list) or not group or any(not isinstance(n, str) for n in group) or len(group) != len(set(group)):
                raise ValueError(f"case {case['id']}: invalid alternative")
            labels.extend(group)
        if (not isinstance(case.get('abstain'), bool) or case['abstain'] != (not alternatives)
                or len(forbidden) != len(set(forbidden)) or set(labels) & set(forbidden)):
            raise ValueError(f"case {case['id']}: inconsistent abstention or forbidden labels")
        if (set(labels) | set(forbidden)) - names:
            raise ValueError(f"case {case['id']}: unknown label")
        eligible = {row['name'] for row in eligible_catalog(case, catalog, data)}
        if set(labels) - eligible:
            raise ValueError(f"case {case['id']}: ineligible positive label")
        if 'lexical_zero' in tags:
            query = skillpick._tokens(case['turn'])
            by_name = {row['name']: row for row in catalog}
            if any(query & (skillpick._tokens(by_name[name]['name'].replace('-', ' ')) |
                            skillpick._tokens(by_name[name]['description'])) for name in labels):
                raise ValueError(f"case {case['id']}: lexical_zero label has token overlap")
        if not eligible:
            raise ValueError(f"case {case['id']}: no eligible catalog")


def schedule(cases: list[dict], arms: list[str], repeats: int, seed: int) -> list[tuple[str, str, int]]:
    """Each shuffled case forms a contiguous paired block with rotated arm order."""
    if repeats < 1 or not arms or len(arms) != len(set(arms)) or not set(arms) <= set(ARMS):
        raise ValueError('invalid repeats or arms')
    rng = random.Random(seed)
    plan = []
    for repeat in range(repeats):
        case_ids = [case['id'] for case in cases]
        rng.shuffle(case_ids)
        for index, case_id in enumerate(case_ids):
            shift = (index + repeat) % len(arms)
            for arm in arms[shift:] + arms[:shift]:
                plan.append((arm, case_id, repeat))
    return plan


def fingerprint(data: dict, catalog: list[dict], arms: list[str], seed: int,
                timeout: float = 5.0) -> str:
    """Repeats are deliberately excluded so a run can be extended."""
    content = {'dataset': data, 'catalog': catalog, 'arms': arms, 'seed': seed,
               'threshold': THRESHOLD, 'top_k': TOP_K, 'timeout': timeout,
               'selector_digest': _selector_digest(), 'harness_digest': _harness_digest(),
               'harness_schema': 3}
    return hashlib.sha256(_json_bytes(content)).hexdigest()


def _selector_digest() -> str:
    """Bind resumable evidence to the exact selector implementation under test."""
    return hashlib.sha256(Path(skillpick.__file__).read_bytes()).hexdigest()


def _harness_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _questions(candidates: list[dict]) -> dict:
    questions = {'needs_skill': client.noul('Doing this turn well requires a specialised skill from this list')}
    for index in range(len(candidates)):
        questions[f's{index}'] = client.noul(f'Skill S{index} is the right specialised procedure for this turn')
    return questions


def _request_size(state: dict, questions: dict) -> int:
    return len(_json_bytes({'state': state, 'questions': questions, 'model': client.DEFAULT_MODEL}))


def _fast_request(case: dict, candidates: list[dict]) -> tuple[dict, dict]:
    state = {'turn': case['turn'], 'skills': {f'S{i}': f"{row['name']}: {row['description'][:600]}" for i, row in enumerate(candidates)}}
    return state, _questions(candidates)


def _control_estimate(case: dict, catalog: list[dict]) -> int:
    """Wire-size proxy with lexical-only stage-two finalists; model picks unknown."""
    total = 0
    for start in range(0, len(catalog), skillpick.BATCH):
        group = catalog[start:start + skillpick.BATCH]
        options = {f'S{start + i}': f"{row['name']}: {row['description'][:skillpick.DESCRIPTION_CHARS]}" for i, row in enumerate(group)}
        options['none'] = 'No listed skill is a specialised procedure for this turn'
        total += _request_size({'turn': case['turn']}, {'pick': client.choice('Which skill is the specialised procedure this turn calls for?', options)})
    lexical = [catalog[i] for i in skillpick._lexical_finalists(case['turn'], catalog)]
    if lexical:
        state, questions = _fast_request(case, lexical)
        total += _request_size(state, questions)
    return total


def _bm25_finalists(turn: str, catalog: list[dict], limit: int = 10) -> list[int]:
    query = skillpick._tokens(turn)
    if not query:
        return []
    documents = [skillpick._tokens(row['name'].replace('-', ' ')) |
                 skillpick._tokens(row['description']) for row in catalog]
    average = sum(len(tokens) for tokens in documents) / max(1, len(documents))
    scored = []
    for index, tokens in enumerate(documents):
        score = 0.0
        for token in query & tokens:
            frequency = sum(token in other for other in documents)
            inverse = math.log(1 + (len(documents) - frequency + .5) / (frequency + .5))
            score += inverse * 2.2 / (1 + .75 * (len(tokens) / max(1.0, average)))
        if score:
            scored.append((score, catalog[index]['name'], index))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [index for _, _, index in scored[:limit]]


def _auto_indices(turn: str, catalog: list[dict], limit: int = 10) -> list[int]:
    return skillpick._auto_finalists(turn, catalog, limit=limit)


def candidates_for(case: dict, arm: str, catalog: list[dict]) -> list[dict]:
    if arm == 'control':
        return []
    if arm == 'union':
        lexical = skillpick._lexical_finalists(case['turn'], catalog, limit=10)
        bm25 = _bm25_finalists(case['turn'], catalog, limit=10)
        indices = [*lexical, *(index for index in bm25 if index not in lexical)]
    elif arm == 'auto':
        indices = _auto_indices(case['turn'], catalog)
    else:
        indices = skillpick._lexical_finalists(case['turn'], catalog, limit=int(arm[4:]))
    return [catalog[i] for i in indices]


def _candidate_recall(case: dict, candidates: list[dict]) -> float | None:
    if case['abstain']:
        return None
    selected = {row['name'] for row in candidates}
    return max(len(set(group) & selected) / len(group) for group in case['alternatives'])


def offline_trials(cases: list[dict], catalog: list[dict], arms: list[str], data: dict | None = None) -> list[dict]:
    if data is None:
        data = {'profiles': {'default': {'exclude': []}}}
        if any(c['profile'] != 'default' for c in cases):
            raise ValueError('profile-aware offline evaluation requires dataset')
    rows = []
    for case in cases:
        eligible = eligible_catalog(case, catalog, data)
        for arm in arms:
            candidate = candidates_for(case, arm, eligible)
            fallback = arm == 'auto' and not candidate
            payload = _control_estimate(case, eligible) if arm == 'control' or fallback else _request_size(*_fast_request(case, candidate))
            rows.append({'case_id': case['id'], 'arm': arm, 'status': 'offline',
                         'candidate_recall': None if arm == 'control' or fallback else _candidate_recall(case, candidate),
                         'candidate_count': None if arm == 'control' or fallback else len(candidate),
                         'candidate_basis': 'model_unknown' if arm == 'control' or fallback else ('lexical_bm25' if arm == 'union' else 'lexical'),
                         'strategy': 'two-stage' if arm == 'control' or fallback else 'one-stage',
                         'payload_bytes': payload, 'payload_basis': 'estimated',
                         'latency_ms': None})
    return rows


def _capture_transport(responses: list, counter: list[int], lock: threading.Lock):
    def transport(body: bytes, headers: dict, timeout: float) -> bytes:
        # Do not persist the request body or headers. The first-party transport
        # still enforces its own endpoint and redirect restrictions.
        with lock:
            counter[0] += len(body)
        raw = client._http_transport(body, headers, timeout)
        try:
            request = json.loads(body)
            payload = json.loads(raw)
            answers = payload.get('answers') if isinstance(payload, dict) else None
            questions = request.get('questions') if isinstance(request, dict) else None
            if not isinstance(answers, dict) or not isinstance(questions, dict):
                return raw
            checked = {
                name: client._check_answer(name, question, answers.get(name))
                for name, question in questions.items()
            }
            allowed_usage = {
                'tokens', 'total_tokens', 'input_tokens', 'output_tokens',
                'prompt_tokens', 'completion_tokens', 'cache_read_tokens',
                'cached_tokens', 'cache_write_tokens', 'reasoning_tokens',
            }
            raw_usage = payload.get('usage')
            usage = {
                key: value for key, value in raw_usage.items()
                if key in allowed_usage and isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(value) and value >= 0
            } if isinstance(raw_usage, dict) else {}
            safe = {'answers': checked, 'usage': usage}
            with lock:
                responses.append(safe)
        except (client.JevError, ValueError, TypeError):
            pass  # malformed replies remain errors in client.ask
        return raw
    return transport


def _stage_one(case: dict, catalog: list[dict], transport, timeout: float) -> list[dict]:
    ranked = []
    for start in range(0, len(catalog), skillpick.BATCH):
        group = catalog[start:start + skillpick.BATCH]
        options = {f'S{start + i}': f"{row['name']}: {row['description'][:skillpick.DESCRIPTION_CHARS]}" for i, row in enumerate(group)}
        options['none'] = 'No listed skill is a specialised procedure for this turn'
        reply = client.ask({'turn': case['turn']}, {'pick': client.choice('Which skill is the specialised procedure this turn calls for?', options)}, timeout=timeout, transport=transport)
        for key, p in reply['answers']['pick']['probabilities'].items():
            if key != 'none':
                ranked.append((p, int(key[1:])))
    ranked.sort(reverse=True)
    return [catalog[i] for p, i in ranked[:skillpick.FINALISTS] if p >= 0.02]


def live_trial(case: dict, arm: str, catalog: list[dict], timeout: float = 5.0) -> dict:
    responses: list[dict] = []
    payload_bytes = [0]
    lock = threading.Lock()
    transport = _capture_transport(responses, payload_bytes, lock)
    strategy = 'two-stage' if arm == 'control' else 'one-stage'
    try:
        if arm == 'control':
            result = skillpick.pick(case['turn'], catalog, top_k=TOP_K, need_threshold=THRESHOLD,
                                    match_threshold=THRESHOLD, timeout=timeout, transport=transport)
            selected = [item['name'] for item in result['skills']]
            status = result['status']
            error = result.get('reason', '')
            latency = result.get('latency_ms')
            strategy = result.get('strategy', strategy)
        elif arm == 'auto':
            result = skillpick.pick_optimized(
                case['turn'], catalog, strategy='auto', top_k=TOP_K,
                need_threshold=THRESHOLD, match_threshold=THRESHOLD,
                timeout=timeout, transport=transport,
            )
            selected = [item['name'] for item in result['skills']]
            status, error, latency = result['status'], result.get('reason', ''), result.get('latency_ms')
            strategy = result.get('strategy', strategy)
        else:
            candidates = candidates_for(case, arm, catalog)
            names = {row['name'] for row in candidates}
            indices = [index for index, row in enumerate(catalog) if row['name'] in names]
            result = skillpick.pick_one_stage(
                case['turn'], catalog, top_k=TOP_K,
                need_threshold=THRESHOLD, match_threshold=THRESHOLD,
                timeout=timeout, transport=transport, _indices=indices,
            )
            selected = [item['name'] for item in result['skills']]
            status, error, latency = result['status'], result.get('reason', ''), result.get('latency_ms')
            strategy = result.get('strategy', strategy)
    except client.JevError as exc:
        status, error, selected, latency = 'error', exc.code, [], None
    return {'status': status, 'error': error, 'selected': selected, 'strategy': strategy,
            'latency_ms': latency, 'payload_bytes': payload_bytes[0],
            'raw_responses': responses}


def _read_trial_history(path: Path, signature: str) -> dict[tuple[str, str, int], list[dict]]:
    history: dict[tuple[str, str, int], list[dict]] = {}
    if not path.exists():
        return history
    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        try:
            row = json.loads(line)
            if row['fingerprint'] != signature:
                raise ValueError('fingerprint mismatch')
            key = (row['arm'], row['case_id'], row['repeat'])
            attempt = row['attempt']
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise ValueError('invalid trial attempt')
            if any(previous['attempt'] == attempt for previous in history.get(key, [])):
                raise ValueError('duplicate trial attempt')
            history.setdefault(key, []).append(row)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f'corrupt trial line {line_number}') from exc
    return history


def read_trials(path: Path, signature: str) -> dict[tuple[str, str, int], dict]:
    """Return the latest successful attempt, or the latest failure for unfinished trials."""
    result = {}
    for key, attempts in _read_trial_history(path, signature).items():
        successful = [row for row in attempts if row.get('status') == 'ok']
        result[key] = successful[-1] if successful else attempts[-1]
    return result


def run_live(cases: list[dict], catalog: list[dict], arms: list[str], repeats: int,
             seed: int, path: Path, signature: str, *, data: dict | None = None,
             timeout: float = 5.0) -> list[dict]:
    import os
    import time
    history = _read_trial_history(path, signature)
    completed = {
        key: successful[-1]
        for key, attempts in history.items()
        if (successful := [row for row in attempts if row.get('status') == 'ok'])
    }
    latest = {key: attempts[-1] for key, attempts in history.items()}
    plan = schedule(cases, arms, repeats, seed)
    if set(history) - set(plan):
        raise ValueError('resume file has trials outside this schedule')
    by_id = {case['id']: case for case in cases}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        for arm, case_id, repeat in plan:
            key = (arm, case_id, repeat)
            if key in completed:
                continue
            case = by_id[case_id]
            eligible = eligible_catalog(case, catalog, data) if data else catalog
            started = time.monotonic()
            try:
                result = live_trial(case, arm, eligible, timeout=timeout)
            except Exception as exc:
                # A failed trial stays an explicit row; don't leak exception text.
                result = {'status': 'error', 'error': type(exc).__name__, 'selected': [],
                          'strategy': 'unknown', 'latency_ms': None, 'payload_bytes': 0,
                          'raw_responses': []}
            result['latency_ms'] = round((time.monotonic() - started) * 1000, 3)
            row = {'fingerprint': signature, 'arm': arm, 'case_id': case_id,
                   'repeat': repeat, 'attempt': len(history.get(key, [])) + 1, **result}
            stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            history.setdefault(key, []).append(row)
            latest[key] = row
            if row.get('status') == 'ok':
                completed[key] = row
    return [completed.get(key, latest[key]) for key in plan]


def summarize(rows: list[dict], cases: list[dict]) -> dict:
    labels = {case['id']: case for case in cases}
    groups = {}
    for row in rows:
        groups.setdefault(row['arm'], []).append(row)
    report = {'arms': {}}
    for arm, trials in groups.items():
        positives = [row for row in trials if not labels[row['case_id']]['abstain']]
        negatives = [row for row in trials if labels[row['case_id']]['abstain']]
        errors = Counter(row.get('error', 'unknown') for row in trials if row['status'] not in ('ok', 'offline'))
        recall = [row['candidate_recall'] for row in positives if row.get('candidate_recall') is not None]
        observed = [row for row in trials if row['status'] == 'ok']
        observed_pos = [row for row in observed if not labels[row['case_id']]['abstain']]
        observed_neg = [row for row in observed if labels[row['case_id']]['abstain']]
        def exact(row):
            case = labels[row['case_id']]
            selected = set(row['selected'])
            return not selected if case['abstain'] else any(selected == set(group) for group in case['alternatives'])
        def label_recall(row):
            case = labels[row['case_id']]
            return max(len(set(row['selected']) & set(group)) / len(group) for group in case['alternatives'])
        latencies = [row['latency_ms'] for row in observed if isinstance(row.get('latency_ms'), (int, float))]
        payloads = [row['payload_bytes'] for row in trials if isinstance(row.get('payload_bytes'), (int, float))]
        selected_count = sum(len(row.get('selected', [])) for row in observed)
        relevant_count = sum(
            sum(name in {label for group in labels[row['case_id']]['alternatives'] for label in group}
                for name in row.get('selected', []))
            for row in observed
        )
        def percentile(values, fraction):
            if not values:
                return None
            ordered = sorted(values)
            return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]
        report['arms'][arm] = {
            'trials': len(trials), 'evaluated': len(observed), 'coverage': len(observed) / len(trials),
            'errors': dict(sorted(errors.items())),
            'strategies': dict(sorted(Counter(row.get('strategy', 'unknown') for row in trials).items())),
            'candidate_recall_mean': sum(recall) / len(recall) if recall else None,
            'candidate_recall_full': sum(x == 1 for x in recall) / len(recall) if recall else None,
            'candidate_positives': len(recall),
            'exact_success': sum(exact(row) for row in observed),
            'exact_success_rate': sum(exact(row) for row in observed) / len(observed) if observed else None,
            'positive_label_recall': sum(label_recall(row) for row in observed_pos) / len(observed_pos) if observed_pos else None,
            'negative_abstention_rate': sum(not row['selected'] for row in observed_neg) / len(observed_neg) if observed_neg else None,
            'forbidden_trials': sum(bool(set(row['selected']) & set(labels[row['case_id']]['forbidden'])) for row in observed),
            'candidate_precision': relevant_count / selected_count if selected_count else None,
            'unnecessary_candidates': selected_count - relevant_count,
            'payload_bytes_total': sum(row['payload_bytes'] for row in trials),
            'payload_bytes_mean': sum(row['payload_bytes'] for row in trials) / len(trials),
            'payload_bytes_median': statistics.median(payloads) if payloads else None,
            'latency_ms_mean': sum(latencies) / len(latencies) if latencies else None,
            'latency_ms_median': statistics.median(latencies) if latencies else None,
            'latency_ms_p95': percentile(latencies, .95),
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--catalog', type=Path, help='explicit frozen JSON skill snapshot (never scans installed skills)')
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS))
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--live', action='store_true', help='ONLY flag that enables Jev calls')
    parser.add_argument('--output', type=Path, help='append-only JSONL live trial path')
    args = parser.parse_args(argv)
    if args.live and args.output is None:
        parser.error('--live requires --output')
    if args.repeats < 1 or args.timeout <= 0 or len(args.arms) != len(set(args.arms)):
        parser.error('repeats and timeout must be positive; arms must be unique')
    data = load_dataset(args.dataset)
    catalog = load_catalog(args.catalog, data)
    validate_dataset(data, catalog)
    signature = fingerprint(data, catalog, args.arms, args.seed, args.timeout)
    if args.live:
        rows = run_live(data['cases'], catalog, args.arms, args.repeats, args.seed,
                        args.output, signature, data=data, timeout=args.timeout)
    else:
        rows = offline_trials(data['cases'], catalog, args.arms, data=data)
    report = {'mode': 'live' if args.live else 'offline', 'fingerprint': signature,
              'cases': len(data['cases']), 'positive_cases': sum(not c['abstain'] for c in data['cases']),
              'negative_cases': sum(c['abstain'] for c in data['cases']),
              'notes': 'Offline payloads estimated; control recall unknown and union recall lexical lower bound.' if not args.live else 'Live payload bytes measured at transport; latency includes entire trial.',
              **summarize(rows, data['cases'])}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
