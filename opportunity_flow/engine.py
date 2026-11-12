"""Compute features and explicit scenarios first; the LLM never does arithmetic."""
from __future__ import annotations

from datetime import date,timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

from .common import canonical, digest, stamp
from .data import validate_quote,validate_reference


def economics(entry, stop, target, quantity, spread, p):
    d = lambda x: Decimal(str(x))
    if type(quantity) is not int or quantity <= 0 or not 0 < stop < entry < target or spread < 0:
        raise ValueError("INVALID_SCENARIO")
    slip = d(p["slippage_bps_each_side"]) / 10000
    cost = d(entry) * (1 + slip) * quantity
    proceeds = (d(target) - d(spread)) * (1 - slip) * quantity
    salvage = (d(stop) - d(spread)) * (1 - slip) * quantity
    if salvage <= 0:
        raise ValueError("EXIT_PRICE_NONPOSITIVE")
    fee = lambda gross: max(d(p["minimum_commission_twd"]), (gross * d(p["commission_rate"])).to_integral_value(rounding=ROUND_FLOOR))
    tax = lambda gross: (gross * d(p["sell_tax_rate"])).to_integral_value(rounding=ROUND_CEILING)
    invested = cost + fee(cost)
    gain = proceeds - fee(proceeds) - tax(proceeds) - invested
    loss = invested - (salvage - fee(salvage) - tax(salvage))
    return dict(quantity=quantity, entry=entry, stop=stop, target=target,
        invested_twd=round(float(invested), 4), net_profit_twd=round(float(gain), 4),
        modeled_loss_twd=round(float(loss), 4), net_return=round(float(gain / invested), 6),
        reward_risk=round(float(gain / loss), 6) if loss > 0 else None,
        spread_reserve=spread, slippage_bps_each_side=p["slippage_bps_each_side"])


