"""Read-only SKCOM/OpenAI configuration; secrets are loaded without printing."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re

ROOT = Path(__file__).resolve().parent
TAIPEI = timezone(timedelta(hours=8))
MAX_QUOTE_AGE_SECONDS = 120


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

