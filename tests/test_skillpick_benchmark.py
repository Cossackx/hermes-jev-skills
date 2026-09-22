"""Focused offline tests for the held-out skill selector benchmark."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'evals'))
import skillpick_benchmark as bench

DATASET = ROOT / 'evals' / 'skillpick_heldout.json'


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.data = bench.load_dataset(DATASET)
        self.catalog = bench.load_catalog(None, self.data)

    def test_schema_balance_and_coverage(self):
        cases = self.data['cases']
        raw_cases = json.loads(DATASET.read_text(encoding='utf-8'))['cases']
        self.assertTrue(all({'alternatives', 'forbidden', 'abstain', 'tags', 'profile', 'disabled'} <= set(c)
                            for c in raw_cases))
        self.assertGreaterEqual(len(cases), 120)
        self.assertGreaterEqual(sum(not c['abstain'] for c in cases), 60)
        self.assertGreaterEqual(sum(c['abstain'] for c in cases), 60)
        tags = {t for c in cases for t in c['tags']}
        self.assertTrue({'lexical_zero', 'paraphrase', 'compound', 'overlap', 'fft',
                         'non_fft', 'negated_name', 'unavailable', 'disabled',
                         'ordinary', 'profile_change'} <= tags)
        self.assertTrue(any(len(c['alternatives']) > 1 for c in cases))
        self.assertTrue(any(any(len(group) > 1 for group in c['alternatives']) for c in cases))
        self.assertGreaterEqual(sum('lexical_zero' in c['tags'] for c in cases), 5)

    def test_invalid_labels_and_eligibility_fail_closed(self):
        data = json.loads(json.dumps(self.data))
        case = next(c for c in data['cases'] if not c['abstain'])
        case['alternatives'] = [['not-a-real-skill']]
        with self.assertRaisesRegex(ValueError, 'unknown'):
            bench.validate_dataset(data, self.catalog)
        case['alternatives'] = [['threejs-spectral-ocean']]
        case['disabled'] = ['threejs-spectral-ocean']
        with self.assertRaisesRegex(ValueError, 'ineligible'):
            bench.validate_dataset(data, self.catalog)
        case['disabled'] = []
        case['abstain'] = True
        with self.assertRaises(ValueError):
            bench.validate_dataset(data, self.catalog)

    def test_lexical_zero_tags_are_enforced(self):
        data = json.loads(json.dumps(self.data))
        case = next(c for c in data['cases'] if 'lexical_zero' in c['tags'])
        case['turn'] += ' ' + case['alternatives'][0][0]
        with self.assertRaisesRegex(ValueError, 'lexical_zero'):
            bench.validate_dataset(data, self.catalog)

    def test_deterministic_paired_schedule(self):
        cases = self.data['cases'][:5]
        arms = ['control', 'fast10', 'fast16', 'fast24', 'union']
        a = bench.schedule(cases, arms, repeats=3, seed=14)
        self.assertEqual(a, bench.schedule(cases, arms, repeats=3, seed=14))
        self.assertNotEqual(a, bench.schedule(cases, arms, repeats=3, seed=15))
        self.assertEqual(len(a), 75)
        self.assertEqual(len(set(a)), 75)
        for offset in range(0, len(a), len(arms)):
            block = a[offset:offset + len(arms)]
            self.assertEqual({x[1:] for x in block}, {block[0][1:]})
            self.assertEqual({x[0] for x in block}, set(arms))

    def test_auto_prefilter_only_accelerates_safe_positive_cases(self):
        accelerated = [
            case for case in self.data['cases']
            if bench.candidates_for(case, 'auto', bench.eligible_catalog(case, self.catalog, self.data))
        ]
        self.assertTrue(accelerated)
        self.assertTrue(all(not case['abstain'] for case in accelerated))
        self.assertTrue(all('compound' not in case['tags'] for case in accelerated))

    def test_fingerprint_binds_selector_implementation(self):
        with patch.object(bench, '_selector_digest', return_value='selector-a'):
            first = bench.fingerprint(self.data, self.catalog, ['control', 'auto'], 42)
        with patch.object(bench, '_selector_digest', return_value='selector-b'):
            second = bench.fingerprint(self.data, self.catalog, ['control', 'auto'], 42)
        self.assertNotEqual(first, second)

    def test_resume_skips_exact_completed_trials_and_rejects_other_runs(self):
        cases = self.data['cases'][:2]
        arms = ['control', 'fast10']
        plan = bench.schedule(cases, arms, 2, 5)
        signature = bench.fingerprint(self.data, self.catalog, arms, 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trials.jsonl'
            calls = []
            def runner(case, arm, catalog, timeout=5.0):
                calls.append((case['id'], arm))
                return {'status': 'ok', 'selected': [], 'latency_ms': 2,
                        'payload_bytes': 10, 'raw_responses': []}
            with patch.object(bench, 'live_trial', side_effect=runner):
                bench.run_live(cases, self.catalog, arms, 2, 5, path, signature, data=self.data)
                bench.run_live(cases, self.catalog, arms, 2, 5, path, signature, data=self.data)
            self.assertEqual(len(calls), len(plan))
            self.assertEqual(len(bench.read_trials(path, signature)), len(plan))
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                bench.read_trials(path, 'different')

    def test_resume_retries_failed_trial_and_keeps_failure_history(self):
        cases = self.data['cases'][:1]
        arms = ['control']
        signature = bench.fingerprint(self.data, self.catalog, arms, 9)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trials.jsonl'
            outcomes = iter([
                {'status': 'error', 'error': 'network', 'selected': [], 'strategy': 'two-stage',
                 'latency_ms': 1, 'payload_bytes': 10, 'raw_responses': []},
                {'status': 'ok', 'error': '', 'selected': [], 'strategy': 'two-stage',
                 'latency_ms': 1, 'payload_bytes': 10, 'raw_responses': []},
            ])
            with patch.object(bench, 'live_trial', side_effect=lambda *args, **kwargs: next(outcomes)):
                first = bench.run_live(cases, self.catalog, arms, 1, 9, path, signature, data=self.data)
                second = bench.run_live(cases, self.catalog, arms, 1, 9, path, signature, data=self.data)

            history = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([row['attempt'] for row in history], [1, 2])
            self.assertEqual(first[0]['status'], 'error')
            self.assertEqual(second[0]['status'], 'ok')
            self.assertEqual(bench.read_trials(path, signature)[('control', cases[0]['id'], 0)]['status'], 'ok')

    def test_fast_live_arm_executes_production_one_stage_selector(self):
        case = self.data['cases'][0]
        eligible = bench.eligible_catalog(case, self.catalog, self.data)
        expected = bench.candidates_for(case, 'fast10', eligible)
        expected_names = {row['name'] for row in expected}
        with patch.object(bench.skillpick, 'pick_one_stage', return_value={
            'status': 'ok', 'skills': [], 'strategy': 'one-stage', 'latency_ms': 0,
        }) as picker:
            result = bench.live_trial(case, 'fast10', eligible)

        indices = picker.call_args.kwargs['_indices']
        self.assertEqual({eligible[index]['name'] for index in indices}, expected_names)
        self.assertEqual(result['strategy'], 'one-stage')

    def test_metrics_distinguish_compound_alternatives_abstain_and_forbidden(self):
        cases = [
            {'id': 'p', 'abstain': False, 'alternatives': [['alpha', 'beta'], ['gamma']],
             'forbidden': ['delta']},
            {'id': 'n', 'abstain': True, 'alternatives': [], 'forbidden': ['delta']},
        ]
        rows = [
            {'case_id': 'p', 'arm': 'fast10', 'status': 'ok', 'selected': ['alpha', 'delta'],
             'latency_ms': 12, 'payload_bytes': 100, 'strategy': 'one-stage'},
            {'case_id': 'n', 'arm': 'fast10', 'status': 'ok', 'selected': [],
             'latency_ms': 8, 'payload_bytes': 80, 'strategy': 'one-stage'},
        ]
        result = bench.summarize(rows, cases)['arms']['fast10']
        self.assertEqual(result['trials'], 2)
        self.assertEqual(result['exact_success'], 1)
        self.assertEqual(result['forbidden_trials'], 1)
        self.assertEqual(result['positive_label_recall'], 0.5)
        self.assertEqual(result['negative_abstention_rate'], 1.0)
        self.assertEqual(result['candidate_precision'], 0.5)
        self.assertEqual(result['unnecessary_candidates'], 1)
        self.assertEqual(result['latency_ms_median'], 10)
        self.assertEqual(result['latency_ms_p95'], 12)
        self.assertEqual(result['payload_bytes_total'], 180)
        self.assertEqual(result['latency_ms_mean'], 10)
        self.assertEqual(result['strategies'], {'one-stage': 2})

    def test_offline_recall_payload_and_no_live_default(self):
        rows = bench.offline_trials(self.data['cases'], self.catalog, ['control', 'fast10', 'union'], data=self.data)
        self.assertEqual(len(rows), len(self.data['cases']) * 3)
        self.assertTrue(all('candidate_recall' in r and 'payload_bytes' in r for r in rows))
        self.assertTrue(all(r['candidate_recall'] is None for r in rows if r['arm'] == 'control'))
        by_case = {(row['case_id'], row['arm']): row for row in rows}
        for case in self.data['cases']:
            self.assertLessEqual(by_case[(case['id'], 'union')]['payload_bytes'],
                                 by_case[(case['id'], 'control')]['payload_bytes'])
        with patch.object(bench, 'live_trial', side_effect=AssertionError('live invoked')):
            result = bench.main(['--dataset', str(DATASET), '--arms', 'fast10'])
        self.assertEqual(result, 0)
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-do-not-use'}):
            proc = subprocess.run([sys.executable, '-B', str(ROOT / 'evals' / 'skillpick_benchmark.py'),
                                   '--dataset', str(DATASET), '--arms', 'fast10'],
                                  cwd=ROOT, capture_output=True, text=True, check=True,
                                  env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(json.loads(proc.stdout)['mode'], 'offline')

    def test_live_trial_mocked_transport_preserves_answers_not_prompts(self):
        case = self.data['cases'][0]
        requests = []
        def fake_transport(body, headers, timeout):
            request = json.loads(body)
            requests.append(request)
            answers = {}
            for name, question in request['questions'].items():
                if question['type'] == 'choice':
                    options = list(question['criteria'])
                    answers[name] = {'type': 'choice', 'choice': options[0], 'confidence': .9,
                                     'probabilities': {key: float(key == options[0]) for key in options},
                                     'unexpected': 'private-provider-text'}
                else:
                    answers[name] = {'type': 'noul', 'noul': .9, 'unexpected': 'private-provider-text'}
            return json.dumps({'answers': answers, 'usage': {'input_tokens': 2,
                                                              'note': 'private-provider-text'}}).encode()
        with patch.object(bench.client.keystore, 'resolve', return_value='synthetic'), \
             patch.object(bench.client, '_http_transport', side_effect=fake_transport):
            for arm in ('control', 'fast10', 'union'):
                before = len(requests)
                result = bench.live_trial(case, arm, self.catalog)
                self.assertEqual(result['status'], 'ok')
                self.assertGreater(result['payload_bytes'], 0)
                self.assertTrue(result['raw_responses'])
                self.assertIn(result['strategy'], {'one-stage', 'two-stage'})
                self.assertNotIn(case['turn'], json.dumps(result))
                self.assertNotIn('private-provider-text', json.dumps(result))
                if arm == 'union':
                    self.assertEqual(len(requests) - before, 1)
        self.assertTrue(requests)


if __name__ == '__main__':
    unittest.main()
