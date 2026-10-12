"""Versioned Markdown strategy inputs shared by live and simulation screens."""
import hashlib
import json
from pathlib import Path

from config import ROOT

DEFAULT_PROMPT = ROOT / 'strategies' / 'prompt_positions.md'
LIVE_SELECTION = ROOT / 'data' / 'analysis' / 'live_strategy.json'


def read_strategy(path):
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != '.md' or not path.is_file():
        raise ValueError('請選擇存在的 .md 策略檔案')
    content = path.read_text(encoding='utf-8-sig')
    if not content.strip():
        raise ValueError('策略檔案不可為空白')
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return dict(name=path.stem, path=str(path), version=digest[:12], sha256=digest,
                prompt=content)


def live_selection():
    if LIVE_SELECTION.is_file():
        saved = json.loads(LIVE_SELECTION.read_text(encoding='utf-8'))
        return read_strategy(saved['path'])
    return read_strategy(DEFAULT_PROMPT)


def save_live_selection(path):
    strategy = read_strategy(path)
    LIVE_SELECTION.parent.mkdir(parents=True, exist_ok=True)
    temporary = LIVE_SELECTION.with_suffix('.tmp')
    temporary.write_text(json.dumps({'path': strategy['path']}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(LIVE_SELECTION)
    return strategy
