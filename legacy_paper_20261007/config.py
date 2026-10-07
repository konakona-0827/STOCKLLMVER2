"""Paper trading only. Broker credentials are never used by this application."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re

ROOT = Path(__file__).resolve().parent
TAIPEI = timezone(timedelta(hours=8))
MAX_STRATEGY_CAPITAL_TWD = 10000
DECISION_INTERVAL_MINUTES = 30
QUOTE_INTERVAL_SECONDS = 30
MAX_QUOTE_AGE_SECONDS = 120
DRY_RUN = True  # Informational: there is no live execution implementation.


def load_env(path=ROOT / '.env'):
    # Accept the existing OPENAI_API_KEY= followed by the key on the next line.
    pending = False
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if pending and line.startswith('sk-') and '=' not in line:
            if not os.environ.get('OPENAI_API_KEY'):
                os.environ['OPENAI_API_KEY'] = line
            pending = False
            continue
        pending = False
        key, sep, value = line.partition('=')
        if sep and re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            value = value.strip().strip('\"').strip("'")
            pending = key.strip() == 'OPENAI_API_KEY' and not value
            if value:
                os.environ.setdefault(key.strip(), value)


def now():
    return datetime.now(TAIPEI)


def redact(value):
    text = str(value)
    for key in ('OPENAI_API_KEY', 'CAPITAL_USER_ID', 'CAPITAL_PASSWORD'):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', text)


def symbols():
    result = list(dict.fromkeys(os.getenv('PAPER_SYMBOLS', '2330,2317,2882').split(',')))
    result = [s.strip() for s in result]
    if not result or any(not re.fullmatch(r'[1-9][0-9]{3}', s) for s in result):
        raise ValueError('PAPER_SYMBOLS must contain four-digit TWSE ordinary stock symbols')
    return result


def current_slot(at):
    at = at.astimezone(TAIPEI)
    minute = at.hour * 60 + at.minute
    if at.weekday() >= 5 or not 540 <= minute < 810:
        return None
    return at.replace(minute=(at.minute // 30) * 30, second=0, microsecond=0).isoformat(timespec='minutes')


def next_slot(at):
    candidate = at.astimezone(TAIPEI).replace(second=0, microsecond=0)
    candidate += timedelta(minutes=30 - candidate.minute % 30)
    while candidate.weekday() >= 5 or not 540 <= candidate.hour * 60 + candidate.minute <= 780:
        candidate += timedelta(minutes=30)
    return candidate.isoformat(timespec='minutes')
