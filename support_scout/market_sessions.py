"""Verify stock history against actual TWSE market sessions, including ad hoc closures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
import ssl
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import certifi

from config import now
from daily_history import _month_starts

BASE_URL = 'https://www.twse.com.tw/exchangeReport/FMTQIK'


def parse_sessions(payload, month):
    fields = payload.get('fields', [])
    required = ('日期', '成交金額', '發行量加權股價指數')
    if payload.get('stat') != 'OK' or not all(key in fields for key in required):
        raise ValueError('TWSE_MARKET_REPORT_UNAVAILABLE')
    result = []
    for row in payload['data']:
        year, m, day = map(int, row[fields.index('日期')].split('/'))
        session = date(year + 1911, m, day)
        if (session.year, session.month) != (month.year, month.month):
            raise ValueError('TWSE_MARKET_REPORT_MONTH_MISMATCH')
        for field in required[1:]:
            value = float(str(row[fields.index(field)]).replace(',', ''))
            if not math.isfinite(value) or value <= 0:
                raise ValueError('TWSE_MARKET_SESSION_WITHOUT_TRADING')
        result.append(session.isoformat())
    if result != sorted(set(result)):
        raise ValueError('TWSE_MARKET_DUPLICATE_OR_UNSORTED_SESSIONS')
    return result


def fetch_month(month, latest, cache_dir):
    path = cache_dir / f'market_{month:%Y%m}.json'
    url = BASE_URL + '?' + urlencode(dict(response='json', date=month.strftime('%Y%m%d')))
    record = None
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        raw = cached['raw'].encode('utf-8')
        if cached['url'] == url and sha256(raw).hexdigest() == cached['sha256']:
            sessions = parse_sessions(json.loads(raw), month)
            current = month == latest.replace(day=1)
            if not current or (cached['fetched_at'][:10] == now().date().isoformat()
                               and latest.isoformat() in sessions):
                record = cached
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass
    if record is None:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urlopen(request, timeout=8, context=ssl.create_default_context(cafile=certifi.where())) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('TWSE_MARKET_RESPONSE_TOO_LARGE')
        sessions = parse_sessions(json.loads(raw), month)
        record = dict(url=url, fetched_at=now().isoformat(), sha256=sha256(raw).hexdigest(),
                      raw=raw.decode('utf-8'))
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    return sessions, {key: record[key] for key in ('url', 'fetched_at', 'sha256')}


def load_sessions(latest, count, cache_dir):
    latest = date.fromisoformat(latest)
    months = list(_month_starts(latest, max(3, math.ceil(count / 16) + 1)))
    cache_dir = Path(cache_dir)

    def fetch(month):
        try:
            sessions, provenance = fetch_month(month, latest, cache_dir)
            return sessions, provenance, None
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            return [], None, dict(month=month.strftime('%Y%m'), error=type(exc).__name__)

    with ThreadPoolExecutor(max_workers=6) as pool:
        reports = list(pool.map(fetch, months))
    sessions = sorted({s for rows, _, _ in reports for s in rows if s <= latest.isoformat()})[-count:]
    errors = [error for _, _, error in reports if error]
    first_month = sessions[0][:7].replace('-', '') if sessions else '000000'
    relevant_errors = [error for error in errors if error['month'] >= first_month]
    verified = (len(sessions) == count and sessions[-1] == latest.isoformat() and not relevant_errors)
    return sessions, dict(source='TWSE_FMTQIK_ACTUAL_SESSIONS', verified=verified,
        expected_latest_date=latest.isoformat(), failed_months=errors,
        provenance=[p for _, p, _ in reports if p])
