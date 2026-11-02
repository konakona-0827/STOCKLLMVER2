"""Trading-session gate and delayed quotes, never a broker execution loop."""
from copy import deepcopy
from datetime import datetime,time
import hashlib
import json
import math
from pathlib import Path

from .contracts import DataError, timestamp
from .japan import JST, holidays_for_year
from .settings import ROOT
from .storage import atomic_json,pointer_lock
from .scan_schedule import reservation_slot

MAX_QUOTE_AGE_SECONDS=1800


def market_open(now):
    local=now.astimezone(JST)
    return (local.weekday()<5 and local.date().isoformat() not in holidays_for_year(local.year)
        and (time(9)<=local.time()<time(11,30) or time(12,30)<=local.time()<time(15,30)))


def validate_quote(q,symbol,as_of,*,max_age_seconds=MAX_QUOTE_AGE_SECONDS):
    if type(max_age_seconds) is not int or not 1<=max_age_seconds<=3600:raise DataError("INVALID_QUOTE_AGE_LIMIT")
    if (not isinstance(q,dict) or q.get("symbol")!=symbol or q.get("currency")!="JPY"
            or type(q.get("price")) not in (int,float) or not math.isfinite(q["price"]) or q["price"]<=0):
        raise DataError("INVALID_DELAYED_QUOTE")
    # Keep true exchange/retrieval times; never backdate a current quote.
    quoted,fetched=(timestamp(q[k]) for k in ("quoted_at","fetched_at"))
    cutoff=timestamp(as_of)
    if not quoted<=fetched<=cutoff:raise DataError("FUTURE_DELAYED_QUOTE")
    value=deepcopy(q)
    value.update(age_seconds=(cutoff-quoted).total_seconds(),usable=(cutoff-quoted).total_seconds()<=max_age_seconds,
        is_realtime=False,meaning="Yahoo延遲參考價，須以券商即時報價複核，不是可保證成交價。")
    return value


def attach_quotes(bundle):
    """Read today's archived response, not a fallback cache or a second API."""
    by_symbol={}
    for source in bundle.get("collection",{}).get("sources",[]):
        try:
            envelope=json.loads(Path(source["cache_file"]).read_text(encoding="utf-8"))
            raw=envelope["raw_json"]
            if hashlib.sha256(raw.encode("utf-8")).hexdigest()!=source["sha256"]:raise DataError("QUOTE_SOURCE_DIGEST_MISMATCH")
            m=json.loads(raw)["chart"]["result"][0]["meta"];symbol=m["symbol"]
            if "/"+symbol+"?" not in source["url"]:raise DataError("QUOTE_SOURCE_IDENTITY_MISMATCH")
            q={"symbol":symbol,"price":m["regularMarketPrice"],"currency":m["currency"],
                "quoted_at":datetime.fromtimestamp(m["regularMarketTime"],JST).isoformat(),"fetched_at":source["fetched_at"],
                "source_url":source["url"],"source_sha256":source["sha256"]}
            by_symbol[symbol]=validate_quote(q,symbol,bundle["as_of"])
        except (DataError,ValueError,KeyError,IndexError,TypeError,OSError):continue
    for raw in bundle["securities"]:
        raw["market_quote"]=by_symbol.get(raw["symbol"])
    bundle["session_scan"]={"version":"jp-session-delayed-quote-v1","max_quote_age_seconds":MAX_QUOTE_AGE_SECONDS,
        "complete_daily_patterns_only":True,"quotes_are_not_intraday_bars":True}
    return bundle


def run_session(*,now=None,observer=None,data_root=None,output_root=None,universe_path=None,rerun=False):
    now=now or datetime.now(JST)
    if not market_open(now):
        return {"status":"SKIPPED_MARKET_CLOSED","time_jst":now.astimezone(JST).isoformat(),"model_calls":0}
    root=Path(data_root or ROOT/"local_data");root.mkdir(parents=True,exist_ok=True)
    slot=reservation_slot(now).isoformat()
    entered=False
    try:
        with pointer_lock(root/"session_scan.lock"):
            entered=True
            state_path=root/"session_scan_state.json"
            state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if state.get("slot")==slot and not rerun:return {"status":"SKIPPED_DUPLICATE_SLOT","slot":slot,"model_calls":0}
            # Reserve before calling: a failed process cannot repeat the same
            # slot indefinitely and incur duplicate API charges.
            atomic_json(state_path,{"slot":slot,"status":"RUNNING","started_at":now.isoformat()})
            from .observation import observe
            report,target,code=(observer or observe)(agents=True,market="JP",universe_source="fixed",scan_limit=20,
                max_packets=4,max_calls=9,public_information=True,session_prices=True,
                db_path=root/"observations.sqlite3",portfolio_path=root/"manual_fills.json",
                output_root=output_root,universe_path=universe_path)
            result={"status":"COMPLETE" if code==0 else "DEGRADED","slot":slot,"report":str(target/"report.html"),
                "model_calls":report.get("discovery",{}).get("total_call_count",0),
                "opportunity_counts":report.get("opportunity_summary",{}).get("counts",{}),"exit_code":code}
            result["agent_errors"]=report.get("desk",{}).get("error_count",0)
            if (report.get("discovery",{}).get("search") or {}).get("status")=="FAILED":
                result["agent_errors"]=max(1,result["agent_errors"])
            if report.get("session_scan",{}).get("model_skip_reason"):
                result["status"]="SKIPPED_NO_USABLE_DELAYED_QUOTES"
            atomic_json(state_path,result)
            return result
    except DataError as exc:
        if not entered and str(exc)=="LATEST_POINTER_LOCK_TIMEOUT":return {"status":"SKIPPED_BUSY","model_calls":0}
        raise
