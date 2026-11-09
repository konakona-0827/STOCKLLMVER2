"""Local readiness checks; never log credentials or open a broker session."""
from contextlib import closing
from importlib.metadata import version, PackageNotFoundError
import json
import os
import sqlite3


def main():
    from config import ROOT, CODE_ROOT, load_env
    from .trading import settings
    load_env()
    report = dict(code_root=str(CODE_ROOT), state_root=str(ROOT), checks={}, errors=[])
    from codex_cli import executable, child_environment, _capture, metadata
    import asyncio
    import tempfile
    report.update(metadata())
    try:
        cli = executable()
        with tempfile.TemporaryDirectory(prefix='codex-login-check-') as tmp:
            code, out, err = asyncio.run(_capture([cli, 'login', 'status'], cwd=tmp, env=child_environment()))
        report['checks']['codex_chatgpt_login'] = code == 0 and 'Logged in using ChatGPT' in out+err
        if not report['checks']['codex_chatgpt_login']:
            report['errors'].append('CODEX_CHATGPT_LOGIN_REQUIRED')
    except (RuntimeError, OSError, TimeoutError) as exc:
        report['errors'].append(type(exc).__name__+': CODEX_CLI_UNAVAILABLE')
    for key in ('CAPITAL_USER_ID', 'CAPITAL_PASSWORD'):
        report['checks'][key+'_configured'] = bool(os.getenv(key))
        if not report['checks'][key+'_configured']:
            report['errors'].append(key+'_MISSING')
    for key, rel, table in [('execution', 'data/execution/live_execution.sqlite3', 'execution_attempts'),
                            ('analysis', 'data/analysis/market_history.sqlite3', 'scan_runs')]:
        path = ROOT / rel
        try:
            with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)) as db:
                report['checks'][key+'_rows'] = db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        except sqlite3.Error:
            report['errors'].append(key+'_LEDGER_UNAVAILABLE')
    try:
        report['checks']['agents_sdk_version'] = version('openai-agents')
    except PackageNotFoundError:
        report['errors'].append('AGENTS_SDK_MISSING')
    report['checks']['support_agents'] = settings(CODE_ROOT)
    if not report['checks']['support_agents']['enabled']:
        report['errors'].append('SUPPORT_AGENTS_DISABLED')
    report['ready'] = not report['errors']
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ready'] else 2
