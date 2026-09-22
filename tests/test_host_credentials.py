"""Host credential isolation uses synthetic values only."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevkit import keystore


class CredentialScopeTests(unittest.TestCase):
    def test_nested_missing_and_error_never_fall_back_to_environment(self):
        with patch.dict('os.environ', {keystore.ENV_VAR: 'synthetic-ambient'}):
            with keystore.credential_scope(lambda: 'synthetic-A'):
                self.assertEqual(keystore.resolve(), 'synthetic-A')
                with keystore.credential_scope(lambda: None):
                    self.assertIsNone(keystore.resolve())
                self.assertEqual(keystore.resolve(), 'synthetic-A')
                def broken():
                    raise RuntimeError('scope missing')
                with keystore.credential_scope(broken):
                    self.assertIsNone(keystore.resolve())
            self.assertEqual(keystore.resolve(), 'synthetic-ambient')

    def test_concurrent_contexts_do_not_share_resolver(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        def read(value):
            with keystore.credential_scope(lambda: value):
                barrier.wait(timeout=5)
                return keystore.resolve()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(read, 'synthetic-A')
            b = pool.submit(read, 'synthetic-B')
            self.assertEqual(a.result(timeout=10), 'synthetic-A')
            self.assertEqual(b.result(timeout=10), 'synthetic-B')

if __name__ == '__main__':
    unittest.main()
