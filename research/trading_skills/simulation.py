"""Causal OHLC moving-block bootstrap, net outcomes and prequential calibration.

Inspired by Jake Schincariol's MIT trader-timemachine; independently implemented
for daily research. This is not a reproduction of its 78-slot intraday algorithm.
"""
from __future__ import annotations

from datetime import datetime, time
import math
import random
import statistics

from .market import Costs, Market, decimal, instant


def state(closes):
    if len(closes) < 21:
        return None
    returns = [b / a - 1 for a, b in zip(closes[-21:-1], closes[-20:])]
    return dict(regime="UP" if closes[-1] > closes[-21] else "DOWN_OR_FLAT", volatility=statistics.pstdev(returns))


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = statistics.fmean(max(x, 0) for x in changes[:n])
    loss = statistics.fmean(max(-x, 0) for x in changes[:n])
    for x in changes[n:]:
        gain = (gain * (n - 1) + max(x, 0)) / n
        loss = (loss * (n - 1) + max(-x, 0)) / n
    return 100 - 100 / (1 + gain / loss) if loss else (50.0 if gain == 0 else 100.0)


def setup_tags(closes):
    if len(closes) < 30:
        return []
    last = closes[-1]
    mean = statistics.fmean(closes[-20:])
    previous = statistics.fmean(closes[-21:-1])
    tags = []
    if last > max(closes[-21:-1]):
        tags.append("breakout_20d")
    if closes[-6] / closes[-16] > 1.10 and (max(closes[-5:]) - min(closes[-5:])) / last < .05:
        tags.append("bull_flag")
    if closes[-2] < previous and last > mean:
        tags.append("reclaim_ma20")
    if rsi(closes) < 30:
        tags.append("oversold")
    if rsi(closes[:-1]) < 30 and last > closes[-2]:
        tags.append("oversold_bounce")
    return tags


def pool_blocks(bars, as_of, protocol, block_size, regimes=None, conditioned=False, strict_availability=False):
    """No block wraps, crosses gaps, contains illiquid days or ends after t.

    Conditioning uses the state BEFORE the first bar of each block. The current
    state is known at t. Publication/receipt times are mandatory in forward mode.
    """
    history = [b for b in bars if b["date"] <= as_of]
    if strict_availability:
        cutoff = datetime.combine(datetime.fromisoformat(as_of).date(), time(23, 59, 59), tzinfo=Market().tz)
        for b in history:
            if not b.get("available_at") or instant(b["available_at"]) > cutoff or instant(b["retrieved_at"]) > cutoff:
                raise ValueError("HISTORICAL_AVAILABILITY_UNVERIFIED")
    start = max(21, len(history) - protocol["history_sessions"])
    current = state([b["close"] for b in history])
    current_regime = (regimes or {}).get(as_of)
    blocks = []
    for i in range(start, len(history) - block_size + 1):
        rows = history[i:i + block_size]
        previous = history[i - 1]
        if any(b["session_index"] != a["session_index"] + 1 for a, b in zip([previous] + rows, rows)):
            continue
        if any(b.get("turnover_twd") is None or b["turnover_twd"] < protocol["minimum_turnover_twd"] or b["volume"] <= 0 for b in rows):
            continue
        if any(abs(b["close"] / a["close"] - 1) > .18 for a, b in zip([previous] + rows, rows)):
            continue
        before = state([b["close"] for b in history[:i]])
        if conditioned:
            if not before or not current or current_regime is None or (regimes or {}).get(previous["date"]) != current_regime:
                continue
            if not protocol["volatility_ratio_low"] * current["volatility"] <= before["volatility"] <= protocol["volatility_ratio_high"] * current["volatility"]:
                continue
        factors = []
        for row in rows:
            factors.append(tuple(row[k] / previous["close"] for k in ("open", "high", "low", "close")))
            previous = row
        blocks.append(dict(start=rows[0]["date"], end=rows[-1]["date"], factors=factors))
    return blocks


def exit_price(bar, stop, target, *, expired=False):
    """Adverse opening gaps and stop first for ambiguous OHLC. No gap upside bonus."""
    if decimal(bar["open"]) <= stop:
        return decimal(bar["open"]), "GAP_STOP"
    if decimal(bar["low"]) <= stop:
        return stop, "STOP"
    if decimal(bar["high"]) >= target:
        return target, "TARGET"
    return (decimal(bar["close"]), "TIME") if expired else (None, None)