def features(bars,holding_sessions=5):
    closes = [b["close"] for b in bars]
    tr = [max(b["high"] - b["low"], abs(b["high"] - closes[i-1]), abs(b["low"] - closes[i-1]))
          for i, b in enumerate(bars) if i]
    avg = lambda xs: sum(xs) / len(xs)
    amounts = [b["turnover_twd"] for b in bars[-20:] if b["turnover_twd"] is not None]
    excursions=sorted(max(b["high"] for b in bars[i+1:i+1+holding_sessions])/bars[i]["close"]-1
        for i in range(len(bars)-holding_sessions))
    return dict(close=closes[-1], ma5=avg(closes[-5:]), ma20=avg(closes[-20:]),
        return_5d=closes[-1] / closes[-6] - 1, return_20d=closes[-1] / closes[-21] - 1,
        atr14=avg(tr[-14:]), average_volume_20d=avg([b["volume"] for b in bars[-20:]]),
        average_turnover_20d=sum(amounts) / 20 if len(amounts) == 20 else None,
        volume_ratio_5d=bars[-1]["volume"] / avg([b["volume"] for b in bars[-6:-1]]) if any(b["volume"] for b in bars[-6:-1]) else 0,
        historical_high_excursion_sessions=holding_sessions,historical_high_excursion_samples=len(excursions),
        historical_high_excursion_median=excursions[len(excursions)//2],
        historical_high_excursion_p75=excursions[int((len(excursions)-1)*.75)])


def setups(bars, f):
    last, prior = bars[-1], bars[-2]
    found = []
    if f["ma5"] > f["ma20"] and f["close"] >= f["ma20"] and f["return_5d"] > 0:
        found.append("TREND_CONTINUATION")
    if prior["close"] < bars[-3]["close"] and last["close"] > prior["high"] and last["close"] > last["open"]:
        found.append("RECOVERY_RECLAIM")
    if last["close"] > max(b["high"] for b in bars[-21:-1]):
        found.append("COMPLETED_BREAKOUT")
    if f["ma5"] > f["ma20"] and last["low"] <= f["ma5"] <= last["close"]:
        found.append("TREND_PULLBACK")
    if not found and last["close"] >= max(b["high"] for b in bars[-21:-1]) - f["atr14"] * .3:
        found.append("NEAR_BREAKOUT_WATCH")
    return found


def levels(bars):
    supports, targets = {}, {}
    # The right neighbour must already be a completed daily bar.
    for i in range(1, len(bars)-1):
        b, before, after = bars[i], bars[i-1], bars[i+1]
        if b["low"] < before["low"] and b["low"] <= after["low"]:
            supports[b["low"]] = dict(price=b["low"], date=b["date"], known_on=after["date"], kind="CONFIRMED_SWING_LOW")
        if b["high"] > before["high"] and b["high"] >= after["high"]:
            targets[b["high"]] = dict(price=b["high"], date=b["date"], known_on=after["date"], kind="CONFIRMED_SWING_HIGH")
    last = bars[-1]
    supports[last["low"]] = dict(price=last["low"], date=last["date"], known_on=last["date"], kind="LAST_COMPLETED_LOW")
    for window in (20, 60):
        b = max(bars[-window:], key=lambda b: b["high"])
        targets[b["high"]] = dict(price=b["high"], date=b["date"], known_on=last["date"], kind=f"OBSERVED_{min(window,len(bars))}D_HIGH")
    return sorted(supports.values(), key=lambda r: r["price"], reverse=True), sorted(targets.values(), key=lambda r: r["price"])


def routes(bars, f, q, p, price_reference=None, account=None):
    supports, targets = levels(bars)
    reference = q["ask"] if q else price_reference["price"] if price_reference else f["close"]
    spread = q["ask"] - q["bid"] if q else 0
    entries = [(reference, "CURRENT_ASK" if q else "PUBLIC_PRICE_RESEARCH_REFERENCE" if price_reference else "LAST_CLOSE_RESEARCH_REFERENCE")]
    entries += [(r["price"], "OBSERVED_SUPPORT_PULLBACK") for r in supports if reference * .95 <= r["price"] < reference]
    result = []
    account=account or {}
    held=int(account.get('held_qty',0))
    unit=max(reference,float(account.get('average_cost_twd') or reference))
    allocation=max(0,p['capital_twd']*p['allocation_fraction']-held*unit)
    funding=min(allocation,float(account.get('available_cash_twd',allocation)))
    for entry, kind in entries:
        stops = [r for r in supports if r["price"] < entry and entry-r["price"] >= max(p["minimum_stop_atr"] * f["atr14"], 2*spread)]
        overhead = [r for r in targets if r["price"] > entry]
        if not stops or not overhead:
            result.append(dict(route_id=digest([entry, kind])[:16], entry=entry, kind=kind,
                eligible=False, blockers=["STRUCTURAL_STOP_MISSING" if not stops else "OBSERVED_TARGET_MISSING"],
                waiting_for="NEW_COMPLETED_PRICE_STRUCTURE", economics=None))
            continue
        stop, target = stops[0], overhead[0]
        # Nearest observed overhead target prevents silently jumping resistance.
        ceiling = min(999, int(max(0,funding) / entry))
        existing_risk=float(account.get('risk_by_stop',{}).get(str(float(stop['price'])),0)) if not held or account.get('risk_by_stop') else None
        viable = []
        for qty in range(ceiling, 0, -1):
            e = economics(entry, stop["price"], target["price"], qty, spread, p)
            reserve=max(float(account.get('minimum_fee_reserve_twd',0)),entry*qty*float(account.get('buy_cash_buffer_rate',0)))
            if (max(e['invested_twd'],entry*qty+reserve)<=funding and existing_risk is not None
                    and e['modeled_loss_twd']+existing_risk<=p['capital_twd']*p['maximum_position_risk_fraction']):
                viable.append(e)
                break
        e = viable[0] if viable else economics(entry, stop["price"], target["price"], 1, spread, p)
        blocks = []
        if not viable:
            blocks.append("CAPITAL_OR_RISK_CAP")
        if e["net_return"] < p["minimum_net_return"]:
            blocks.append("NET_RETURN_BELOW_FLOOR")
        if e["reward_risk"] is None or e["reward_risk"] < p["minimum_reward_risk"]:
            blocks.append("REWARD_RISK_BELOW_FLOOR")
        if e["net_profit_twd"] < p["minimum_net_profit_twd"]:
            blocks.append("NET_PROFIT_BELOW_FLOOR")
        route = dict(entry=entry, stop=stop, target=target, kind=kind, economics=e,
            existing_modeled_risk_twd=existing_risk,allocation_remaining_twd=round(allocation,4),
            target_distance_atr=round((target["price"]-entry)/f["atr14"],4),
            target_required_return=round(target["price"]/entry-1,6),
            eligible=not blocks, blockers=blocks,
            waiting_for="PRICE_AT_OR_BELOW_ENTRY" if entry < reference else "RESEARCH_AND_EXECUTION_DATA")
        route["route_id"] = digest(route)[:16]
        result.append(route)
    return result


def build(symbol, dataset, at, p, raw_quote=None, event=None, account=None):
    at = stamp(at)
    day = at.date().isoformat()
    sessions = [d for d in dataset["sessions"] if d < day]
    base = dict(symbol=symbol, evaluated_at=at.isoformat(), state="DATA_BLOCKED", reasons=[], packet=None)
    if not sessions:
        base["reasons"] = ["COMPLETED_CALENDAR_MISSING"]
        return base
    verified_through=dataset["manifest"].get("calendar_verified_through",dataset["manifest"].get("end",dataset["sessions"][-1]))
    previous_calendar_day=(at.date()-timedelta(days=1)).isoformat()
    if verified_through < previous_calendar_day:
        base["reasons"]=["CALENDAR_COVERAGE_NOT_CURRENT"]
        return base
    required = sessions[-p["minimum_history_sessions"]:]
    # Filtering happens before any feature, rank, level or packet is computed.
    bars = [b for b in dataset["grouped"].get(symbol, []) if b["date"] < day][-p["history_sessions"]:]
    errors = [e for e in dataset["errors"].get(symbol, []) if e.get("date") in required]
    if len(required) < p["minimum_history_sessions"] or len(bars) < p["minimum_history_sessions"]:
        base["reasons"] = ["INSUFFICIENT_COMPLETED_HISTORY"]
        return base
    if errors or not set(required).issubset({b["date"] for b in bars}):
        base["reasons"] = ["RECENT_HISTORY_GAP_OR_INVALID_BAR"]
        return base
    if bars[-1]["date"] != sessions[-1] or len({b["exchange"] for b in bars}) != 1:
        base["reasons"] = ["STALE_HISTORY_OR_EXCHANGE_CHANGED"]
        return base
    older_gaps=sorted(set(sessions[-p["history_sessions"]:])-{b["date"] for b in bars})
    if older_gaps:
        bars=[b for b in bars if b["date"]>older_gaps[-1]]
    if bars[-1]["volume"]<=0:
        base["reasons"]=["LATEST_SESSION_HAS_NO_TRADES"]
        return base
    exchange = bars[-1]["exchange"]
    base["exchange"] = exchange
    if account and (account.get('pending_execution') or account.get('trading_enabled') is False or account.get('buy_block_reason')):
        base.update(state='ACCOUNT_BLOCKED',reasons=[account.get('buy_block_reason') or
                    ('PENDING_BROKER_ORDER' if account.get('pending_execution') else 'AI_TRADING_DISABLED')])
        return base
    f = features(bars,p["holding_sessions"])
    if (f["average_turnover_20d"] is None or f["average_turnover_20d"] < p["minimum_average_turnover_twd"]
            or f["average_volume_20d"] < p["minimum_average_volume_shares"] or f["atr14"] <= 0):
        base.update(state="SCREENED_OUT", reasons=["LIQUIDITY_UNKNOWN_OR_BELOW_FLOOR"])
        return base
    q, quote_error = validate_quote(raw_quote, symbol, exchange, at, p["quote_max_age_seconds"])
    price_reference=validate_reference(raw_quote,symbol,exchange,at,p["quote_max_age_seconds"])
    families = setups(bars, f)
    observed_price=q["ask"] if q else price_reference["price"] if price_reference else None
    thresholds=dict(ma5=f["ma5"],ma20=f["ma20"],previous_high=bars[-1]["high"],previous_low=bars[-1]["low"],
                    high20=max(b["high"] for b in bars[-20:]))
    trigger_states={k:"UNOBSERVED" if observed_price is None else "ABOVE" if observed_price>v else "BELOW" if observed_price<v else "AT"
                    for k,v in thresholds.items()}
    if observed_price is not None:
        if observed_price>max(b["high"] for b in bars[-20:]):
            families.append("INTRADAY_BREAKOUT_WATCH")
        if bars[-1]["close"]<bars[-2]["close"] and observed_price>bars[-1]["high"]:
            families.append("INTRADAY_RECLAIM_WATCH")
    if not families:
        base.update(state="NO_SETUP", reasons=["NO_CURRENT_SETUP"])
        return base
    scenarios = routes(bars, f, q, p,price_reference,account)
    # Data availability and trade economics are explicit, separate dimensions.
    eligible = [r for r in scenarios if r["eligible"]]
    base.update(state="REVIEW_PENDING" if eligible else "ECONOMIC_BLOCKED",
        reasons=[] if eligible else sorted({r for s in scenarios for r in s["blockers"]}),
        setup_families=families, rank_score=round(f["return_20d"] + .1*f["return_5d"], 6))
    source_rows = [dict(date=b["date"], sha256=b["source_sha256"], source=b["source"]) for b in bars]
    evidence = [dict(evidence_id="E_FEATURES", facts={k: round(v,6) if isinstance(v,float) else v for k,v in f.items()}),
                dict(evidence_id="E_LEVELS", facts=dict(zip(("supports", "targets"), levels(bars)))),
                dict(evidence_id="E_ECONOMICS", facts=scenarios),
                dict(evidence_id="E_DATA_LIMITS", facts=dict(price_basis=bars[-1]["price_basis"],
                    point_in_time_certified=dataset["manifest"].get("point_in_time_certified",False),
                    corporate_actions_verified=dataset["manifest"].get("corporate_actions_verified",False),
                    older_history_gaps=older_gaps,
                    price_reference_basis="ODDLOT_QUOTE" if q else "PUBLIC_BOARD_LOT_REFERENCE" if price_reference else "LAST_COMPLETED_CLOSE",
                    spread_known=q is not None,
                    quote_problem=quote_error, event_check=event or {"status":"NOT_CHECKED"}))]
    core = dict(schema_version=1, system="TW Opportunity Flow", policy=p, symbol=symbol, exchange=exchange,
        decision_session=day, completed_through=bars[-1]["date"], setup_families=families,
        trigger_states=trigger_states,
        execution_context={k:v for k,v in (account or {}).items() if k!='checked_at'} or None,
        evidence=evidence, recent_bars=[{k:b[k] for k in ("date","open","high","low","close","volume")} for b in bars[-8:]],
        source_lineage_sha256=digest(source_rows), source_history_sha256=digest(bars),
        decision_scope="RESEARCH_ONLY_NOT_ORDER_PERMISSION", selected_routes=[r["route_id"] for r in eligible])
    packet = {**core, "packet_id": digest(core)}
    # Quote time, last-trade volume and sub-threshold price noise do not retrigger an LLM.
    material = dict(structure=digest({**core,"evidence":evidence[:2]+evidence[3:]}),
        price_band=math.floor(math.log((q["ask"] if q else price_reference["price"])/f["close"])/math.log(1+p["material_price_change_fraction"])) if q or price_reference else "NO_QUOTE",
        eligible_routes=[r["kind"] for r in eligible], quote_problem=quote_error, event=event)
    base.update(packet=packet, material_key=digest(material), quote=q, quote_problem=quote_error,
                hypothetical_reference=q is None, input_characters=len(canonical(packet)))
    return base
