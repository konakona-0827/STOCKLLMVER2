"""Daily price-path scenarios. These labels are neither fills nor portfolio returns.

Uses only the standard library; importing this module cannot load credentials,
connect a broker, call an LLM, or open the live account databases.
"""
from collections import defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import io
import itertools
import json
from pathlib import Path
import uuid


SCHEMA_VERSION = 1
DEFAULT_PROTOCOL = Path(__file__).with_name('protocol.json')
FIELDS = (
    'profile', 'symbol', 'signal_date', 'entry_date', 'horizon_trading_days',
    'take_profit_rate', 'stop_loss_rate', 'status', 'outcome', 'reason',
    'exit_date', 'holding_trading_days', 'window_complete', 'ambiguous_bar', 'quantity',
    'entry_scenario_price', 'exit_scenario_price', 'buy_cost_twd',
    'sell_proceeds_twd', 'commission_twd', 'sell_tax_twd', 'net_pnl_twd',
    'net_return',
)


def decimal(value, *, minimum=Decimal('0'), maximum=None):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'無效數值：{value!r}') from exc
    if (not number.is_finite() or number < minimum or
            (maximum is not None and number > maximum)):
        raise ValueError(f'數值超出範圍：{value!r}')
    return number


def positive_int(value):
    if type(value) is not int or value <= 0:
        raise ValueError(f'須為正整數：{value!r}')
    return value


def iso_date(value):
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError(f'日期須為 YYYY-MM-DD：{value!r}')
    return result


def validate_protocol(protocol):
    expected = dict(schema_version=1, mode='OFFLINE_LABEL_RESEARCH',
                    market='TW_COMMON_STOCK', entry_rule='NEXT_SESSION_OPEN',
                    earliest_exit_sessions_after_entry=1, ambiguous_bar_rule='STOP_FIRST')
    if any(protocol.get(key) != value for key, value in expected.items()):
        raise ValueError('不支援的實驗版本、商品或進出場規則')
    if not isinstance(protocol.get('experiment_id'), str) or not protocol['experiment_id'].strip():
        raise ValueError('缺少 experiment_id')
    decimal(protocol['notional_per_opportunity_twd'], minimum=Decimal('0.01'))
    if positive_int(protocol['max_oddlot_quantity']) > 999:
        raise ValueError('盤中零股股數不得超過 999')
    costs = protocol['costs']
    if not costs.get('version') or costs.get('fee_rounding') != 'CEILING_TWD':
        raise ValueError('成本須有版本且使用 CEILING_TWD 情境')
    for key in ('commission_rate', 'commission_discount', 'sell_tax_rate'):
        decimal(costs[key], maximum=Decimal('1'))
    decimal(costs['minimum_commission_twd'])
    decimal(costs['slippage_bps_per_side'], maximum=Decimal('9999'))
    profiles = protocol['profiles']
    if not isinstance(profiles, list) or not profiles:
        raise ValueError('至少需要一條研究線')
    ids = [p['id'] for p in profiles]
    if any(sid not in ('A', 'B', 'C') for sid in ids) or len(set(ids)) != len(ids):
        raise ValueError('研究線 ID 必須為不重複的 A、B、C')
    for profile in profiles:
        horizons = profile['forecast_horizons_trading_days']
        if not isinstance(horizons, list) or not horizons:
            raise ValueError('預測期限不得為空')
        for horizon in horizons:
            positive_int(horizon)
        if len(set(horizons)) != len(horizons):
            raise ValueError('預測期限重複')
        positive_int(profile['planned_max_holding_trading_days'])
        barriers = profile['barriers']
        if not isinstance(barriers, list) or not barriers:
            raise ValueError('至少需要一組障礙條件；價格基準使用 null/null')
        seen = set()
        for barrier in barriers:
            tp, sl = barrier['take_profit_rate'], barrier['stop_loss_rate']
            if (tp is None) != (sl is None):
                raise ValueError('停利與停損須同時設定或同時為 null')
            if tp is not None:
                decimal(tp, minimum=Decimal('0.000001'))
                decimal(sl, minimum=Decimal('0.000001'), maximum=Decimal('0.999999'))
            key = (None, None) if tp is None else (Decimal(str(tp)), Decimal(str(sl)))
            if key in seen:
                raise ValueError('障礙條件重複')
            seen.add(key)
    return protocol


