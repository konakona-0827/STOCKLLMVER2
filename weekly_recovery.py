"""Short-horizon TPEx recovery observations from completed exchange sessions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from urllib.parse import urlencode

from config import redact
from decimal import Decimal
from entry_structure import completed_recovery
from main import save_json
from market_calendar import previous_trading_day
from market_discovery import _day, _number, affordable_rows, fetch_json


DAILY_URL = 'https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes'
COMPANIES_URL = 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O'
FIELDS = {'symbol': '代號', 'name': '名稱', 'open': '開盤', 'high': '最高',
          'low': '最低', 'close': '收盤', 'change': '漲跌',
          'volume': '成交股數', 'turnover': '成交金額(元)'}


def _companies(payload, expected, as_of):
    companies = {}
    for item in payload:
        symbol = item['SecuritiesCompanyCode']
        if not re.fullmatch(r'[1-9]\d{3}', symbol):
            continue
        if not expected <= _day(item['Date']) <= as_of:
            continue
        listed = item.get('DateOfListing')
        if listed and _day(listed) > expected:
            continue
        if symbol in companies:
            raise ValueError('DUPLICATE_TPEX_COMPANY')
        companies[symbol] = item['SecuritiesIndustryCode']
    if not companies:
        raise ValueError('TPEX_COMPANY_LIST_NOT_CURRENT')
    return companies


def _bars(payload, expected, companies):
    if payload['stat'].lower() != 'ok' or _day(payload['date']) != expected:
        raise ValueError('TPEX_DAILY_WRONG_SESSION')
    table = next((item for item in payload['tables'] if item['title'] == '上櫃股票行情'), None)
    if table is None or _day(table['date']) != expected:
        raise ValueError('TPEX_DAILY_TABLE_UNAVAILABLE')
    indices = {key: table['fields'].index(field) for key, field in FIELDS.items()}
    rows, seen = {}, set()
    for item in table['data']:
        symbol = item[indices['symbol']]
        if symbol not in companies:
            continue
        if symbol in seen:
            raise ValueError('DUPLICATE_TPEX_DAILY_SYMBOL')
        seen.add(symbol)
        try:
            row = {key: _number(item[indices[key]], positive=key != 'change')
                   for key in ('open', 'high', 'low', 'close', 'change', 'volume', 'turnover')}
        except (ValueError, TypeError):
            continue
        if not row['low'] <= min(row['open'], row['close']) <= max(
                row['open'], row['close']) <= row['high']:
            continue
        if row['close'] <= row['change']:
            continue
        rows[symbol] = dict(row, symbol=symbol, name=item[indices['name']],
                            industry=companies[symbol], date=expected.isoformat())
    if not rows:
        raise ValueError('TPEX_NO_VALID_COMMON_STOCK_ROWS')
    return rows


def _recoveries(latest, prior):
    candidates = []
    for symbol, row in latest.items():
        previous = prior.get(symbol)
        if previous is None or row['change'] <= 0:
            continue
        plan = completed_recovery(previous, row, Decimal(str(previous['change'])))
        if plan is None:
            continue
        range_position = (row['close'] - row['low']) / (row['high'] - row['low'])
        candidates.append(dict(row, venue='TPEx',
            return_1d=round(row['change'] / (row['close'] - row['change']), 6),
            range_position=round(range_position, 6),
            prior_session=previous['date'], reclaimed_high=previous['high'],
            watch_reference=row['close'], watch_session_high=row['high'],
            research_plan=dict(plan, state='WAIT_FOR_LIVE_RETEST'),
            purpose='RESEARCH_ONLY', required_next_checks=[
                'LIVE_TPEX_ODD_LOT', 'TRADING_RESTRICTIONS', 'FULL_DAILY_STRUCTURE',
                'OBSERVED_TARGET_AND_STOP', 'NET_COST_AND_RISK']))
    return candidates


def discover_weekly_recovery(root, account, opportunity_policy, scout_policy, at, *,
                             excluded=(), fetcher=None):
    """Rank completed-session recoveries without supplying tradeable symbols."""
    session = previous_trading_day(at.date())
    prior = previous_trading_day(session)
    urls = [DAILY_URL + '?' + urlencode(dict(date=day.strftime('%Y/%m/%d'), response='json'))
            for day in (session, prior)] + [COMPANIES_URL]
    result = dict(status='UNAVAILABLE', purpose='RESEARCH_ONLY', venue='TPEx',
                  source_session=session.isoformat(), prior_session=prior.isoformat(),
                  source_urls=urls, candidates=[])
    cache = Path(root) / 'data' / 'analysis' / 'market_discovery' / f'tpex_recovery_{session}.json'
    try:
        rows = None
        if cache.is_file():
            saved = json.loads(cache.read_text('utf-8'))
            if (saved.get('schema') == 2 and saved.get('source_urls') == urls
                    and isinstance(saved.get('candidates'), list)):
                rows = saved['candidates']
        if rows is None:
            with ThreadPoolExecutor(max_workers=3) as pool:
                latest_payload, prior_payload, registry = pool.map(fetcher or fetch_json, urls)
            companies = _companies(registry, session, at.date())
            rows = _recoveries(_bars(latest_payload, session, companies),
                               _bars(prior_payload, prior, companies))
            save_json(cache, dict(schema=2, source_urls=urls, candidates=rows))
        eligible = affordable_rows(rows, account, opportunity_policy, scout_policy, excluded)
        eligible.sort(key=lambda row: (-row['turnover'], -row['return_1d'], row['symbol']))
        return dict(result, status='COMPLETE', eligible_count=len(eligible),
                    candidates=eligible[:scout_policy['max_screen_candidates']])
    except Exception as exc:
        # A failed optional feed must never interrupt the existing scout.
        return dict(result, error=redact(f'{type(exc).__name__}: {exc}'))
