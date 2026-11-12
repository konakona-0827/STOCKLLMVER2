from __future__ import annotations

from collections import Counter
from datetime import timedelta
import time

from .common import digest, save, stamp
from .engine import build,economics
from .review import CodexRunner, combine, validate
from .state import Ledger


def execution_readiness(candidate, reviewed_packet, route_id, at, event, p):
    if route_id is None:
        return dict(state="NO_APPROVED_RESEARCH_ROUTE",missing=[])
    route = next(r for r in reviewed_packet["evidence"][2]["facts"] if r["route_id"] == route_id)
    missing = []
    q = candidate.get("quote")
    if q and (stamp(at)-stamp(q["quote_timestamp"])).total_seconds()>p["quote_max_age_seconds"]:
        missing.append("QUOTE_STALE_AFTER_REVIEW")
    if not q:
        missing.append(candidate.get("quote_problem") or "ODDLOT_QUOTE_MISSING")
    limits = reviewed_packet["evidence"][3]["facts"]
    if not limits["corporate_actions_verified"]:
        missing.append("CORPORATE_ACTIONS_NOT_VERIFIED")
    if (not event or event.get("status") != "CHECKED_CLEAR" or not event.get("source_url")
            or not event.get("checked_at")):
        missing.append("COMPANY_EVENTS_AND_TRADING_STATUS_NOT_CHECKED")
    else:
        try:
            age=(stamp(at)-stamp(event["checked_at"])).total_seconds()
            if not 0 <= age <= 86400:
                missing.append("EVENT_CHECK_STALE_OR_FUTURE")
        except (ValueError,TypeError):
            missing.append("EVENT_CHECK_TIME_INVALID")
    if q:
        if q["bid"] <= route["stop"]["price"]:
            return dict(state="INVALIDATED",missing=missing)
        if q["ask"] >= route["target"]["price"]:
            return dict(state="TARGET_ALREADY_REACHED",missing=missing)
        # Freeze the reviewed entry/stop/target/quantity. Quote noise must not
        # chase the ask or silently substitute today's newly generated route.
        e=economics(route["entry"],route["stop"]["price"],route["target"]["price"],
            route["economics"]["quantity"],q["ask"]-q["bid"],p)
        if (e["invested_twd"]>p["capital_twd"]*p["allocation_fraction"]
                or e["modeled_loss_twd"]>p["capital_twd"]*p["maximum_position_risk_fraction"]
                or e["net_return"]<p["minimum_net_return"] or e["reward_risk"] is None
                or e["reward_risk"]<p["minimum_reward_risk"] or e["net_profit_twd"]<p["minimum_net_profit_twd"]):
            missing.append("FROZEN_PLAN_ECONOMICS_CHANGED_REQUIRES_REVIEW")
        if missing:
            return dict(state="EXECUTION_DATA_NEEDED",missing=missing)
        if q["ask"] > route["entry"]:
            return dict(state="WAITING_FOR_PLANNED_ENTRY",missing=[])
    if missing:
        return dict(state="EXECUTION_DATA_NEEDED",missing=missing)
    return dict(state="PAPER_REVIEW_READY",missing=[],orders_permitted=False)


