"""Funnel and input-shape diagnostics; deliberately does not claim investment alpha."""
from collections import Counter
from datetime import date,timedelta
from pathlib import Path
import statistics
import time

from .common import canonical,digest,read,save
from .engine import build


def recent_funnel(dataset,p,count=15):
    started=time.monotonic()
    sessions=dataset["sessions"][-count:]
    rows=[]
    totals=Counter()
    qualified=[]
    for day in sessions:
        at=(date.fromisoformat(day)+timedelta(days=1)).isoformat()+"T08:30:00+08:00"
        candidates=[build(s,dataset,at,p) for s in sorted(dataset["grouped"])]
        counts=Counter(c["state"] for c in candidates)
        totals.update(counts)
        rows.append(dict(completed_through=day,states=dict(counts)))
        qualified.extend(dict(symbol=c["symbol"],as_of=at,packet_id=c["packet"]["packet_id"],
            routes=c["packet"]["selected_routes"]) for c in candidates if c["state"]=="REVIEW_PENDING")
    return dict(scope="RECONSTRUCTED_NUMERIC_RESEARCH_FUNNEL_NOT_TRADES_OR_RETURNS",
        policy_sha256=digest(p),dataset_manifest_sha256=dataset["manifest_sha256"],
        universe_scope=dataset["manifest"].get("universe"),symbols=len(dataset["grouped"]),
        sessions=len(sessions),observed_stock_days=sum(totals.values()),states=dict(totals),daily=rows,
        numeric_qualified=qualified,llm_calls=0,orders_sent=0,elapsed_seconds=round(time.monotonic()-started,3),
        warnings=["CURRENT_UNIVERSE_SURVIVORSHIP_BIAS","RAW_PRICES_UNVERIFIED_CORPORATE_ACTIONS",
                  "NOT_A_PAIRED_STRATEGY_PERFORMANCE_COMPARISON","DAILY_REFERENCES_NOT_EXECUTABLE_ODDLOT_PRICES"])


def archived_input_shapes(benchmark_path,calendar_path,p,turnover_lookup=None):
    source=read(benchmark_path)
    calendar=read(calendar_path)
    rows=[]
    for event in source["events"]:
        c=event["candidate"]
        if c.get("asset_type")!="STOCK":
            rows.append(dict(symbol=c["symbol"],status="UNSUPPORTED_ASSET_PRESERVED",event_id=event["event_id"]))
            continue
        # Archived benchmark explicitly contains TWSE daily bars, not TPEX.
        if any(b.get("source")!="TWSE_DAILY" for b in c["daily_bars"]):
            raise ValueError("ARCHIVE_EXCHANGE_NOT_VERIFIED")
        bars=[dict(symbol=c["symbol"],exchange="TWSE",date=b["date"],
            **{k:b[k] for k in ("open","high","low","close","volume")},
            turnover_twd=b.get("turnover_twd",b.get("turnover",(turnover_lookup or {}).get((c["symbol"],b["date"])))),source=b["source"],source_sha256=digest(b),
            retrieved_at=None,price_basis="RECONSTRUCTED_RAW_UNADJUSTED") for b in c["daily_bars"]]
        manifest=dict(end=max(calendar["dates"]),point_in_time_certified=False,corporate_actions_verified=False)
        ds=dict(grouped={c["symbol"]:bars},errors={},sessions=calendar["dates"],manifest=manifest,manifest_sha256=digest(manifest))
        new=build(c["symbol"],ds,event["as_of"],p,{**c,"exchange":"TWSE"})
        row=dict(event_id=event["event_id"],symbol=c["symbol"],state=new["state"],reasons=new["reasons"],
            legacy_candidate_characters=len(canonical(c)),compact_packet_characters=new.get("input_characters",0))
        if new.get("packet"):
            row["payload_reduction_fraction"]=1-row["compact_packet_characters"]/row["legacy_candidate_characters"]
        rows.append(row)
    ratios=[r["payload_reduction_fraction"] for r in rows if "payload_reduction_fraction" in r]
    return dict(scope="INPUT_PAYLOAD_SIZE_DIAGNOSTIC_NOT_LLM_ACCURACY_AB_TEST",archived_events=len(rows),
        median_payload_reduction_fraction=statistics.median(ratios) if ratios else None,rows=rows,
        missing_turnover_enriched_from_official_bulk=turnover_lookup is not None,
        note="Character counts exclude CLI system/schema overhead; full source history remains in dataset. No future outcomes were read.")
