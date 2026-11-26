"""Freeze every eligible archived decision frame, independently of its outcome."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta, date
from pathlib import Path

from .common import read, save, digest, instant, protocol, ROOT, stamp


def prepare(archive, output):
    archive, out = Path(archive), Path(output)
    out.mkdir(parents=True, exist_ok=False)
    frames, excluded = [], []
    for folder in sorted((archive / "data/analysis/runs").iterdir()):
        required = ("openai_request.json", "multi_market_snapshot.json", "run.json")
        if not all((folder / f).is_file() for f in required):
            continue
        request, snap, run = (read(folder / f) for f in required)
        payload = __import__('json').loads(request["input"])
        if not isinstance(payload.get("analysis_set"), list):
            continue
        at = run.get("llm_request_started_at")
        if not at:
            excluded.append(dict(id=folder.name, reason="NO_ORIGINAL_LLM_REQUEST_TIME"))
            continue
        cutoff = instant(at)
        quotes = []
        for q in snap.get("quotes", []):
            if not q.get("quote_timestamp") or not q.get("received_at"):
                continue
            if instant(q["quote_timestamp"]) > cutoff or instant(q["received_at"]) > cutoff:
                raise ValueError("ARCHIVED_FUTURE_QUOTE: " + folder.name)
            quotes.append(q)
        if not any(q.get("quote_status") == "LIVE" and q.get("is_trial") is False for q in quotes):
            excluded.append(dict(id=folder.name, reason="NO_LIVE_NONTRIAL_QUOTE"))
            continue
        candidates = []
        for original in payload["analysis_set"]:
            c = deepcopy(original)
            if c.get("quote_timestamp") and instant(c["quote_timestamp"]) > cutoff:
                raise ValueError("ARCHIVED_FUTURE_CANDIDATE")
            bars = c.get("daily_bars", [])
            if any(b["date"] >= at[:10] for b in bars):
                raise ValueError("ARCHIVED_FUTURE_DAILY_BAR")
            retrieved = (c.get("daily_history_status") or {}).get("latest_retrieved_at")
            if retrieved and instant(retrieved) > cutoff:
                raise ValueError("ARCHIVED_FUTURE_DAILY_RECEIPT")
            for h in c.get("market_history", []):
                for key in ("exchange_time", "quote_timestamp", "received_at", "observed_at"):
                    if h.get(key) and instant(h[key]) > cutoff:
                        raise ValueError("ARCHIVED_FUTURE_INTRADAY_INPUT")
            # Only exogenous evidence survives the freeze. Each book supplies its
            # own cash, holdings, acquisition plans, dates and sellable quantity.
            for key in ("position", "paper_position", "ai_cost_basis", "ai_trade_dates", "buy_plans", "max_sell_qty", "sellable_context", "max_buy_qty", "budget_price"):
                c.pop(key, None)
            candidates.append(c)
        frames.append(dict(id=folder.name, at=at, quotes=quotes, candidates=candidates,
                           original_model=request["model"], original_instructions_sha256=digest(request["instructions"].encode()),
                           original_tool_types=[t.get("type") for t in request.get("tools", [])],
                           source_files={f:digest((folder / f).read_bytes()) for f in required},
                           original_strategy=run.get("strategy")))
    frames.sort(key=lambda f: instant(f["at"]))
    if not frames or len({x["at"] for x in frames}) != len(frames):
        raise ValueError("EMPTY_OR_DUPLICATE_COHORT")
    calendar_source = archive / "market_calendar.py"
    # Use the already documented 2026 calendar, not the current date or a
    # weekday-only calendar; source and known emergency closure are retained.
    from market_calendar import twse_closed_reason
    begin = date.fromisoformat(frames[0]["at"][:10])
    end = date.fromisoformat(frames[-1]["at"][:10]) + timedelta(days=40)
    sessions = []
    while begin <= end:
        if twse_closed_reason(begin) is None:
            sessions.append(begin.isoformat())
        begin += timedelta(days=1)
    result = dict(version="formal-llm-cohort-v1", data_role="OCTOBER_DEVELOPMENT_NOT_UNSEEN_VALIDATION", frozen_at=stamp(),
                  frames=frames, sessions=sessions, excluded=excluded, model_counts=dict(Counter(x["original_model"] for x in frames)),
                  days=dict(Counter(x["at"][:10] for x in frames)), calendar_sha256=digest(calendar_source.read_bytes()),
                  missing=["HISTORICAL_ODDLOT_DEPTH", "INDEPENDENT_HISTORICAL_NEWS_PUBLICATION_TIMES"],
                  tool_policy="NO_NEW_WEB_SEARCH_DURING_HISTORICAL_REPLAY", cohort_selection="ALL_ELIGIBLE_REQUESTS_WITHOUT_LOOKING_AT_DECISIONS_OR_FUTURE_PNL")
    result["cohort_sha256"] = digest(result)
    save(out / "cohort.json", result)
    save(out / "summary.json", {k:v for k,v in result.items() if k != "frames"})
    return result
