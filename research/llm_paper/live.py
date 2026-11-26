"""Build common production-style inputs from fresh read-only Capital observations."""
from copy import deepcopy

from .common import Market,instant,decimal


def live_frame(snapshot,sources,history,at):
    clock=instant(at);market=Market();day=clock.astimezone(market.tz).date().isoformat()
    if sources["calendar_valid_year"]!=int(day[:4]) or day not in sources["sessions"]:
        raise ValueError("UNVERIFIED_OR_CLOSED_SESSION")
    if instant(sources["at"])>clock:
        raise ValueError("FUTURE_OFFICIAL_SOURCE")
    quotes=[]
    for raw in snapshot["quotes"]:
        q=deepcopy(raw);symbol=q["symbol"]
        meta=sources["instruments"].get(symbol)
        if not meta or meta["exchange"]!=q["exchange"] or instant(meta["received_at"])>clock:
            continue
        q["asset_type"]=meta["asset_type"]
        age=(clock-instant(q["quote_timestamp"])).total_seconds()
        received=q.get('received_at')
        receipt_ok=bool(received and 0<=(clock-instant(received)).total_seconds()<=120)
        sides_ok=bool(q.get('bid') and q.get('ask') and 0<decimal(q['bid'])<=decimal(q['ask']))
        q["quote_status"]="LIVE" if market.session_open(at,sources["sessions"]) and 0<=age<=120 and receipt_ok and sides_ok and q["is_trial"] is False else "LAST_KNOWN"
        q["warnings"]=[] if q["quote_status"]=="LIVE" else ["不是即時行情"]
        quotes.append(q)
    from quant_scanner import scan_and_rank
    from .common import read,ROOT
    scanned,ranking,top=scan_and_rank(quotes,read(ROOT/"config/scan_rules.json"),clock)
    metrics={r["symbol"]:r for r in scanned};ranks={r["symbol"]:r["rank"] for r in ranking}
    top_symbols={x["symbol"] for x in top}
    from strategy_evidence import assess
    candidates=[]
    for q in quotes:
        symbol=q["symbol"]
        daily=sources["daily"].get(symbol,{})
        bars=[{k:b[k] for k in ("date","open","high","low","close","volume","complete","source")} for b in daily.get("bars",[])]
        status=daily.get("status",{})
        if any(b["date"]>=day for b in bars) or status.get("latest_retrieved_at") and instant(status["latest_retrieved_at"])>clock:
            raise ValueError("FUTURE_DAILY_PRICE_OR_RECEIPT")
        prior=[r for r in history.get(symbol,[]) if instant(r["exchange_time"])<=clock]
        evidence=assess(dict(q,daily_bars=bars),prior)
        depth_at=q.get("depth_received_at")
        depth_ok=bool(depth_at and 0<=(clock-instant(depth_at)).total_seconds()<=10 and q.get("depth_status")=="ODDLOT_CALLBACK_SHARES" and
                      type(q.get("ask_size_shares")) is int and q["ask_size_shares"]>0 and q.get("limit_up") and q.get("limit_down"))
        daily_ok=bool(status.get("latest_date")==status.get("expected_latest_date") and status.get("prior_complete_sessions",0)>=21)
        c=dict(q,quant_rank=ranks.get(symbol) if symbol in top_symbols else None,quant_score=metrics[symbol].get("quant_score"),
               components=metrics[symbol]["components"],metrics=metrics[symbol]["metrics"],market_history=prior[-15:],
               daily_bars=bars,daily_trend=daily.get("trend"),daily_history_status=status,
               strategy_evidence=evidence,eligible_strategies=evidence["eligible_strategies"],
               paper_buy_allowed=depth_ok and daily_ok,analysis_source="TOP10" if symbol in top_symbols else "AVAILABLE_QUOTE_FOR_PAPER_POSITION")
        candidates.append(c)
    return dict(id="forward-"+at.replace(":","").replace("+","_"),at=at,quotes=quotes,candidates=candidates,
                source_manifest_sha256=sources.get("calendar_sha256"),scope="FORWARD_PAPER")
