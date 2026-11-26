"""Immutable Git account handoffs; no credentials, broker login or orders."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import zipfile

from runtime_paths import resolve_state_root
from state_lock import StateLease

CODE_ROOT = Path(__file__).resolve().parents[1]
SHARE_ROOT = CODE_ROOT / 'account-share'
DATABASES = ('data/execution/live_execution.sqlite3', 'data/analysis/market_history.sqlite3')
REQUIRED = DATABASES + ('config/execution_live.json', 'config/universe_tw.json')
OPTIONAL = ('config/scan_rules.json', 'config/web_search.json', 'data/analysis/live_strategy.json')
ALLOWED = frozenset(REQUIRED + OPTIONAL)
LEASES = ('account-transfer', 'flow-handoff', 'flow-supervisor', 'dashboard', 'broker-runtime', 'health-order-test')
RECEIPT = 'data/health/git_account_receipt.json'
ID_PATTERN = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{16}')
MAX_ARCHIVE = 90 * 1024 * 1024
MAX_CONTENT = 1024 * 1024 * 1024


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    temporary.replace(path)


def latest_id(share):
    pointer = Path(share) / 'latest.json'
    if not pointer.exists():
        if any((Path(share) / 'snapshots').glob('*/manifest.json')):
            raise ValueError('LATEST_POINTER_MISSING')
        return None
    ident = read(pointer)['snapshot_id']
    if not isinstance(ident, str) or not ID_PATTERN.fullmatch(ident):
        raise ValueError('INVALID_SNAPSHOT_ID')
    return ident


def database_summary(path, relative):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLITE_INTEGRITY_FAILED')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        required = {'execution_attempts', 'execution_events', 'ai_positions'} if relative == DATABASES[0] else {'paper_account'}
        if not required.issubset(tables):
            raise ValueError('ACCOUNT_SCHEMA_MISSING')
        result = {'rows': {t: db.execute('SELECT count(*) FROM "' + t.replace('"', '""') + '"').fetchone()[0] for t in tables}}
        if relative == DATABASES[1]:
            row = db.execute('SELECT capital_limit_cents FROM paper_account WHERE account_id=1').fetchone()
            if row is None:
                raise ValueError('CAPITAL_ACCOUNT_MISSING')
            result['capital_limit_twd'] = row[0] / 100
        return result


def secret_values(root):
    """Check known passwords/keys, including the legacy multiline API key."""
    path = Path(root) / '.env'
    values, pending = [], False
    if not path.is_file():
        return values
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if pending and line.startswith('sk-') and '=' not in line:
            values.append(line)
            pending = False
            continue
        pending = False
        key, sep, value = line.partition('=')
        value = value.strip().strip('"').strip("'")
        if sep:
            pending = key == 'OPENAI_API_KEY' and not value
            if value and re.search(r'PASSWORD|API_KEY|SECRET|TOKEN', key.upper()):
                values.append(value)
    return values


def verify(share=SHARE_ROOT, snapshot_id=None):
    share = Path(share).resolve()
    ident = snapshot_id or latest_id(share)
    if not isinstance(ident, str) or not ID_PATTERN.fullmatch(ident):
        raise ValueError('SNAPSHOT_NOT_FOUND')
    folder = share / 'snapshots' / ident
    manifest = read(folder / 'manifest.json')
    if manifest.get('version') != 1 or manifest.get('snapshot_id') != ident:
        raise ValueError('INVALID_SNAPSHOT_MANIFEST')
    files = manifest['files']
    if not set(REQUIRED).issubset(files) or not set(files).issubset(ALLOWED):
        raise ValueError('SNAPSHOT_FILE_NOT_ALLOWED')
    archive = folder / 'account.zip'
    if archive.stat().st_size > MAX_ARCHIVE or sha(archive.read_bytes()) != manifest['archive_sha256']:
        raise ValueError('ARCHIVE_HASH_MISMATCH')
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.namelist()) != len(files) or set(bundle.namelist()) != set(files):
            raise ValueError('ARCHIVE_MEMBERS_MISMATCH')
        if sum(i.file_size for i in bundle.infolist()) > MAX_CONTENT:
            raise ValueError('ARCHIVE_TOO_LARGE')
        for name, item in files.items():
            raw = bundle.read(name)
            if len(raw) != item['bytes'] or sha(raw) != item['sha256']:
                raise ValueError('SNAPSHOT_FILE_HASH_MISMATCH')
    return manifest


def export_account(state_root=None, share=SHARE_ROOT):
    root = Path(state_root).resolve() if state_root else resolve_state_root(CODE_ROOT)
    share = Path(share).resolve()
    for name in REQUIRED:
        if not (root / name).is_file():
            raise ValueError('ACCOUNT_FILE_MISSING: ' + name)
    share.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for name in LEASES:
            stack.enter_context(StateLease(root, name))
        parent = latest_id(share)
        if parent:
            verify(share, parent)
            receipt = read(root / RECEIPT) if (root / RECEIPT).is_file() else {}
            if receipt.get('snapshot_id') != parent:
                raise ValueError('STALE_ACCOUNT_HANDOFF_PULL_AND_COMPARE')
        # Reserve both databases against writers, then back up committed state
        # with separate read connections. This also includes committed WAL data.
        for relative in DATABASES:
            lock = stack.enter_context(closing(sqlite3.connect((root / relative).as_uri() + '?mode=rw', uri=True, timeout=.1)))
            try:
                lock.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as exc:
                raise RuntimeError('ACCOUNT_DATABASE_BUSY') from exc
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=share) as temporary:
            stage = Path(temporary)
            payload = stage / 'payload'
            payload.mkdir()
            summary, files = {}, {}
            configs = {n: (root / n).read_bytes() for n in REQUIRED + OPTIONAL if n not in DATABASES and (root / n).is_file()}
            secrets = secret_values(root)
            for relative in DATABASES:
                target = payload / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect((root / relative).as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(target)) as dest:
                    source.backup(dest)
                summary[relative] = database_summary(target, relative)
            for name, raw in configs.items():
                json.loads(raw.decode('utf-8-sig'))
                target = payload / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            for name in ALLOWED:
                target = payload / name
                if not target.is_file():
                    continue
                raw = target.read_bytes()
                if any(s.encode(enc) in raw for s in secrets for enc in ('utf-8', 'utf-16-le')):
                    raise ValueError('CREDENTIAL_VALUE_IN_ACCOUNT_EXPORT: ' + name)
                if re.search(rb'sk-(?:proj-)?[A-Za-z0-9_-]{24,}', raw) or any(
                    marker in raw for marker in (b'BEGIN PRIVATE KEY', b'BEGIN RSA PRIVATE KEY', b'BEGIN ENCRYPTED PRIVATE KEY')):
                    raise ValueError('CREDENTIAL_PATTERN_IN_ACCOUNT_EXPORT: ' + name)
                files[name] = {'bytes': len(raw), 'sha256': sha(raw)}
            if any((root / n).read_bytes() != raw for n, raw in configs.items()):
                raise ValueError('CONFIG_CHANGED_DURING_EXPORT')
            archive = stage / 'account.zip'
            with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                for name in sorted(files):
                    bundle.write(payload / name, name)
            if archive.stat().st_size > MAX_ARCHIVE:
                raise ValueError('ARCHIVE_EXCEEDS_GIT_SIZE_LIMIT')
            now = datetime.now(timezone.utc)
            ident = now.strftime('%Y%m%dT%H%M%SZ-') + sha(archive.read_bytes())[:16]
            revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=CODE_ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
            manifest = dict(version=1, snapshot_id=ident, parent_snapshot_id=parent, created_at=now.isoformat(),
                            source_code_commit=revision, archive_sha256=sha(archive.read_bytes()), files=files,
                            summary=summary, credentials_included=False, broker_login_performed=False, orders_sent=0)
            write(stage / 'manifest.json', manifest)
            destination = share / 'snapshots' / ident
            destination.parent.mkdir(exist_ok=True)
            package = stage / 'published'
            package.mkdir()
            archive.replace(package / archive.name)
            (stage / 'manifest.json').replace(package / 'manifest.json')
            if destination.exists():
                raise ValueError('SNAPSHOT_ALREADY_EXISTS')
            package.rename(destination)
        verify(share, ident)
        write(share / 'latest.json', {'snapshot_id': ident})
        write(root / RECEIPT, {'snapshot_id': ident, 'action': 'EXPORTED', 'at': now.isoformat()})
        return manifest


def restore_account(target, share=SHARE_ROOT, snapshot_id=None):
    """Restore into a new directory only; existing ledgers are never replaced."""
    target = Path(target).expanduser().resolve()
    if target.exists():
        raise ValueError('RESTORE_TARGET_MUST_NOT_EXIST')
    manifest = verify(share, snapshot_id)
    archive = Path(share).resolve() / 'snapshots' / manifest['snapshot_id'] / 'account.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.account-restore-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'account'
        stage.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            for name, item in manifest['files'].items():
                raw = bundle.read(name)
                if sha(raw) != item['sha256']:
                    raise ValueError('RESTORE_CONTENT_CHANGED')
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
        for name in DATABASES:
            if database_summary(stage / name, name) != manifest['summary'][name]:
                raise ValueError('RESTORE_ACCOUNT_SUMMARY_MISMATCH')
        write(stage / RECEIPT, {'snapshot_id': manifest['snapshot_id'], 'action': 'RESTORED',
                              'at': datetime.now(timezone.utc).isoformat()})
        if target.exists():
            raise ValueError('RESTORE_TARGET_MUST_NOT_EXIST')
        stage.rename(target)
    return {'restored_to': str(target), 'snapshot_id': manifest['snapshot_id'],
            'credentials_restored': False, 'account_binding_changed': False, 'orders_sent': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--share-root', type=Path, default=SHARE_ROOT)
    commands = parser.add_subparsers(dest='action', required=True)
    export = commands.add_parser('export', help='停止交易後建立可提交 Git 的帳本快照')
    export.add_argument('--state-root', type=Path)
    inspect = commands.add_parser('inspect', help='驗證並顯示快照，不匯入帳戶')
    inspect.add_argument('--snapshot')
    restore = commands.add_parser('restore', help='還原至不存在的新目錄；不覆寫原帳本')
    restore.add_argument('--target', type=Path, required=True)
    restore.add_argument('--snapshot')
    args = parser.parse_args()
    if args.action == 'export':
        result = export_account(args.state_root, args.share_root)
    elif args.action == 'restore':
        result = restore_account(args.target, args.share_root, args.snapshot)
    else:
        result = verify(args.share_root, args.snapshot)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
