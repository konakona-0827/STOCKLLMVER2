"""Closed-loop paired replay: common quote tape, different endogenous books."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path

from .common import ROOT, ARMS, protocol, read, save, digest, instant, freeze_sources, verify_sources, stamp
from .engine import Book
from .llm import Provider, build_request, decide


def run(cohort_file, output, strategy_file, transport_file, config=None, max_frames=None):
    config = config or protocol()
    cohort = read(cohort_file)
    saved = cohort.pop("cohort_sha256")
    if digest(cohort) != saved:
        raise ValueError("COHORT_HASH_MISMATCH")
    cohort["cohort_sha256"] = saved
    out = Path(output)
    out.mkdir(parents=True,exist_ok=False)
    source = freeze_sources(out,strategy_file)
    save(out/"protocol.json",config)
    save(out/"cohort_manifest.json",dict(cohort_sha256=saved,cohort_file=str(Path(cohort_file).resolve()),frames=len(cohort["frames"]),role=cohort["data_role"]))
    strategy = Path(strategy_file).read_text(encoding="utf-8-sig")
    provider = Provider(transport_file,out/"model_calls",config)
    books = {arm:Book(config) for arm in ARMS}
    frames=cohort["frames"] if max_frames is None else cohort["frames"][:max_frames]
    completed,errors=0,[]
    cache={}
    used_keys=set()
    with ThreadPoolExecutor(max_workers=config["replay_workers"]) as pool:
        for frame_index,frame in enumerate(frames):
            verify_sources(source)
            for book in books.values():
                book.advance(frame["quotes"],frame["at"],cohort["sessions"],strict_depth=False)
            inputs={arm:build_request(frame,books[arm],strategy,arm) for arm in ARMS}
            # Exogenous market inputs and cost settings match. Account state is
            # allowed to diverge only as a result of this arm's prior decisions.
            common=[]
            for arm,(_,request) in inputs.items():
                x=json.loads(request["input"])
                common.append(dict(calendar=x["trading_calendar"],rules=x["paper_simulation_rules"],frame=x["replay_scope"]))
            if common[0]!=common[1]:
                raise AssertionError("EXOGENOUS_OR_EXECUTION_MODEL_MISMATCH")
            # Prefetch only separately frozen future CASE requests, never their
            # information in an earlier case. A forecast is reusable only when
            # its entire request hash equals the actual closed-loop request.
            # Account divergence invalidates forecasts; wasted calls stay billed
            # and preserved in model_calls, not silently removed from cost.
            for arm in ARMS:
                predicted=Book(config,books[arm].state())
                for upcoming in frames[frame_index:frame_index+config["replay_lookahead_frames"]]:
                    predicted.advance(upcoming["quotes"],upcoming["at"],cohort["sessions"],strict_depth=False)
                    supplied=inputs[arm] if upcoming is frame else build_request(upcoming,predicted,strategy,arm)
                    key=(arm,digest(supplied[1]))
                    if key not in cache:
                        cache[key]=pool.submit(decide,*supplied,provider)
            keys={arm:(arm,digest(inputs[arm][1])) for arm in ARMS}
            pending={arm:cache[key] for arm,key in keys.items()}
            used_keys.update(keys.values())
            results={}
            for arm,future in pending.items():
                try:
                    result=future.result()
                    result["status"]="VALID"
                    ready=(instant(frame["at"])+timedelta(seconds=result["elapsed_seconds"])).isoformat()
                    by_symbol={c["symbol"]:c for c in inputs[arm][0]}
                    decisions=[dict(d,asset_type=by_symbol[d["symbol"]].get("asset_type","STOCK")) for d in result["decision"]["decisions"]]
                    books[arm].submit(decisions,ready,frame["id"]+arm)
                    result["simulated_ready_at"]=ready
                except Exception as exc:
                    result=dict(status="ERROR",error_type=type(exc).__name__,error=str(exc).splitlines()[0][:220])
                    errors.append(dict(frame=frame["id"],arm=arm,**result))
                    if any(x in result["error"] for x in ("QUOTA","BUDGET_EXHAUSTED","LOGIN_REQUIRED","MODEL_UNAVAILABLE")):
                        save(out/"interrupted.json",dict(reason=result,completed=completed))
                        raise
                books[arm].decisions.append(dict(frame_id=frame["id"],at=frame["at"],**result))
                results[arm]=result
            completed+=1
            save(out/"cycles"/(frame["id"]+".json"),dict(frame_id=frame["id"],at=frame["at"],exogenous_sha256=digest(frame),results=results,
                 accounts={arm:books[arm].state() for arm in ARMS}))
            # A progress file is a disposable pointer, never a historical record.
            status=dict(completed=completed,total=len(frames),last_frame=frame["id"],errors=len(errors),model_calls=provider.calls,at=stamp())
            temp=out/"progress.tmp"
            temp.write_text(json.dumps(status,indent=2),encoding="utf-8");temp.replace(out/"progress.json")
    verify_sources(source)
    for arm,book in books.items():
        save(out/(arm+".json"),dict(metrics=book.metrics(),state=book.state()))
    usage=dict(input_tokens=sum(r.get("usage",{}).get("input_tokens",0) for r in provider.records),
               output_tokens=sum(r.get("usage",{}).get("output_tokens",0) for r in provider.records),
               cached_input_tokens=sum(r.get("usage",{}).get("input_tokens_details",{}).get("cached_tokens",0) for r in provider.records),
               calls=provider.calls,api_billed_usd=0,billing_source="chatgpt-plan",plan_credit_cost_unknown=True)
    summary=dict(status="COMPLETE",frames=completed,available_frames=len(cohort["frames"]),partial_cohort=max_frames is not None,
                 errors=errors,arms={arm:book.metrics() for arm,book in books.items()},usage=usage,
                 cohort_sha256=saved,strategy_sha256=digest(strategy.encode()),model=config["model"],
                 historical_fill_model=config["historical_fill_model"],real_orders_possible=False,
                 prefetched_requests=len(cache),used_request_hashes=len(used_keys),
                 scope="PRODUCTION_STRATEGY_AND_VALIDATOR_IN_CONTROLLED_PAPER_ACCOUNTS_NOT_BIT_IDENTICAL_ONLINE_WEB_REEXECUTION",
                 warnings=["OCTOBER_IS_DEVELOPMENT", "HISTORICAL_DEPTH_UNAVAILABLE", "NO_TODAYS_NEWS", "CURRENT_STRATEGY_POSTDATES_COHORT", "NO_UNSEEN_OOS_CLAIM"])
    save(out/"summary.json",summary)
    return summary
