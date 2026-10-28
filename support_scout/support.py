from __future__ import annotations

from dataclasses import asdict
from statistics import fmean

from .contracts import Rules, Series


def atrs(series: Series, window: int) -> list[float | None]:
    """Simple mean of true ranges, not Wilder smoothing; no future observations."""
    ranges = []
    result = []
    for i, b in enumerate(series.bars):
        previous = series.bars[i - 1].close if i else b.close
        ranges.append(max(b.high - b.low, abs(b.high - previous), abs(b.low - previous)))
        result.append(fmean(ranges[-window:]) if len(ranges) >= window else None)
    return result


def detect_support(series: Series, rules: Rules) -> dict:
    bars = series.bars
    if len(bars) < rules.min_bars:
        return {"state": "INSUFFICIENT_DATA", "required_bars": rules.min_bars, "bar_count": len(bars),
                "technical_candidate": False, "priority_score": None, "evidence": [], "episodes": [],
                "blockers": ["INSUFFICIENT_HISTORY"]}
    atr = atrs(series, rules.atr_window)
    active = None
    episodes = []
    evidence = []
    last_terminal = -rules.reset_after_break - 1

    def record(kind: str, i: int, values: dict):
        ev = {"id": f"{series.symbol}:{kind}:{bars[i].session}", "kind": kind,
              "session": bars[i].session, "available_at": bars[i].available_at, "values": values}
        evidence.append(ev)
        return ev["id"]

    for i, bar in enumerate(bars):
        if active and active["state"] not in {"INVALIDATED", "EXPIRED"}:
            if bar.close < active["lower"]:
                active["state"] = "INVALIDATED"
                active["invalidated_at"] = bar.session
                active["evidence_ids"].append(record("INVALIDATED", i, {"close": bar.close, "frozen_lower": active["lower"]}))
                last_terminal = i
                continue
            if i - active["anchor_index"] > rules.episode_lifetime or (active.get("confirmed_index") is not None and i - active["confirmed_index"] > rules.confirmation_lifetime):
                active["state"] = "EXPIRED"
                active["evidence_ids"].append(record("EXPIRED", i, {}))
                last_terminal = i
                continue
            if active["state"] == "BASE_FORMING" and bar.close >= active["upper"] + rules.bounce_atr * active["atr"]:
                active.update(state="BOUNCE_SEEN", bounce_index=i, bounce_at=bar.session)
                active["evidence_ids"].append(record("BOUNCE", i, {"close": bar.close, "threshold": active["upper"] + rules.bounce_atr * active["atr"]}))
                continue
            if (active["state"] == "BOUNCE_SEEN" and i > active["bounce_index"]
                    and i - active["anchor_index"] >= rules.min_retest_separation
                    and active["lower"] <= bar.low <= active["upper"] and bar.close >= active["upper"]):
                active.update(state="RETEST_HELD", retest_index=i, retest_at=bar.session, retest_high=bar.high)
                active["evidence_ids"].append(record("RETEST", i, {"low": bar.low, "close": bar.close, "high": bar.high,
                                                                "volume": bar.volume, "frozen_lower": active["lower"], "frozen_upper": active["upper"]}))
                continue
            if (active["state"] == "RETEST_HELD" and i > active["retest_index"]
                    and bar.close > active["retest_high"] and bar.close >= active["upper"]
                    and bars[i - 1].close >= active["upper"]):
                active.update(state="RULE_CONFIRMED", confirmed_index=i, confirmed_at=bar.session)
                active["evidence_ids"].append(record("CONFIRMATION", i, {"close": bar.close, "retest_high": active["retest_high"]}))
                continue
            # Never move the price anchor of a live episode.
            continue

        p = i - rules.pivot_right
        if p < max(rules.high_window, rules.pivot_left) or p <= last_terminal + rules.reset_after_break:
            continue
        pivot = bars[p]
        left = bars[p - rules.pivot_left:p]
        right = bars[p + 1:i + 1]
        previous_high = max(b.high for b in bars[p - rules.high_window:p])
        if (len(right) != rules.pivot_right or not all(pivot.low < b.low for b in (*left, *right))
                or 1 - pivot.low / previous_high < rules.min_drawdown or not atr[i] or atr[i] <= 0):
            continue
        width = max(pivot.low * rules.zone_price, atr[i] * rules.zone_atr)
        if bar.close < pivot.low - width:
            continue
        active = {"state": "BASE_FORMING", "anchor_index": p, "anchor_at": pivot.session,
                  "detected_at": bar.session, "available_at": bar.available_at,
                  "anchor": pivot.low, "lower": pivot.low - width, "upper": pivot.low + width,
                  "atr": atr[i], "drawdown_at_anchor": 1 - pivot.low / previous_high}
        active["evidence_ids"] = [record("ANCHOR", i, {"pivot_session": pivot.session, "pivot_low": pivot.low,
            "lower": active["lower"], "upper": active["upper"], "atr_at_detection": active["atr"],
            "previous_high": previous_high, "pivot_right_sessions": rules.pivot_right})]
        episodes.append(active)

    latest = bars[-1]
    japan = series.metadata.get("currency") == "JPY"
    turnover20 = fmean((b.turnover_jpy if japan else b.turnover_twd) for b in bars[-20:])
    current_id = record("LATEST", len(bars) - 1, asdict(latest))
    blockers = []
    state = active["state"] if active else "NO_SETUP"
    distance = (latest.close - active["upper"]) / active["atr"] if active else None
    extended = bool(active and distance > rules.max_distance_atr)
    if state != "RULE_CONFIRMED":
        blockers.append("SUPPORT_NOT_RULE_CONFIRMED")
    if extended:
        blockers.append("EXTENDED_FROM_SUPPORT")
    if turnover20 < (rules.min_turnover_jpy if japan else rules.min_turnover_twd):
        blockers.append("LOW_20_SESSION_TURNOVER")
    if latest.limit_locked:
        blockers.append("LIMIT_LOCKED")
    if active:
        # Locked daily prices cannot demonstrate freely traded support/acceptance.
        relevant = set(range(active["anchor_index"] - rules.pivot_left,
                             active["anchor_index"] + rules.pivot_right + 1))
        relevant.update(active[k] for k in ("bounce_index", "retest_index", "confirmed_index") if k in active)
        if any(bars[i].limit_locked for i in relevant):
            blockers.append("LOCKED_PRICE_IN_SUPPORT_EVIDENCE")
    if active and state == "RULE_CONFIRMED" and latest.close < active["upper"]:
        blockers.append("LATEST_CLOSE_NOT_ABOVE_ZONE")
    if active and latest.low < active["lower"]:
        blockers.append("LATEST_INTRADAY_UNDERCUT")

    ratio = None
    if active and "retest_index" in active:
        ratio = bars[active["retest_index"]].volume / bars[active["bounce_index"]].volume
    base_score = {"BASE_FORMING": 10, "BOUNCE_SEEN": 25, "RETEST_HELD": 50, "RULE_CONFIRMED": 70}.get(state, 0)
    proximity_score = 10 * max(0, 1 - max(distance or 0, 0) / rules.max_distance_atr) if active else 0
    volume_score = 10 if ratio is not None and ratio < 1 else 0
    range_score = 5 * ((latest.close - latest.low) / (latest.high - latest.low)) if latest.high > latest.low else 0
    components = {"state": base_score, "proximity": round(proximity_score, 4), "drying_retest_volume": volume_score,
                  "latest_close_position": round(range_score, 4)}
    return {"state": state, "position": "EXTENDED" if extended else "NEAR_SUPPORT" if active else "NO_ZONE",
            "bar_count": len(bars), "latest_session": latest.session, "latest_close": latest.close,
            "support": {key: value for key, value in active.items() if not key.endswith("_index")} if active else None,
            "distance_above_zone_atr": distance,
            ("average_turnover_20_jpy" if japan else "average_turnover_20_twd"): turnover20,
            "turnover_currency": "JPY" if japan else "TWD",
            "retest_vs_bounce_volume": ratio, "priority_components": components,
            "priority_score": round(sum(components.values()), 4) if base_score else None,
            "score_meaning": "UNVALIDATED_RESEARCH_PRIORITY_NOT_PROBABILITY",
            "technical_candidate": not blockers, "blockers": blockers,
            "invalidation_distance_fraction": (latest.close - active["lower"]) / latest.close if active else None,
            "stress_scenario": {"assumption": "hypothetical_stock_return_minus_6pct_not_probability_or_SOX_beta",
                                "price": latest.close * 0.94,
                                "below_invalidation": latest.close * 0.94 < active["lower"] if active else None},
            "evidence": evidence, "latest_evidence_id": current_id,
            "episodes": [{key: value for key, value in e.items() if not key.endswith("_index")} for e in episodes]}