def run_cycle(dataset,at,p,state_folder,quotes=None,events=None,runner=None,use_llm=False,diagnose_blocked=False,review_symbols=None,publish_latest=True,account_context=None):
    started=time.monotonic()
    at=stamp(at)
    quotes,events=quotes or {},events or {}
    ledger=Ledger(state_folder)
    candidates=[]
    try:
        for symbol in sorted(dataset["grouped"]):
            bars=dataset["grouped"][symbol]
            exchange=bars[-1]["exchange"] if bars else None
            event=events.get(symbol)
            c=build(symbol,dataset,at,p,quotes.get((exchange,symbol)),event,(account_context or {}).get(symbol))
            candidates.append(c)
        queue=sorted([c for c in candidates if c["state"] == "REVIEW_PENDING" or
            (diagnose_blocked and c["state"] == "ECONOMIC_BLOCKED")],
            key=lambda c:(-c.get("rank_score",0),c["symbol"]))
        if review_symbols is not None:
            queue=[c for c in queue if c["symbol"] in review_symbols]
        now=at.timestamp()
        service_key="service:"+p["model"]
        service_error=ledger.lookup(service_key,now,p)
        pending=[]
        cached=0
        for c in queue:
            key=ledger.key(c,p["model"])
            c["review_key"]=key
            saved=ledger.lookup(key,now,p)
            if saved:
                if saved["status"] == "VALID":
                    c["review"]=saved["data"]
                    c["review_source"]="CACHED_FROZEN_THESIS"
                    cached+=1
                else:
                    c["state"]="REVIEW_IN_FLIGHT" if saved["status"] == "IN_FLIGHT" else "REVIEW_ERROR_BACKOFF"
                    c["reasons"]=[saved["data"].get("error",saved["status"])]
            elif service_error:
                c.update(state="REVIEW_SERVICE_BACKOFF",reasons=[service_error["data"]["error"]])
            elif use_llm:
                pending.append(c)
        calls=[]
        if use_llm and pending:
            runner=runner or CodexRunner(p["model"],p["model_timeout_seconds"])
            size=p["model_batch_size"]
            for start in range(0,len(pending),size):
                batch=[c for c in pending[start:start+size] if ledger.claim(c["review_key"],now,p)]
                if not batch:
                    continue
                packets=[c["packet"] for c in batch]
                answers={}
                error=None
                for role in ("researcher","challenger"):
                    began=time.monotonic()
                    result=None
                    try:
                        result=runner(packets,role)
                        answers[role]=validate(result["answer"],packets,role)
                        call=dict(role=role,status="VALID",symbols=[c["symbol"] for c in batch],**result)
                    except Exception as exc:
                        error=type(exc).__name__+":"+str(exc).split("\n")[0][:180]
                        call=dict(role=role,status="ERROR",error=error,usage=None,
                            elapsed_seconds=round(time.monotonic()-began,3),symbols=[c["symbol"] for c in batch])
                        if isinstance(result,dict):
                            call.update(usage=result.get("usage"),raw_result=result)
                    ledger.call(at.isoformat(),role,call["status"],call)
                    calls.append(call)
                    if error:
                        break
                for c in batch:
                    key=c["review_key"]
                    if error:
                        ledger.finish(key,"ERROR",now,dict(error=error))
                        c.update(state="REVIEW_ERROR",reasons=[error])
                    else:
                        pid=c["packet"]["packet_id"]
                        result=dict(packet=c["packet"],researcher=answers["researcher"][pid],challenger=answers["challenger"][pid],
                            reviewed_at=(at+timedelta(seconds=time.monotonic()-started)).isoformat())
                        ledger.finish(key,"VALID",now,result)
                        c.update(review=result,review_source="NEW_MATERIAL_FACTS")
                if error:
                    ledger.finish(service_key,"ERROR",now,dict(error=error))
                    # Transport/quota problems are a service failure, not a market WAIT.
                    for c in pending[start+size:]:
                        c.update(state="REVIEW_DEFERRED_SERVICE_ERROR",reasons=[error])
                    break
        for c in queue:
            if not c.get("review"):
                continue
            r=c["review"]
            state,route=combine(r["packet"],r["researcher"],r["challenger"])
            c.update(state=state,selected_route_id=route,
                execution=execution_readiness(c,r["packet"],route,
                    at+timedelta(seconds=time.monotonic()-started),events.get(c["symbol"]),p))
        states=Counter(c["state"] for c in candidates)
        blockers=Counter(reason for c in candidates for reason in c["reasons"])
        usage=Counter()
        unknown=0
        for call in calls:
            if call.get("usage") is None:
                unknown+=1
            else:
                usage.update({k:v for k,v in call["usage"].items() if isinstance(v,(int,float))})
        summary=dict(system="TW Opportunity Flow",as_of=at.isoformat(),policy_version=p["version"],
            policy_sha256=digest(p),dataset_manifest_sha256=dataset["manifest_sha256"],
            universe_scope=dataset["manifest"].get("universe","DECLARED_DATASET_NOT_WHOLE_MARKET"),
            review_scope="ALL_ELIGIBLE_CANDIDATES" if review_symbols is None else sorted(review_symbols),
            symbols_observed=len(candidates),states=dict(states),blockers=dict(blockers),
            llm_calls=len(calls),cached_theses=cached,usage=dict(usage),usage_unknown_calls=unknown,
            additional_api_billed_usd=0,plan_usage_consumed=bool(calls),orders_sent=0,
            decision_scope="LIVE_ACCOUNT_RESEARCH_SEPARATE_EXECUTION_HANDOFF" if account_context is not None else "SHADOW_RESEARCH_NO_BROKER_OR_ACCOUNT_ACCESS",
            elapsed_seconds=round(time.monotonic()-started,3))
        cycle_id=ledger.cycle(at.isoformat(),summary)
        root=ledger.folder/"cycles"/f"{cycle_id:06d}"
        save(root/"summary.json",summary)
        archived=[]
        for c in candidates:
            row={k:v for k,v in c.items() if k not in ("packet","review")}
            if c.get("packet"):
                row["packet_id"]=c["packet"]["packet_id"]
                packet_file=ledger.folder/"packets"/(row["packet_id"]+".json")
                if not packet_file.exists():
                    save(packet_file,c["packet"])
            if c.get("review"):
                reviewed=c["review"]["packet"]
                row["review"]={k:v for k,v in c["review"].items() if k!="packet"}
                row["reviewed_packet_id"]=reviewed["packet_id"]
                packet_file=ledger.folder/"packets"/(reviewed["packet_id"]+".json")
                if not packet_file.exists():
                    save(packet_file,reviewed)
            archived.append(row)
        save(root/"candidates.json",archived)
        save(root/"calls.json",calls)
        if publish_latest:
            save(ledger.folder/"latest.json",dict(summary=summary,candidates=candidates))
        return dict(summary=summary,candidates=candidates,output=str(root))
    finally:
        ledger.close()