def simulate(blocks, reference, day, protocol, *, seed=None):
    if len(blocks) < protocol["min_pool_blocks"]:
        return dict(status="INSUFFICIENT_POOL", pool_blocks=len(blocks), p_net_positive=None,
                    expected_net_twd=None, probability_kind="SIMULATION_FREQUENCY_NOT_LIVE_WIN_RATE")
    costs, market = Costs(protocol["costs"]), Market()
    rng = random.Random(protocol["seed"] if seed is None else seed)
    pnl, targets, stops = [], 0, 0
    for _ in range(protocol["paths"]):
        price, path = float(reference), []
        # Daily experiment excludes entry-day exits, matching selection_lab.
        while len(path) < protocol["horizon"] + 1:
            for ratios in rng.choice(blocks)["factors"]:
                o, h, l, c = [price * v for v in ratios]
                path.append(dict(open=o, high=h, low=l, close=c))
                price = c
                if len(path) == protocol["horizon"] + 1:
                    break
        entry = costs.fill_price(path[0]["open"], "BUY", market, day)
        stop = market.round_price(entry * (1 - decimal(protocol["stop_fraction"])), day)
        target = market.round_price(entry * (1 + decimal(protocol["target_fraction"])), day, up=True)
        qty = costs.size(entry, protocol["notional_twd"], protocol["notional_twd"],
                         decimal(protocol["initial_cash_twd"]) * decimal(protocol["risk_fraction"]), stop)
        if not qty:
            pnl.append(0.0)
            continue
        for i, bar in enumerate(path[1:]):
            px, reason = exit_price(bar, stop, target, expired=i == protocol["horizon"] - 1)
            if px is not None:
                sell = costs.fill_price(px, "SELL", market, day)
                pnl.append(float(costs.amount(sell, qty, "SELL") - costs.amount(entry, qty, "BUY")))
                targets += reason == "TARGET"
                stops += reason in ("STOP", "GAP_STOP")
                break
    return dict(status="OK", paths=len(pnl), pool_blocks=len(blocks), pool_end=max(b["end"] for b in blocks),
                p_net_positive=sum(x > 0 for x in pnl) / len(pnl), p_target=targets / len(pnl),
                p_stop=stops / len(pnl), p_neither=1 - (targets + stops) / len(pnl),
                monte_carlo_standard_error=(sum(x > 0 for x in pnl) / len(pnl) * (1 - sum(x > 0 for x in pnl) / len(pnl)) / len(pnl)) ** .5,
                expected_net_twd=statistics.fmean(pnl),
                probability_kind="SIMULATION_FREQUENCY_NOT_LIVE_WIN_RATE")


def calibrated_probability(records, raw_probability, before, protocol):
    """Fixed-bin Beta(1,1), using only fully matured prior forecast outcomes."""
    bins = protocol["calibration_bins"]
    bucket = min(bins - 1, int(raw_probability * bins))
    eligible = [r for r in records if r["ready_at"] < before and r["signal_date"] < before and
                min(bins - 1, int(r["probability"] * bins)) == bucket and r.get("outcome") in (0, 1)]
    if len(eligible) < protocol["calibration_minimum"]:
        return dict(probability=None, samples=len(eligible), status="UNCALIBRATED_INSUFFICIENT_MATURED_SAMPLES")
    return dict(probability=(1 + sum(r["outcome"] for r in eligible)) / (2 + len(eligible)),
                samples=len(eligible), status="PREQUENTIAL_FIXED_BIN_ESTIMATE_NOT_LIVE_GUARANTEE")


def calibration_metrics(pairs, bins=5):
    pairs = [(float(p), int(y)) for p, y in pairs if p is not None and y in (0, 1)]
    if not pairs:
        return dict(samples=0, brier=None, log_loss=None, ece=None, bins=[])
    rows = []
    for i in range(bins):
        subset = [(p, y) for p, y in pairs if min(bins - 1, int(p * bins)) == i]
        if subset:
            rows.append(dict(bin=i, samples=len(subset), mean_probability=statistics.fmean(p for p, _ in subset),
                             observed_fraction=statistics.fmean(y for _, y in subset)))
    return dict(samples=len(pairs), brier=statistics.fmean((p - y) ** 2 for p, y in pairs),
                log_loss=-statistics.fmean(y * math.log(max(1e-12, p)) + (1 - y) * math.log(max(1e-12, 1 - p)) for p, y in pairs),
                ece=sum(r["samples"] * abs(r["mean_probability"] - r["observed_fraction"]) for r in rows) / len(pairs), bins=rows)