def read_sessions(path):
    with csv_source(path) as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ['date']:
            raise ValueError('交易日曆 CSV 欄位須為 date')
        sessions = [iso_date(row['date']) for row in reader]
    if not sessions or sessions != sorted(set(sessions)):
        raise ValueError('交易日曆須非空、依日期遞增且不重複')
    return sessions


def csv_source(path_or_stream):
    return (path_or_stream if hasattr(path_or_stream, 'read') else
            Path(path_or_stream).open(encoding='utf-8-sig', newline=''))


def read_bars(path, sessions):
    """Reject missing sessions inside a symbol's supplied history; never compress gaps."""
    index = {day: i for i, day in enumerate(sessions)}
    grouped = defaultdict(list)
    seen = set()
    required = {'symbol', 'date', 'open', 'high', 'low', 'close', 'volume'}
    with csv_source(path) as source:
        reader = csv.DictReader(source)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('日線 CSV 缺少 symbol,date,open,high,low,close,volume')
        for row in reader:
            symbol = row['symbol'].strip()
            day = iso_date(row['date'])
            if not symbol or day not in index or (symbol, day) in seen:
                raise ValueError(f'空白股票代碼、非日曆交易日或重複日線：{symbol} {day}')
            seen.add((symbol, day))
            bar = {key: decimal(row[key], minimum=Decimal('0.000001'))
                   for key in ('open', 'high', 'low', 'close')}
            if not (bar['low'] <= min(bar['open'], bar['close']) <=
                    max(bar['open'], bar['close']) <= bar['high']):
                raise ValueError(f'OHLC 關係錯誤：{symbol} {day}')
            volume = int(row['volume'])
            if volume < 0:
                raise ValueError('成交量不得為負數')
            bar.update(symbol=symbol, date=day, volume=volume, session_index=index[day])
            grouped[symbol].append(bar)
    if not grouped:
        raise ValueError('日線不得為空')
    for symbol, bars in grouped.items():
        bars.sort(key=lambda bar: bar['date'])
        if any(b['session_index'] != a['session_index'] + 1
               for a, b in zip(bars, bars[1:])):
            raise ValueError(f'{symbol} 日線缺少交易日；不得將資料缺口壓縮為較短持有期')
    return dict(sorted(grouped.items()))


def commission(gross, costs):
    fee = gross * decimal(costs['commission_rate']) * decimal(costs['commission_discount'])
    return max(decimal(costs['minimum_commission_twd']), fee).to_integral_value(rounding=ROUND_CEILING)


