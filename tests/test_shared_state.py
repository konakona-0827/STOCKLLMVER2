import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing

from runtime_paths import bind_state_root, resolve_state_root, REQUIRED_STATE_FILES
from state_lock import StateLease


class SharedStateTests(unittest.TestCase):
    def make_state(self, root):
        for name in REQUIRED_STATE_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return root

    def test_new_process_uses_canonical_env_and_updates_the_same_sqlite_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code = root / 'code'
            code.mkdir()
            state = self.make_state(root / 'account')
            (state / '.env').write_text('STATE_TEST_MARKER=canonical', encoding='utf-8')
            (code / '.env').write_text('STATE_TEST_MARKER=stale', encoding='utf-8')
            with closing(sqlite3.connect(state / REQUIRED_STATE_FILES[2])) as db:
                db.execute('CREATE TABLE ai_positions(symbol TEXT, qty INTEGER)')
                db.execute("INSERT INTO ai_positions VALUES('2891', 25)")
                db.commit()
            source = Path(__file__).resolve().parents[1]
            for name in ('config.py', 'runtime_paths.py'):
                shutil.copyfile(source / name, code / name)
            bind_state_root(code, state)
            script = '''from config import ROOT, load_env
import os, sqlite3
load_env()
assert os.environ['STATE_TEST_MARKER'] == 'canonical'
with sqlite3.connect(ROOT/'data/execution/live_execution.sqlite3') as db:
    assert db.execute('SELECT qty FROM ai_positions').fetchone()[0] == 25
    db.execute('UPDATE ai_positions SET qty=24')
'''
            env = dict(os.environ)
            env.pop('TRADING_STATE_ROOT', None)
            env.pop('STATE_TEST_MARKER', None)
            result = subprocess.run([sys.executable, '-c', script], cwd=code, env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            with closing(sqlite3.connect(state / REQUIRED_STATE_FILES[2])) as db:
                self.assertEqual(db.execute('SELECT qty FROM ai_positions').fetchone()[0], 24)

    def test_missing_canonical_state_never_falls_back_to_a_stale_local_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code = self.make_state(root / 'code')
            state = self.make_state(root / 'account')
            bind_state_root(code, state)
            (state / '.env').unlink()
            with self.assertRaisesRegex(ValueError, '.env'):
                resolve_state_root(code, {})

    def test_an_unbound_git_worktree_cannot_start_with_its_old_database_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            code = self.make_state(Path(folder) / 'code')
            (code / '.git').write_text('gitdir: ../repository/worktrees/code', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'worktree'):
                resolve_state_root(code, {})

    def test_explicit_state_override_has_priority_and_is_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code = root / 'code'
            code.mkdir()
            first = self.make_state(root / 'first')
            second = self.make_state(root / 'second')
            bind_state_root(code, first)
            self.assertEqual(resolve_state_root(code, {}), first.resolve())
            self.assertEqual(resolve_state_root(code, {'TRADING_STATE_ROOT': str(second)}), second.resolve())
            with self.assertRaises(ValueError):
                resolve_state_root(code, {'TRADING_STATE_ROOT': str(root/'missing')})

    def test_another_process_cannot_open_the_same_account_controller_until_released(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = '''from state_lock import StateLease
import sys
try:
    with StateLease(sys.argv[1], 'broker-runtime'): pass
except RuntimeError:
    raise SystemExit(3)
'''
            def attempt():
                return subprocess.run([sys.executable, '-c', script, str(root)],
                                      capture_output=True, timeout=15).returncode
            with StateLease(root, 'broker-runtime'):
                self.assertEqual(attempt(), 3)
            self.assertEqual(attempt(), 0)


if __name__ == '__main__':
    unittest.main()
