"""Resolve one account state tree before any database or credential is opened."""
import json
import os
from pathlib import Path

BINDING_FILE = '.trading-state.json'
REQUIRED_STATE_FILES = (
    '.env', 'config/execution_live.json',
    'data/execution/live_execution.sqlite3',
    'data/analysis/market_history.sqlite3',
)


def validate_state_root(root):
    root = Path(root).expanduser().resolve()
    missing = [name for name in REQUIRED_STATE_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(f'正式資料目錄不完整：{root}；缺少：{", ".join(missing)}')
    return root


def resolve_state_root(code_root, environ=None):
    code_root = Path(code_root).resolve()
    environ = os.environ if environ is None else environ
    explicit = environ.get('TRADING_STATE_ROOT')
    if explicit:
        return validate_state_root(explicit)
    binding = code_root / BINDING_FILE
    if not binding.exists():
        if (code_root / '.git').is_file():
            raise ValueError('Git worktree 必須先綁定正式資料目錄，或明確設定 TRADING_STATE_ROOT')
        return code_root
    saved = json.loads(binding.read_text(encoding='utf-8-sig'))
    if (saved.get('version') != 1 or not isinstance(saved.get('state_root'), str)
            or not saved['state_root'].strip()):
        raise ValueError(f'正式資料綁定設定無效：{binding}')
    return validate_state_root(code_root / Path(saved['state_root']).expanduser())


def bind_state_root(code_root, state_root):
    code_root = Path(code_root).resolve()
    state_root = validate_state_root(state_root)
    try:
        selected = os.path.relpath(state_root, code_root)
    except ValueError:
        selected = str(state_root)
    destination = code_root / BINDING_FILE
    temporary = destination.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(dict(version=1, state_root=selected),
                                    ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(destination)
    return state_root