def label_path(bars, signal_index, horizon, barrier, protocol):
    """Signal after close, next-session open entry, no entry-day odd-lot exit.

    Horizon counts sessions AFTER entry. A gap through a stop exits at the lower
    open; a gap above a target receives only the target, with adverse slippage.
    """
    signal = bars[signal_index]
    tp, sl = barrier['take_profit_rate'], barrier['stop_loss_rate']
    result = dict(symbol=signal['symbol'], signal_date=signal['date'].isoformat(),
                  horizon_trading_days=horizon, take_profit_rate=tp, stop_loss_rate=sl,
                  status='CENSORED', outcome=None, reason='INSUFFICIENT_FUTURE_SESSIONS',
                  window_complete=signal_index + 1 + horizon < len(bars),
                  ambiguous_bar=False)
    if signal_index + 1 >= len(bars):
        return result
    entry_bar = bars[signal_index + 1]
    result['entry_date'] = entry_bar['date'].isoformat()
    if entry_bar['volume'] == 0:
        result.update(status='NOT_ENTERED', reason='NO_ENTRY_VOLUME')
        return result
    costs = protocol['costs']
    slip = decimal(costs['slippage_bps_per_side']) / 10000
    entry = entry_bar['open'] * (1 + slip)
    budget = decimal(protocol['notional_per_opportunity_twd'])
    qty = min(int(budget // entry), protocol['max_oddlot_quantity'])
    while qty > 0 and entry * qty + commission(entry * qty, costs) > budget:
        qty -= 1
    if not qty:
        result.update(status='NOT_ENTERED', reason='INSUFFICIENT_NOTIONAL')
        return result
    result.update(quantity=qty, entry_scenario_price=str(entry))
    target = entry * (1 + decimal(tp)) if tp is not None else None
    stop = entry * (1 - decimal(sl)) if sl is not None else None
    # Entry-day highs/lows cannot cause an exit in this odd-lot research model.
    future = bars[signal_index + 2:signal_index + 2 + horizon]
    for holding_days, bar in enumerate(future, 1):
        if bar['volume'] == 0:
            result['reason'] = 'ZERO_VOLUME_PATH_REQUIRES_REVIEW'
            return result
        hit_stop = stop is not None and bar['low'] <= stop
        hit_target = target is not None and bar['high'] >= target
        ambiguous = False
        if stop is not None and bar['open'] <= stop:
            raw_exit, outcome = bar['open'], 'SL'
        elif target is not None and bar['open'] >= target:
            raw_exit, outcome = target, 'TP'
        elif hit_stop:
            raw_exit, outcome = stop, 'SL'
            ambiguous = bool(hit_target)
        elif hit_target:
            raw_exit, outcome = target, 'TP'
        elif holding_days == horizon:
            raw_exit, outcome = bar['close'], 'TIMEOUT'
        else:
            continue
        exit_price = raw_exit * (1 - slip)
        gross_buy, gross_sell = entry * qty, exit_price * qty
        fees = commission(gross_buy, costs) + commission(gross_sell, costs)
        tax = (gross_sell * decimal(costs['sell_tax_rate'])).to_integral_value(rounding=ROUND_CEILING)
        buy_cost = gross_buy + commission(gross_buy, costs)
        proceeds = gross_sell - commission(gross_sell, costs) - tax
        pnl = proceeds - buy_cost
        result.update(status='COMPLETE', reason='', outcome=outcome,
                      exit_date=bar['date'].isoformat(), holding_trading_days=holding_days,
                      ambiguous_bar=ambiguous,
                      exit_scenario_price=str(exit_price), buy_cost_twd=str(buy_cost),
                      sell_proceeds_twd=str(proceeds), commission_twd=str(fees),
                      sell_tax_twd=str(tax), net_pnl_twd=str(pnl), net_return=str(pnl / buy_cost))
        return result
    return result


def iter_labels(grouped, protocol):
    for profile in protocol['profiles']:
        for bars in grouped.values():
            for index in range(len(bars)):
                for horizon, barrier in itertools.product(profile['forecast_horizons_trading_days'],
                                                          profile['barriers']):
                    yield dict(profile=profile['id'], **label_path(bars, index, horizon, barrier, protocol))


class DescriptiveSummary:
    """Aggregate scenarios without presenting overlapping opportunities as a portfolio."""
    def __init__(self):
        self.groups = {}

    def add(self, row):
        key = tuple(row[name] for name in ('profile', 'horizon_trading_days',
                                          'take_profit_rate', 'stop_loss_rate'))
        stats = self.groups.setdefault(key, dict(complete=0, censored=0, not_entered=0,
                                                evaluated=0, partial_window_exits=0,
                                                tp=0, sl=0, timeout=0, ambiguous=0,
                                                pnl=Decimal(0), returns=Decimal(0),
                                                days=0, capital_days=Decimal(0)))
        stats[row['status'].lower()] += 1
        if row['status'] == 'COMPLETE':
            # Otherwise tail TP/SL outcomes would be included while unresolved
            # peers are censored, biasing horizon-specific descriptive rates.
            if not row['window_complete']:
                stats['partial_window_exits'] += 1
                return
            stats['evaluated'] += 1
            stats[row['outcome'].lower()] += 1
            stats['ambiguous'] += int(row['ambiguous_bar'])
            stats['pnl'] += Decimal(row['net_pnl_twd'])
            stats['returns'] += Decimal(row['net_return'])
            stats['days'] += row['holding_trading_days']
            stats['capital_days'] += Decimal(row['buy_cost_twd']) * row['holding_trading_days']

    def rows(self):
        result = []
        for (profile, horizon, tp, sl), stats in self.groups.items():
            count = stats['evaluated']
            mean = lambda value: float(value / count) if count else None
            result.append(dict(
                profile=profile, horizon_trading_days=horizon, take_profit_rate=tp, stop_loss_rate=sl,
                complete=stats['complete'], evaluated=count,
                partial_window_exits_excluded=stats['partial_window_exits'],
                censored=stats['censored'], not_entered=stats['not_entered'],
                tp_count=stats['tp'], sl_count=stats['sl'], timeout_count=stats['timeout'],
                ambiguous_stop_first_count=stats['ambiguous'],
                empirical_tp_fraction=stats['tp'] / count if count and tp is not None else None,
                mean_scenario_net_pnl_twd=mean(stats['pnl']), mean_scenario_net_return=mean(stats['returns']),
                mean_holding_trading_days=mean(stats['days']),
                scenario_pnl_per_10000_capital_day=(
                    float(stats['pnl'] / stats['capital_days'] * 10000) if stats['capital_days'] else None)))
        return result


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_research(bars_path, sessions_path, *, protocol_path=DEFAULT_PROTOCOL,
                 output_root=Path('research-runs'), dataset_note):
    """Save immutable inputs and an individual run manifest; no production imports."""
    if not dataset_note.strip():
        raise ValueError('須描述資料來源、股票歷史範圍、除權息及排除規則')
    # Read inputs exactly once: these very bytes are hashed, parsed and archived.
    inputs = {name: Path(path).read_bytes() for name, path in
              (('bars.csv', bars_path), ('sessions.csv', sessions_path), ('protocol.json', protocol_path))}
    protocol = validate_protocol(json.loads(inputs['protocol.json'].decode('utf-8-sig')))
    sessions = read_sessions(io.StringIO(inputs['sessions.csv'].decode('utf-8-sig')))
    grouped = read_bars(io.StringIO(inputs['bars.csv'].decode('utf-8-sig')), sessions)
    stamp = datetime.now(timezone(timedelta(hours=9)))
    run_id = f'{stamp:%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:8]}'
    destination = Path(output_root).resolve() / run_id
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in inputs.items():
        (destination / name).write_bytes(data)
    summary = DescriptiveSummary()
    count = 0
    with (destination / 'labels.csv').open('w', encoding='utf-8-sig', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=FIELDS)
        writer.writeheader()
        for row in iter_labels(grouped, protocol):
            writer.writerow(row)
            summary.add(row)
            count += 1
    source_root = Path(__file__).parent
    manifest = dict(
        schema_version=SCHEMA_VERSION, run_id=run_id, created_at=stamp.isoformat(),
        experiment_id=protocol['experiment_id'], mode=protocol['mode'],
        broker_order_sent=False, model='NONE_PRICE_LABELS_ONLY', dataset_note=dataset_note,
        input_sha256={name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()},
        implementation_sha256={name: sha256(source_root / name) for name in ('engine.py', '__main__.py')},
        first_session=sessions[0].isoformat(), last_session=sessions[-1].isoformat(),
        symbols=list(grouped), bar_count=sum(map(len, grouped.values())), label_count=count,
        assumptions=dict(
            horizon='Exchange sessions after entry; entry day excluded',
            opportunities='Every supplied close; overlapping and unfiltered',
            prices='Daily OHLC scenarios, not executable odd-lot quotes',
            zero_volume='Not entered or censored; requires review',
            corporate_actions='Must be handled by data preparation; not inferred',
            settlement='Not modeled; no portfolio cash reuse or allocation',
            probabilities='Descriptive frequencies only; no calibrated predictions',
            quality='C is a price baseline; fundamentals model pending'))
    for name, value in (('summary.json', summary.rows()), ('run.json', manifest)):
        (destination / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return destination
