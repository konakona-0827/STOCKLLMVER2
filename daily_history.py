"""Point-in-time TWSE daily OHLCV for research, separate from odd-lot quotes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import math
import re
import ssl

import certifi

from config import ROOT, now
from market_calendar import previous_trading_day, twse_closed_reason

SOURCE = 'TWSE_DAILY'
BASE_URL = 'https://www.twse.com.tw/exchangeReport/STOCK_DAY'
DEFAULT_CACHE = ROOT/'data'/'analysis'/'daily_history'
MAX_BARS = 22


def _month_starts(as_of: date, months=3):
    month = as_of.replace(day=1)
    for _ in range(months):
        yield month
        month = (month - timedelta(days=1)).replace(day=1)


def _number(value):
    try:
        result = float(str(value).replace(',', '').strip())
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _parse_month(payload, symbol, month, fetched_at, url):
    if payload.get('stat') != 'OK' or not isinstance(payload.get('data'), list):
        raise ValueError('TWSE_DAILY_RESPONSE_UNAVAILABLE')
    if symbol not in str(payload.get('title', '')):
        raise ValueError('TWSE_DAILY_SYMBOL_MISMATCH')
    expected = ('日期', '成交股數', '開盤價', '最高價', '最低價', '收盤價')
    fields = payload.get('fields') or []
    if not all(name in fields for name in expected):
        raise ValueError('TWSE_DAILY_FIELDS_CHANGED')
    indices = {name: fields.index(name) for name in expected}
    bars = []
    for row in payload['data']:
        try:
            year, m, day = (int(v) for v in str(row[indices['日期']]).split('/'))
            bar_date = date(year + 1911, m, day)
        except (ValueError, TypeError, IndexError):
            continue
        if (bar_date.year, bar_date.month) != (month.year, month.month):
            continue
        try:
            prices = {key: _number(row[indices[field]]) for key, field in (
                ('open', '開盤價'), ('high', '最高價'), ('low', '最低價'), ('close', '收盤價'))}
            volume = _number(row[indices['成交股數']])
        except (IndexError, TypeError, KeyError):
            continue
        if (volume is None or any(v is None for v in prices.values()) or
                not prices['low'] <= prices['open'] <= prices['high'] or
                not prices['low'] <= prices['close'] <= prices['high']):
            continue
        bars.append(dict(date=bar_date.isoformat(), **prices, volume=int(volume),
                         turnover_twd=(_number(row[fields.index('成交金額')])
                                       if '成交金額' in fields and len(row) > fields.index('成交金額') else None),
                         complete=True, source=SOURCE,
                         market='TWSE_LISTED_DAILY_NOT_ODD_LOT',
                         source_url=url, retrieved_at=fetched_at))
    dates = [bar['date'] for bar in bars]
    if len(dates) != len(set(dates)):
        raise ValueError('TWSE_DAILY_DUPLICATE_DATE')
    return sorted(bars, key=lambda bar: bar['date'])


def _fetch_month(symbol, month, cache_dir, as_of, *, require_turnover=False, minimum_latest=None):
    cache_file = cache_dir/f'{symbol}_{month:%Y%m}.json'
    cached = None
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding='utf-8'))
            if (not isinstance(cached, dict) or cached.get('symbol') != symbol or
                    cached.get('month') != month.strftime('%Y%m') or
                    not isinstance(cached.get('bars'), list) or
                    any(not isinstance(bar, dict) or 'date' not in bar
                        for bar in cached['bars'])):
                cached = None
        except (OSError, ValueError):
            cached = None
    is_current = (month.year, month.month) == (as_of.year, as_of.month)
    complete_amounts = cached and all(bar.get('turnover_twd') is not None for bar in cached['bars'])
    latest_present = (not minimum_latest or not is_current or
                      any(bar['date'] >= minimum_latest for bar in (cached or {}).get('bars', [])))
    if cached and latest_present and (not require_turnover or complete_amounts) and (not is_current or cached.get('fetched_at', '')[:10] == as_of.isoformat()):
        return cached.get('bars', []), None
    url = BASE_URL + '?' + urlencode(dict(response='json', date=month.strftime('%Y%m%d'), stockNo=symbol))
    try:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urlopen(request, timeout=8,
                     context=ssl.create_default_context(cafile=certifi.where())) as response:
            payload = json.load(response)
        fetched_at = now().isoformat()
        bars = _parse_month(payload, symbol, month, fetched_at, url)
        cache_dir.mkdir(parents=True, exist_ok=True)
        record = dict(symbol=symbol, month=month.strftime('%Y%m'), fetched_at=fetched_at, bars=bars)
        temporary = cache_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        temporary.replace(cache_file)
        return bars, None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return (cached.get('bars', []) if cached else []), type(exc).__name__


def daily_trend(bars):
    """Simple, explicit close-based facts; no prediction or corporate-action adjustment."""
    closes = [bar['close'] for bar in bars]
    volumes = [bar['volume'] for bar in bars]
    return dict(latest_date=bars[-1]['date'] if bars else None,
                return_5d=round(closes[-1]/closes[-6]-1, 6) if len(bars) >= 6 else None,
                return_20d=round(closes[-1]/closes[-21]-1, 6) if len(bars) >= 21 else None,
                ma5=round(sum(closes[-5:])/5, 4) if len(bars) >= 5 else None,
                ma20=round(sum(closes[-20:])/20, 4) if len(bars) >= 20 else None,
                volume_5d_vs_prior_5d=(round(sum(volumes[-5:])/sum(volumes[-10:-5]), 6)
                                        if len(bars) >= 10 and sum(volumes[-10:-5]) > 0 else None),
                price_basis='UNADJUSTED_TWSE_DAILY_CLOSE',
                note='歷史日線為證交所上市證券日成交資料；未經除權息調整，非盤中零股可成交價。')


def load_many(symbols, as_of=None, cache_dir=DEFAULT_CACHE, *, max_bars=MAX_BARS,
              require_turnover=False, include_completed_today=False):
    """Return completed sessions (22 by default); network failure is explicit."""
    as_of = as_of or now().date()
    if type(max_bars) is not int or not 1 <= max_bars <= 250:
        raise ValueError('DAILY_HISTORY_MAX_BARS_MUST_BE_1_TO_250')
    cache_dir = Path(cache_dir)
    completed_today = (include_completed_today and as_of == now().date()
                       and now().hour >= 16 and not twse_closed_reason(as_of))
    expected_latest = (as_of if completed_today else previous_trading_day(as_of)).isoformat()
    symbols = list(dict.fromkeys(s for s in symbols if re.fullmatch(r'[0-9A-Z]{4,6}', s)))
    months = max(3, math.ceil(max_bars / 16) + 1) if max_bars > MAX_BARS else 3
    jobs = [(symbol, month) for symbol in symbols for month in _month_starts(as_of, months)]
    grouped = {symbol: {'bars': [], 'failed_months': []} for symbol in symbols}
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetch_options = {'require_turnover': True} if require_turnover else {}
        if completed_today:
            fetch_options['minimum_latest'] = expected_latest
        futures = {pool.submit(_fetch_month, symbol, month, cache_dir, as_of, **fetch_options): (symbol, month)
                   for symbol, month in jobs}
        for future in as_completed(futures):
            symbol, month = futures[future]
            try:
                bars, error = future.result()
            except Exception as exc:
                bars, error = [], type(exc).__name__
            grouped[symbol]['bars'].extend(bars)
            if error:
                grouped[symbol]['failed_months'].append(month.strftime('%Y%m'))
    result = {}
    for symbol, item in grouped.items():
        by_date = {bar['date']: bar for bar in item['bars']
                   if bar['date'] <= expected_latest}
        source_bars = [by_date[key] for key in sorted(by_date)[-max_bars:]]
        # Keep the LLM payload small while retaining monthly source provenance.
        bars = [{key: bar[key] for key in ('date', 'open', 'high', 'low', 'close',
                                           'volume', 'complete', 'source')}
                for bar in source_bars]
        for bar, source_bar in zip(bars, source_bars):
            if source_bar.get('turnover_twd') is not None:
                bar['turnover_twd'] = source_bar['turnover_twd']
        source_urls = list(dict.fromkeys(bar['source_url'] for bar in source_bars
                                         if bar.get('source_url')))
        count = len(bars)
        result[symbol] = dict(bars=bars, trend=daily_trend(bars),
                              status=dict(source=SOURCE, as_of=as_of.isoformat(),
                                          market='TWSE_LISTED_DAILY_NOT_ODD_LOT',
                                          prior_complete_sessions=count,
                                          latest_date=bars[-1]['date'] if bars else None,
                                          source_urls=source_urls,
                                          latest_retrieved_at=(source_bars[-1].get('retrieved_at')
                                                               if source_bars else None),
                                          expected_latest_date=expected_latest,
                                          coverage=('STALE' if bars and bars[-1]['date'] < expected_latest else
                                                    'SUFFICIENT_FOR_20D_COMPARISON' if count >= 21 else
                                                    'PARTIAL' if count else 'UNAVAILABLE'),
                                          failed_months=sorted(item['failed_months'])))
    return result


def enrich_candidates(candidates, quote_lookup, as_of=None, cache_dir=DEFAULT_CACHE):
    """Attach daily evidence without changing snapshot score or broker prices."""
    from strategy_evidence import assess
    from pretrade_policy import price_levels

    history = load_many((c['symbol'] for c in candidates), as_of, cache_dir)
    for candidate in candidates:
        item = history[candidate['symbol']]
        candidate['daily_bars'] = item['bars']
        candidate['daily_trend'] = item['trend']
        candidate['daily_history_status'] = item['status']
        candidate['price_levels'] = price_levels(candidate)
        quote = dict(quote_lookup[candidate['symbol']], daily_bars=item['bars'])
        evidence = assess(quote, candidate.get('market_history'))
        candidate['strategy_evidence'] = evidence
        candidate['eligible_strategies'] = evidence['eligible_strategies']
    return candidates
