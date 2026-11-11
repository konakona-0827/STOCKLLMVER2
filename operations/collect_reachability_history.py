"""Collect official history without computing or selecting on outcome returns."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import ssl
import time
from urllib.request import Request, urlopen

import certifi

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/target_reachability_20261111'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    temporary.replace(path)


def select_universe():
    source = read(REPORT/'universe_source.json')
    protocol = read(REPORT/'protocol.json')
    payload = source['payload']
    assert payload['date'] == '20251231' and payload['stat'] == 'OK'
    table = next(t for t in payload['tables'] if '證券代號' in (t.get('fields') or []))
    fields = table['fields'];eligible = []
    excluded = set(protocol['validation']['exclude_symbols'])
    for row in table['data']:
        values = dict(zip(fields, row));symbol = values['證券代號']
        if not (len(symbol) == 4 and symbol.isdigit() and int(symbol) >= 1100) or symbol in excluded:
            continue
        try:
            number = lambda key: float(str(values[key]).replace(',', ''))
            price, volume, turnover = number('收盤價'), number('成交股數'), number('成交金額')
        except ValueError:
            continue
        if not 0 < price <= 3500 or volume < 100000 or turnover < 100000000:
            continue
        eligible.append(dict(symbol=symbol, name=values['證券名稱']))
    eligible.sort(key=lambda row: row['symbol'])
    indices = [round(i*(len(eligible)-1)/19) for i in range(20)]
    selected = [eligible[i] for i in indices]
    assert len({r['symbol'] for r in selected}) == 20
    result = dict(as_of='2025-12-31', rule=protocol['validation']['universe_rule'], eligible_count=len(eligible),
        selections=selected, source_sha256=hashlib.sha256((REPORT/'universe_source.json').read_bytes()).hexdigest())
    save(REPORT/'validation_universe.json', result)
    return result


def fetch(symbol, month):
    path = REPORT/'sources'/f'twse_{symbol}_{month}.json'
    if path.exists():
        record = read(path)
        if record['payload'].get('stat') == 'OK':
            return symbol, month, True
    url = f'https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={month}01&stockNo={symbol}'
    error = None
    for attempt in range(3):
        try:
            time.sleep(.2+attempt)
            request = Request(url, headers={'User-Agent':'Mozilla/5.0', 'Accept':'application/json'})
            with urlopen(request, timeout=25, context=ssl.create_default_context(cafile=certifi.where())) as response:
                payload = json.load(response)
            if payload.get('stat') != 'OK' or symbol not in str(payload.get('title', '')):
                raise ValueError('OFFICIAL_MONTH_UNAVAILABLE:'+str(payload.get('stat')))
            save(path, dict(source_url=url, fetched_at=datetime.now(timezone.utc).isoformat(), payload=payload))
            return symbol, month, True
        except Exception as exc:
            error = type(exc).__name__+':'+str(exc)
    save(path.with_suffix('.failure.json'), dict(source_url=url, error=error))
    return symbol, month, False


def collect():
    universe = read(REPORT/'validation_universe.json') if (REPORT/'validation_universe.json').exists() else select_universe()
    jobs = []
    for row in universe['selections']:
        for year, months in [(2025, range(1,13)), (2026, range(1,5))]:
            jobs.extend((row['symbol'], f'{year}{month:02d}') for month in months)
    # Development stocks are separate from every validation name.
    for symbol in ['1101','2603','2891','2377']:
        for year, months in [(2025, range(1,13)), (2026, range(1,11))]:
            jobs.extend((symbol, f'{year}{month:02d}') for month in months)
    completed = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(fetch, symbol, month) for symbol, month in jobs]
        for future in as_completed(futures):
            completed.append(future.result())
            if len(completed) % 30 == 0 or len(completed) == len(jobs):
                print(json.dumps(dict(completed=len(completed), total=len(jobs), failures=sum(not r[2] for r in completed))), flush=True)
    save(REPORT/'collection_manifest.json', dict(jobs=len(jobs), results=completed,
        universe_sha256=hashlib.sha256((REPORT/'validation_universe.json').read_bytes()).hexdigest(),
        validation_outcomes_inspected=False))


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    collect()
