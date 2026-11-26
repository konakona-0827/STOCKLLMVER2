from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from operations.account_transfer import (DATABASES, RECEIPT, export_account, restore_account, verify)
from state_lock import StateLease


class AccountTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.share = Path(self.tmp.name) / 'share'
        self.target = Path(self.tmp.name) / 'restored'
        self.make_account(self.root)

    def make_account(self, root):
        for name in DATABASES:
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(p)) as db:
                if name == DATABASES[0]:
                    db.executescript('CREATE TABLE execution_attempts(id TEXT, status TEXT);'
                        'CREATE TABLE execution_events(id INTEGER, detail TEXT);'
                        'CREATE TABLE ai_positions(symbol TEXT, qty INTEGER);'
                        "INSERT INTO execution_attempts VALUES('order-1','PARTIAL_FILLED');"
                        "INSERT INTO execution_events VALUES(1,'confirmed 2 shares');"
                        "INSERT INTO ai_positions VALUES('0050',2);")
                else:
                    db.executescript('CREATE TABLE paper_account(account_id INTEGER, capital_limit_cents INTEGER, cash_cents INTEGER);'
                        'INSERT INTO paper_account VALUES(1,1000000,800000);')
        (root / 'config').mkdir()
        (root / 'config/execution_live.json').write_text(json.dumps(dict(enabled=False, min_available_to_buy_twd=5000)), encoding='utf-8')
        (root / 'config/universe_tw.json').write_text('{}', encoding='utf-8')
        (root / '.env').write_text('CAPITAL_PASSWORD=not-for-git-password\nOPENAI_API_KEY=\nsk-private-should-never-be-exported\n', encoding='utf-8')

    def export(self):
        return export_account(self.root, self.share)

    def test_roundtrip_preserves_partial_fill_cash_cap_and_original_authorization(self):
        manifest = self.export()
        result = restore_account(self.target, self.share)
        self.assertEqual(result['snapshot_id'], manifest['snapshot_id'])
        self.assertFalse(result['account_binding_changed'])
        self.assertFalse((self.target / '.env').exists())
        self.assertFalse(json.loads((self.target / 'config/execution_live.json').read_text())['enabled'])
        with closing(sqlite3.connect(self.target / DATABASES[0])) as db:
            self.assertEqual(db.execute('SELECT * FROM ai_positions').fetchall(), [('0050', 2)])
            self.assertEqual(db.execute('SELECT status FROM execution_attempts').fetchone()[0], 'PARTIAL_FILLED')
        with closing(sqlite3.connect(self.target / DATABASES[1])) as db:
            self.assertEqual(db.execute('SELECT * FROM paper_account').fetchone(), (1,1000000,800000))
        self.assertEqual(result['orders_sent'], 0)

    def test_archive_allowlist_excludes_credentials_binding_and_locks(self):
        (self.root / '.trading-state.json').write_text('{"state_root":"private"}')
        manifest = self.export()
        with zipfile.ZipFile(self.share / 'snapshots' / manifest['snapshot_id'] / 'account.zip') as z:
            self.assertEqual(set(z.namelist()), set(DATABASES) | {'config/execution_live.json','config/universe_tw.json'})

    def test_existing_target_is_never_replaced(self):
        self.export()
        self.target.mkdir()
        original = self.target / 'existing.txt'
        original.write_text('newer local account')
        with self.assertRaisesRegex(ValueError, 'TARGET_MUST_NOT_EXIST'):
            restore_account(self.target, self.share)
        self.assertEqual(original.read_text(), 'newer local account')

    def test_active_broker_or_dashboard_prevents_export(self):
        for name in ('broker-runtime', 'dashboard', 'flow-supervisor'):
            with self.subTest(name=name), StateLease(self.root, name):
                with self.assertRaises(RuntimeError):
                    self.export()
        self.assertFalse((self.share / 'latest.json').exists())

    def test_uncommitted_database_writer_prevents_export(self):
        with closing(sqlite3.connect(self.root / DATABASES[0])) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(RuntimeError, 'DATABASE_BUSY'):
                self.export()

    def test_committed_wal_rows_are_included(self):
        with closing(sqlite3.connect(self.root / DATABASES[0])) as source:
            source.execute('PRAGMA journal_mode=WAL')
            source.execute("INSERT INTO execution_events VALUES(2,'WAL event')")
            source.commit()
            self.export()
            restore_account(self.target, self.share)
        with closing(sqlite3.connect(self.target / DATABASES[0])) as target:
            self.assertEqual(target.execute('SELECT count(*) FROM execution_events').fetchone()[0], 2)

    def test_stale_source_cannot_replace_latest_handoff(self):
        self.export()
        other = Path(self.tmp.name) / 'other'
        self.make_account(other)
        with self.assertRaisesRegex(ValueError, 'STALE_ACCOUNT_HANDOFF'):
            export_account(other, self.share)

    def test_restored_source_can_append_new_snapshot_without_mutating_old_one(self):
        first = self.export()
        original = (self.share / 'snapshots' / first['snapshot_id'] / 'account.zip').read_bytes()
        restore_account(self.target, self.share)
        with closing(sqlite3.connect(self.target / DATABASES[0])) as db:
            db.execute("INSERT INTO execution_events VALUES(2,'next computer')")
            db.commit()
        second = export_account(self.target, self.share)
        self.assertEqual(second['parent_snapshot_id'], first['snapshot_id'])
        self.assertEqual(original, (self.share / 'snapshots' / first['snapshot_id'] / 'account.zip').read_bytes())
        self.assertEqual(json.loads((self.target / RECEIPT).read_text())['snapshot_id'], second['snapshot_id'])

    def test_tampered_archive_rejected_before_creating_target(self):
        manifest = self.export()
        p = self.share / 'snapshots' / manifest['snapshot_id'] / 'account.zip'
        p.write_bytes(p.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
            restore_account(self.target, self.share)
        self.assertFalse(self.target.exists())

    def test_unapproved_archive_path_rejected(self):
        m = self.export()
        p = self.share / 'snapshots' / m['snapshot_id'] / 'manifest.json'
        m['files']['../.env'] = dict(bytes=1,sha256='x')
        p.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, 'FILE_NOT_ALLOWED'):
            restore_account(self.target, self.share)

    def test_password_or_multiline_key_inside_json_is_not_published(self):
        for secret in ('not-for-git-password', 'sk-private-should-never-be-exported'):
            with self.subTest(secret='REDACTED'):
                (self.root / 'config/execution_live.json').write_text(json.dumps(dict(note=secret)))
                with self.assertRaisesRegex(ValueError, 'CREDENTIAL_VALUE'):
                    self.export()
                self.assertFalse((self.share / 'latest.json').exists())

    def test_corrupt_database_is_rejected(self):
        (self.root / DATABASES[0]).write_bytes(b'not a database')
        with self.assertRaises(sqlite3.DatabaseError):
            self.export()
        self.assertFalse((self.share / 'latest.json').exists())

    def test_unknown_api_key_is_not_published(self):
        (self.root / 'config/execution_live.json').write_text(json.dumps(dict(note='sk-' + 'x' * 40)))
        with self.assertRaisesRegex(ValueError, 'CREDENTIAL_PATTERN'):
            self.export()
        self.assertFalse((self.share / 'latest.json').exists())

    def test_no_broker_or_model_modules_loaded_by_transfer(self):
        import ast
        import operations.account_transfer as module
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse(any(x and x.startswith(('execution', 'capital', 'codex_cli', 'openai')) for x in imports))


if __name__ == '__main__':
    unittest.main()
