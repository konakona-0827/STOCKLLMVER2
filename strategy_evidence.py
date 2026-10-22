"""Describe which trading setups can be evaluated from observed market data.

Eligibility means that evidence exists to *evaluate* a setup. It is not a
trading signal, price forecast, or permission to send an order.
"""
from __future__ import annotations

from datetime import datetime
import math
from market_calendar import previous_trading_day


STRATEGY_IDS = (
    'intraday_recovery', 'short_term_reversal', 'breakout', 'multi_day_trend',
)


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _session(value):
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def assess(quote, history=()):
    """Return auditable per-setup evidence without a global history minimum."""
    history = history or ()
    today = _session(quote.get('quote_timestamp') or quote.get('exchange_time'))
    last, op, high, low = (quote.get(key) for key in ('last_price', 'open', 'high', 'low'))
    bid, ask, volume = (quote.get(key) for key in ('bid', 'ask', 'volume'))
    live = quote.get('quote_status') == 'LIVE' and not quote.get('is_trial')
    book = _positive(bid) and _positive(ask) and bid <= ask
    day_range = (all(_positive(v) for v in (last, op, high, low))
                 and low < high and low <= last <= high and low <= op <= high)
    observed = []
    for row in history:
        stamp = row.get('exchange_time')
        price = row.get('price', row.get('last_price'))
        if _session(stamp) and _positive(price) and stamp != quote.get('quote_timestamp'):
            observed.append((stamp, price, row))
    same_day = sorted((entry for entry in observed if _session(entry[0]) == today),
                      key=lambda entry: entry[0])
    previous_session = previous_trading_day(datetime.fromisoformat(today).date()).isoformat() if today else None
    prior_days = sorted({_session(stamp) for stamp, _, _ in observed
                         if _session(stamp) and today and _session(stamp) < today})
    has_previous_session = previous_session in prior_days
    latest_prior = next((price for stamp, price, _ in reversed(sorted(observed, key=lambda entry: entry[0]))
                         if _session(stamp) == previous_session), None)
    common = live and book and _positive(volume) and bool(today)
    specs = {}

    def add(strategy_id, eligible, missing, facts):
        specs[strategy_id] = dict(eligible=bool(eligible), missing=missing,
                                  observed=facts)

    base_missing = ([] if live else ['LIVE_NON_TRIAL_QUOTE'])
    base_missing += ([] if book else ['VALID_BID_ASK'])
    base_missing += ([] if _positive(volume) else ['POSITIVE_VOLUME'])
    base_missing += ([] if today else ['QUOTE_SESSION_DATE'])
    range_missing = base_missing + ([] if day_range else ['VALID_INTRADAY_OHLC_RANGE'])
    add('intraday_recovery', common and day_range, range_missing,
        dict(range_position=(last-low)/(high-low) if day_range else None,
             price_vs_open=(last/op-1) if day_range else None,
             interpretation='當日區間證據；尚未證明已止跌'))
    add('short_term_reversal', common and day_range and has_previous_session,
        range_missing + ([] if has_previous_session else ['PREVIOUS_TRADING_SESSION_PRICE_SAMPLE']),
        dict(prior_session_count=len(prior_days), latest_prior_sample_price=latest_prior,
             interpretation='前期價格僅是盤中抽樣，不能聲稱前日收盤或完整日 K'))
    prior_stamps = {stamp for stamp, _, _ in same_day}
    add('breakout', common and day_range and len(prior_stamps) >= 2,
        range_missing + ([] if len(prior_stamps) >= 2 else ['TWO_PRIOR_INTRADAY_SAMPLES']),
        dict(prior_intraday_samples=len(prior_stamps),
             prior_sample_high=max((price for _, price, _ in same_day), default=None),
             interpretation='只能比較盤中抽樣高點，不能稱為突破多日前高'))

    # The present store contains intraday price/volume samples, not complete
    # certified OHLCV bars. A future daily-bar source can populate this field.
    bars = quote.get('daily_bars') or []
    valid_bars = []
    for bar in bars:
        date = _session(bar.get('date')) if isinstance(bar, dict) else None
        if (date and today and date < today and
                bar.get('complete') is True and bar.get('source') in {'TWSE_DAILY', 'BROKER_DAILY'} and
                all(_positive(bar.get(k)) for k in ('open', 'high', 'low', 'close', 'volume')) and
                bar['low'] <= bar['open'] <= bar['high'] and
                bar['low'] <= bar['close'] <= bar['high']):
            valid_bars.append(date)
    # Five complete sessions are the evidence requirement of this setup only.
    # Other setups remain available with fewer observations.
    required_dates = []
    if today:
        cursor = datetime.fromisoformat(today).date()
        for _ in range(5):
            cursor = previous_trading_day(cursor)
            required_dates.append(cursor.isoformat())
    daily_ready = len(required_dates) == 5 and set(required_dates).issubset(set(valid_bars))
    add('multi_day_trend', common and daily_ready,
        base_missing + ([] if daily_ready else ['FIVE_CONSECUTIVE_VERIFIED_DAILY_OHLCV_BARS']),
        dict(complete_daily_sessions=len(set(valid_bars)),
             interpretation='完整日 K 專用；盤中快照不計入'))
    return dict(eligible_strategies=[key for key in STRATEGY_IDS if specs[key]['eligible']],
                strategies=specs,
                history_kind=('TWSE_DAILY_AND_INTRADAY_SAMPLES' if valid_bars else
                              'INTRADAY_SAMPLES_NOT_DAILY_BARS'))
