"""Offline end-to-end selector scope regression; no real keys or network."""
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from threading import Barrier, Lock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevkit import client, keystore, skillpick

PROFILE = ContextVar('synthetic_host_profile', default='ambient')
CATALOG = [{'name': f'procedure-{i}', 'description': 'Synthetic procedure', 'path': 'unused'} for i in range(400)]


def reply(payload):
    answers = {}
    for name, question in payload['questions'].items():
        if question['type'] == 'choice':
            keys = list(question['criteria'])
            chosen = keys[0]
            answers[name] = {'type': 'choice', 'choice': chosen, 'confidence': .9,
                             'probabilities': {k: float(k == chosen) for k in keys}}
        else:
            answers[name] = {'type': 'noul', 'noul': .9}
    return json.dumps({'answers': answers, 'usage': {}}).encode()


class SelectorScopeTests(unittest.TestCase):
    def execute(self, profile, resolver=None, barrier=None):
        token = PROFILE.set(profile)
        seen = []
        lock = Lock()
        def transport(body, headers, timeout):
            payload = json.loads(body)
            with lock:
                seen.append((headers['Authorization'], PROFILE.get(), payload['model']))
            return reply(payload)
        try:
            # Resolver depends on a SECOND ContextVar, like Hermes secret_scope.
            with keystore.credential_scope(resolver or (lambda: 'synthetic-' + PROFILE.get())):
                if barrier:
                    barrier.wait(timeout=5)
                result = skillpick.pick('choose a synthetic procedure', CATALOG, transport=transport)
            return result, seen
        finally:
            PROFILE.reset(token)

    def test_all_shortlists_and_final_call_keep_host_context(self):
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'synthetic-ambient'}):
            result, seen = self.execute('A')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(auth == 'Bearer synthetic-A' and profile == 'A' for auth, profile, _ in seen), seen)

    def test_concurrent_profiles_never_cross(self):
        barrier = Barrier(2)
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'synthetic-ambient'}), ThreadPoolExecutor(max_workers=2) as pool:
            futures = {p: pool.submit(self.execute, p, barrier=barrier) for p in ['A', 'B']}
            for profile, future in futures.items():
                result, seen = future.result(timeout=10)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(len(seen), 5)
                self.assertTrue(all(a == 'Bearer synthetic-' + profile and p == profile for a, p, _ in seen), seen)

    def test_missing_scoped_key_never_uses_ambient_or_transport(self):
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'synthetic-ambient'}):
            result, seen = self.execute('missing', resolver=lambda: None)
        self.assertEqual(result['status'], 'fail_open')
        self.assertIn('no_key', result['reason'])
        self.assertEqual(seen, [])

    def test_broken_scoped_resolver_never_uses_ambient_or_transport(self):
        def broken():
            raise RuntimeError('synthetic unavailable scope')
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'synthetic-ambient'}):
            result, seen = self.execute('broken', resolver=broken)
        self.assertEqual(result['status'], 'fail_open')
        self.assertEqual(seen, [])

    def test_default_model_is_pinned_on_wire(self):
        with patch.dict('os.environ', {'TYPESAFE_MODEL': ''}):
            result, seen = self.execute('A')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual({m for _, _, m in seen}, {'jev-1.13.0'})

    def test_discover_parses_folded_and_literal_yaml_descriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folded = root / 'folded' / 'SKILL.md'
            literal = root / 'literal' / 'SKILL.md'
            folded.parent.mkdir()
            literal.parent.mkdir()
            folded.write_text(
                '---\nname: folded\ndescription: >-\n  Use when a folded\n  description applies.\n---\n\nBody.\n',
                encoding='utf-8',
            )
            literal.write_text(
                '---\nname: literal\ndescription: |-\n  First line.\n  Second line.\n---\n\nBody.\n',
                encoding='utf-8',
            )

            catalog = {item['name']: item for item in skillpick.discover([root])}

        self.assertEqual(catalog['folded']['description'], 'Use when a folded description applies.')
        self.assertEqual(catalog['literal']['description'], 'First line.\nSecond line.')

    def test_discover_preserves_root_precedence_and_disabled_names(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            local = base / 'local'
            external = base / 'external'
            for root, description in ((local, 'Local canonical.'), (external, 'External duplicate.')):
                skill = root / 'duplicate' / 'SKILL.md'
                skill.parent.mkdir(parents=True)
                skill.write_text(
                    f'---\nname: duplicate\ndescription: {description}\n---\n\nBody.\n',
                    encoding='utf-8',
                )
            disabled = local / 'disabled' / 'SKILL.md'
            disabled.parent.mkdir()
            disabled.write_text(
                '---\nname: disabled\ndescription: Must not load.\n---\n\nBody.\n',
                encoding='utf-8',
            )

            catalog = skillpick.discover([local, external], disabled={'disabled'})

        self.assertEqual(catalog, [{
            'name': 'duplicate',
            'description': 'Local canonical.',
            'path': str(local / 'duplicate' / 'SKILL.md'),
        }])

    def test_exact_name_match_cannot_bypass_match_threshold(self):
        catalog = [
            {'name': 'safe-review', 'description': 'Review changes without mutation.', 'path': 'one'},
            {'name': 'apple-notes', 'description': 'Use Apple Notes on macOS.', 'path': 'two'},
        ]

        def transport(body, headers, timeout):
            payload = json.loads(body)
            if 'pick' in payload['questions']:
                return json.dumps({
                    'answers': {'pick': {
                        'type': 'choice', 'choice': 'S0', 'confidence': .9,
                        'probabilities': {'S0': .9, 'S1': .1, 'none': 0.0},
                    }},
                    'usage': {},
                }).encode()
            return json.dumps({
                'answers': {
                    'needs_skill': {'type': 'noul', 'noul': .9},
                    's0': {'type': 'noul', 'noul': .9},
                    's1': {'type': 'noul', 'noul': .49},
                },
                'usage': {},
            }).encode()

        result = skillpick.pick(
            'Use apple notes after inspecting the changes', catalog, top_k=2,
            match_threshold=.5, transport=transport,
        )

        self.assertEqual([item['name'] for item in result['skills']], ['safe-review'])
        self.assertTrue(all(item['match'] >= .5 for item in result['skills']))


if __name__ == '__main__':
    unittest.main()
