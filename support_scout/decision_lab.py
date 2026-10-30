"""Frozen real-price prefix experiments, separate from production and trading.

Full future data is kept only in the scorer. Agents get truncated prefixes and
dated source limitations. Today's history/model are not historical PIT evidence.
"""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime
from html import escape
from pathlib import Path
import re
from statistics import fmean
from uuid import uuid4

from .contracts import DataError, TOKYO, calendar_at, digest, load_series, strict_json_loads, timestamp
from .communication import load_policy as load_communication
from .desk import DeskConfig, run_desk
from .discovery import OpenAISearchProvider, make_search_packet, run_search
from .opportunity import load_policy
from .patterns import load_registry
from .pipeline import run
from .providers import OpenAIProvider
from .settings import ROOT, DEFAULT_SETTINGS, load_settings, openai_config
from .short_eval import evaluate_saved, replay_trade
from .storage import atomic_json

DEFAULT_PLAN = ROOT / "config" / "decision_lab.json"
LIMITATIONS = ["CURRENT_VINTAGE_YAHOO_NOT_HISTORICAL_PIT", "PARAMETERS_AUTHORED_AFTER_REFERENCE_CASES",
    "USER_INTEREST_CURRENT_UNIVERSE_SELECTION_BIAS", "CURRENT_MODEL_MEMORY_NOT_ISOLATED",
    "PRICE_ONLY_NO_RECONSTRUCTED_HISTORICAL_NEWS_OR_FINANCIALS", "SMALL_CORRELATED_SAMPLE_NOT_CALIBRATED_PROBABILITY",
    "NEXT_OPEN_HYPOTHESIS_NOT_BROKER_FILL", "SAME_BAR_STOP_FIRST_INTRADAY_PATH_UNKNOWN",
    "MANUAL_PROPOSAL_NOT_TRADE_APPROVAL", "NUMERIC_SIGNALS_ARE_NOT_AI_PREDICTIONS"]


def load_plan(value=None):
    p=deepcopy(value) if value is not None else strict_json_loads(DEFAULT_PLAN.read_text(encoding="utf-8"))
    fields={"schema_version","study_id","symbols","sessions","max_selected","max_total_calls","numeric_lookback_sessions","profiles"}
    if not isinstance(p,dict) or set(p)!=fields or type(p["schema_version"]) is not int or p["schema_version"]!=1:
        raise DataError("INVALID_DECISION_LAB_PLAN")
    if not isinstance(p["study_id"],str) or not re.fullmatch(r"[a-z0-9_-]{1,90}",p["study_id"]):raise DataError("INVALID_LAB_ID")
    if (not isinstance(p["symbols"],list) or not 1<=len(p["symbols"])<=20
            or any(not isinstance(s,str) or not re.fullmatch(r"[0-9][0-9A-Z]{3}\.T",s) for s in p["symbols"])
            or len(set(p["symbols"]))!=len(p["symbols"])):raise DataError("INVALID_LAB_SYMBOLS")
    if not isinstance(p["sessions"],list) or not 1<=len(p["sessions"])<=30 or len(set(p["sessions"]))!=len(p["sessions"]):
        raise DataError("INVALID_LAB_SESSIONS")
    for s in p["sessions"]:
        if not isinstance(s,str) or date.fromisoformat(s).isoformat()!=s:raise DataError("INVALID_LAB_SESSION")
    for k,lo,hi in (("max_selected",1,4),("max_total_calls",0,120),("numeric_lookback_sessions",10,250)):
        if type(p[k]) is not int or not lo<=p[k]<=hi:raise DataError("INVALID_LAB_BUDGET")
    if not isinstance(p["profiles"],list) or not 1<=len(p["profiles"])<=8:raise DataError("INVALID_LAB_PROFILES")
    seen=set()
    for profile in p["profiles"]:
        if (not isinstance(profile,dict) or set(profile)!={"id","label","short_parameters","minimum_strategy_rr","agent_sessions"}
                or not isinstance(profile["id"],str) or not re.fullmatch(r"[a-z0-9_]{1,40}",profile["id"]) or profile["id"] in seen
                or not isinstance(profile["label"],str) or not profile["label"] or not isinstance(profile["short_parameters"],dict)):
            raise DataError("INVALID_LAB_PROFILE")
        seen.add(profile["id"])
        if profile["id"]=="baseline" and (profile["short_parameters"] or profile["minimum_strategy_rr"] is not None):
            raise DataError("LAB_BASELINE_MUST_KEEP_PRODUCTION_PARAMETERS")
        if profile["agent_sessions"]!="ALL" and (not isinstance(profile["agent_sessions"],list)
                or any(s not in p["sessions"] for s in profile["agent_sessions"])
                or len(set(profile["agent_sessions"]))!=len(profile["agent_sessions"])):raise DataError("INVALID_LAB_PROFILE_SESSIONS")
    return p


def profile_settings(registry, policy, profile):
    registry,policy=load_registry(registry),load_policy(policy)
    for d in registry["definitions"]:
        if d["handler"]=="short_cycle":
            d["parameters"].update(deepcopy(profile["short_parameters"]))
            if profile["id"]!="baseline":d["version"]+="-lab-"+profile["id"]
    if profile["minimum_strategy_rr"] is not None:
        policy["short_horizon"]["minimum_strategy_rr"]=profile["minimum_strategy_rr"]
    if profile["id"]!="baseline":policy["version"]+="-lab-"+profile["id"]
    return load_registry(registry),load_policy(policy)


def build_prefix(source, symbols, session, *, registry, policy, communication, evaluated_at, study_id):
    if source.get("synthetic") is not False or source.get("market")!="JP" or source.get("observation_only") is not True:
        raise DataError("LAB_REAL_JAPAN_OBSERVATION_REQUIRED")
    cutoff=timestamp(session+"T16:00:00+09:00")
    if timestamp(evaluated_at)<max(timestamp(source["as_of"]),cutoff):raise DataError("LAB_EVALUATION_PRECEDES_SOURCE")
    b=deepcopy(source)
    for k in ("discovery","source_universe","portfolio_snapshot","sizing_policy","public_information_policy"):
        b.pop(k,None)
    raw={r["symbol"]:r for r in b["securities"]}
    if not set(symbols)<=set(raw):raise DataError("LAB_SYMBOL_NOT_IN_SOURCE")
    b["securities"]=[]
    for symbol in symbols:
        r=raw[symbol]
        r["bars"]=[x for x in r.get("bars",[]) if x["session"]<=session and timestamp(x["closed_at"])<=cutoff]
        if len(r["bars"])<60 or r["bars"][-1]["session"]!=session:raise DataError("LAB_REQUIRED_PREFIX_UNAVAILABLE:"+symbol+":"+session)
        # Do not expose latest quote metadata, future events, or today's dossier.
        for k in ("yahoo_metadata","source_documents","pattern_context"):r.pop(k,None)
        r["yahoo_events"]=[e for e in r.get("yahoo_events",[]) if e.get("session", "9999")<=session]
        r.update(dossier={},collected_bar_count=len(r["bars"]),point_in_time=False)
        r["observation_blockers"]=list(dict.fromkeys([*r.get("observation_blockers",[]),"RETROSPECTIVE_CURRENT_VINTAGE_RECONSTRUCTION"]))
        b["securities"].append(r)
    b.update(as_of=evaluated_at,market_context={},discovery_required=True,
        pattern_registry=load_registry(registry),opportunity_policy=load_policy(policy),communication_policy=load_communication(communication))
    b["calendar"]["sessions"]=[s for s in b["calendar"]["sessions"] if s<=session]
    b["calendar"]["coverage_through"]=timestamp(evaluated_at).astimezone(TOKYO).date().isoformat()
    b["calendar"]["scope"]="TRUNCATED_RETROSPECTIVE_LAB_NOT_LIVE_CALENDAR"
    # Collection URLs/hashes can describe the entire downloaded series. Keep
    # provenance outside agent inputs; never claim they were fetched in the past.
    b["collection"]={"status":"COMPLETE","expected_latest_session":session,"actual_latest_session":session,
        "mode":"RETROSPECTIVE_PREFIX_ONLY","source_retrieved_at":source["as_of"]}
    b["analysis_context"]={"mode":"RETROSPECTIVE_REAL_PRICE_CASE_RECONSTRUCTION","case_id":study_id,
        "market_data_cutoff":cutoff.isoformat(),"source_retrieved_at":source["as_of"],"analysis_performed_at":evaluated_at,
        "historical_point_in_time_verified":False,"claims_historical_strategy_performance":False,
        "selection_scope":"FROZEN_USER_INTEREST_COHORT_NOT_WHOLE_MARKET",
        "public_information_gaps":["HISTORICAL_FINANCIALS_AND_NEWS_NOT_RECONSTRUCTED"],
        "instruction":"只使用此截點已保留的真實價量；沒有之後行情或歷史財報／新聞，不得憑記憶補未提供的後續結果。條件式試單與formal資格分開，缺口明示；不可預設買進。"}
    universe={"universe_id":study_id+"-"+session,"symbols":list(symbols),"available_at":evaluated_at,
        "scope":"USER_INTEREST_REAL_PREFIX_LAB"}
    b.update(source_universe=universe,universe_version=universe["universe_id"])
    return b


def _panel(bundle, max_selected, providers):
    initial=run(bundle)
    if providers is None:return bundle,initial,{"status":"NUMERIC_ONLY","call_count":0}
    search_provider,researcher,reviewer=providers
    sp=make_search_packet(initial,bundle["source_universe"],max_selected=max_selected)
    result=run_search(sp,search_provider)
    bundle["discovery"]={"mode":"AGENT_SEARCH","search":result,"max_total_calls":1+2*max_selected,
        "total_call_count":result["call_count"],"coverage":{"scope":"REAL_PREFIX_LAB","scanned_count":len(bundle["securities"]),"universe_count":len(bundle["securities"])}}
    report=run(bundle)
    if result["status"]=="COMPLETED":
        report=run_desk(bundle,researcher=researcher,reviewer=reviewer,config=DeskConfig(max_selected,2*max_selected))
        bundle["discovery"]["total_call_count"]+=report["desk"]["call_count"]
        report["discovery"]=deepcopy(bundle["discovery"])
    audit={"status":report.get("desk",{}).get("status",result["status"]),
        "call_count":bundle["discovery"]["total_call_count"],"search_status":result["status"]}
    return bundle,report,audit


def _no_outcome(status,h):
    return {"holding_sessions":h,"status":status,"gross_return":None,"cost_adjusted_return":None,"ambiguous":False}


def score_panel(report, full_source, panel_id, profile_id, session):
    full={r["symbol"]:r for r in full_source["securities"]}
    calendar=calendar_at(full_source,timestamp(full_source["as_of"]))
    cores={p["core"]["symbol"]:p["core"] for p in report["all_packets"]}
    rows=[]
    selected={x["symbol"] for x in report.get("discovery",{}).get("search",{}).get("selection",[])}
    policy=report["opportunity_policy"]
    for item in report["securities"]:
        symbol=item["symbol"];o=item.get("opportunity_decision",{});core=cores.get(symbol,{})
        series=load_series(full[symbol],timestamp(full_source["as_of"]),calendar,allow_observation=True,market="JP")
        future=[b for b in series.bars if b.session>session]
        plans=core.get("opportunity_reference_plans",{})
        # Fix the comparator by policy order, never select it using future gain.
        primary=next((plans[pid] for pid in policy["pattern_ids"] if pid in plans and plans[pid] and plans[pid].get("target_basis")=="PRICE_MOVE"
            and plans[pid]["selection_reward_risk"]>=plans[pid]["minimum_reward_risk"] and plans[pid]["risk_fraction"]<=policy["max_risk_fraction"]),None)
        for h in (3,5):
            comparison=replay_trade(primary,future[:h],h) if primary else _no_outcome("NO_QUALIFIED_SHORT_PLAN",h)
            plan=o.get("reference_plan")
            if o.get("action")!="PROBE_BUY_REVIEW":outcome=_no_outcome("NO_AI_BUY_PROPOSAL",h)
            elif not plan or plan.get("target_basis")!="PRICE_MOVE":outcome=_no_outcome("LEGACY_PLAN_REPRICE_REQUIRED_NOT_SCORED_AS_2P",h)
            else:outcome=replay_trade(plan,future[:h],h)
            rows.append({"panel_id":panel_id,"profile_id":profile_id,"session":session,"month":session[:7],"symbol":symbol,
                "latest_close":item.get("technical",{}).get("latest_close"),"selected_by_search":symbol in selected,
                "research_status":o.get("research_status"),"action":o.get("action"),"pattern_id":o.get("pattern_id"),
                "buy_type_label":o.get("buy_type_label"),"agent_views":o.get("agent_views",{}),"reason_codes":o.get("reason_codes",[]),
                "plan":plan,"outcome":outcome,"primary_comparator":comparison,
                "primary_comparator_channel":primary["channel"] if primary else None})
    return rows


def summarize(rows):
    result=[]
    groups=defaultdict(list)
    for r in rows:
        h=r["outcome"]["holding_sessions"]
        groups[(r["profile_id"],"ALL",h)].append(r)
        groups[(r["profile_id"],r["month"],h)].append(r)
    for (profile,month,h),items in sorted(groups.items()):
        buys=[r for r in items if r["action"]=="PROBE_BUY_REVIEW"]
        filled=[r for r in buys if r["outcome"]["gross_return"] is not None]
        targets=sum(r["outcome"]["status"]=="TARGET" for r in filled)
        result.append({"profile_id":profile,"month":month,"holding_sessions":h,"cases":len(items),
            "search_selected":sum(r["selected_by_search"] for r in items),"buy_proposals":len(buys),
            "buy_proposal_rate":len(buys)/len(items),"resolved_simulated_entries":len(filled),"targets":targets,
            "ambiguous_stop_first":sum(r["outcome"]["ambiguous"] for r in filled),
            "target_rate_stop_first":targets/len(filled) if filled else None,
            "mean_price_return":fmean(r["outcome"]["gross_return"] for r in filled) if filled else None,
            "outcome_counts":dict(Counter(r["outcome"]["status"] for r in buys)),
            "missed_frozen_primary_targets":sum(r["action"]!="PROBE_BUY_REVIEW" and r["primary_comparator"]["status"]=="TARGET" for r in items)})
    return result


def direction_diagnostics(rows, source):
    """Additional price-touch diagnostic, explicitly ignoring entry/stop gates.

    A hit after a strategy stop is a price touch, never a successful trade. A
    non-hit with an incomplete forward window stays unknown, not a failure.
    """
    raw={x["symbol"]:x for x in source["securities"]}
    groups=defaultdict(list)
    for r in rows:
        h=r["outcome"]["holding_sessions"]
        groups[(r["profile_id"],"ALL",h)].append(r)
        groups[(r["profile_id"],r["month"],h)].append(r)
    summaries=[]
    for (profile,month,h),items in sorted(groups.items()):
        reviewed=sum(x.get("research_status")=="REVIEWED" for x in items)
        buys=[x for x in items if x["action"]=="PROBE_BUY_REVIEW"]
        known,hits,unknown,cohort_known,cohort_hits=0,0,0,0,0
        for x in items:
            future=[b for b in raw[x["symbol"]]["bars"] if b["session"]>x["session"]][:h]
            hit=bool(future and max(b["high"] for b in future)>=future[0]["open"]*1.02)
            if hit or len(future)==h:cohort_known+=1;cohort_hits+=hit
            if x["action"]!="PROBE_BUY_REVIEW":continue
            if hit or len(future)==h:known+=1;hits+=hit
            else:unknown+=1
        rule=[x["primary_comparator"] for x in items if x["primary_comparator"].get("gross_return") is not None]
        rule_hits=sum(x["status"]=="TARGET" for x in rule)
        summaries.append({"profile_id":profile,"month":month,"holding_sessions":h,"cases":len(items),
            "reviewed_cases":reviewed,"buy_proposals":len(buys),"buy_rate_among_reviewed":len(buys)/reviewed if reviewed else None,
            "observable_price_windows":known,"unknown_price_windows":unknown,"price_touch_2p":hits,
            "price_touch_rate_ignore_execution":hits/known if known else None,
            "cohort_observable_price_windows":cohort_known,"cohort_price_touch_2p":cohort_hits,
            "cohort_price_touch_rate_ignore_execution":cohort_hits/cohort_known if cohort_known else None,
            "rule_primary_resolved_entries":len(rule),"rule_primary_targets":rule_hits,
            "rule_primary_target_rate":rule_hits/len(rule) if rule else None,
            "basis":"PRICE_TOUCH_FROM_NEXT_OPEN_IGNORE_ENTRY_CEILING_TRIGGER_AND_STOP_NOT_TRADE_SUCCESS"})
    return summaries


def paired_summaries(rows, plan):
    result=[]
    for profile in plan["profiles"]:
        sessions=profile["agent_sessions"]
        if profile["id"]=="baseline" or not isinstance(sessions,list) or not sessions:continue
        subset=[x for x in rows if x["profile_id"] in {"baseline",profile["id"]} and x["session"] in sessions]
        result.extend({"comparison":profile["id"],"sessions":sessions,**s} for s in summarize(subset) if s["month"]=="ALL")
    return result


def symbol_summaries(rows):
    result=[]
    for symbol in sorted({x["symbol"] for x in rows}):
        subset=[x for x in rows if x["symbol"]==symbol]
        result.extend({"symbol":symbol,**s} for s in summarize(subset) if s["month"]=="ALL")
    return result


def run_lab(source, *, plan=None, output_root=None, agents=False, settings_path=DEFAULT_SETTINGS,
            registry=None, policy=None, communication=None, providers=None, progress=None):
    plan=load_plan(plan);registry=load_registry(registry);policy=load_policy(policy);communication=load_communication(communication)
    evaluated=datetime.now(TOKYO).isoformat()
    definitions=[]
    for profile in plan["profiles"]:
        r,p=profile_settings(registry,policy,profile)
        sessions=plan["sessions"] if profile["agent_sessions"]=="ALL" else profile["agent_sessions"]
        definitions.extend((profile,session,r,p) for session in sessions)
    if agents and len(definitions)*(1+2*plan["max_selected"])>plan["max_total_calls"]:
        raise DataError("LAB_FIXED_DESIGN_EXCEEDS_CALL_BUDGET")
    # Validate every prefix before any paid request; never silently drop failures.
    tasks=[]
    for profile,session,r,p in definitions:
        b=build_prefix(source,plan["symbols"],session,registry=r,policy=p,communication=communication,evaluated_at=evaluated,study_id=plan["study_id"])
        tasks.append((profile["id"]+"_d"+session.replace("-",""),profile["id"],session,b))
    if agents and providers is None:
        ai=openai_config(load_settings(Path(settings_path)))
        if not ai["ready"]:raise DataError("LAB_OPENAI_CONFIGURATION_REQUIRED")
        providers=(OpenAISearchProvider(ai["model"],max_output_tokens=2400),
            OpenAIProvider(ai["model"],max_output_tokens=2200),OpenAIProvider(ai["reviewer_model"],max_output_tokens=2200))
    if not agents:providers=None
    target=Path(output_root or ROOT/"output"/"decision-lab")/(datetime.now(TOKYO).strftime("%Y%m%dT%H%M%S")+"-"+uuid4().hex[:10])
    target.mkdir(parents=True,exist_ok=False)
    frozen={"version":"decision-lab-v1","plan":plan,"source_digest":digest(source),"registry":registry,"policy":policy,
        "communication":communication,"evaluated_at":evaluated,"execution_enabled":False,"limitations":LIMITATIONS}
    atomic_json(target/"frozen.json",frozen);atomic_json(target/"source.json",source)
    panels=[]
    def execute(task):
        panel_id,profile,session,b=task
        atomic_json(target/(panel_id+"_input.json"),b)
        b,report,audit=_panel(b,plan["max_selected"],providers)
        atomic_json(target/(panel_id+"_input.json"),b)
        atomic_json(target/(panel_id+"_report.json"),report)
        return {"id":panel_id,"profile_id":profile,"session":session,"audit":audit,
            "input_digest":digest(b),"report_digest":digest(report)},report
    with ThreadPoolExecutor(max_workers=2 if agents else 1) as pool:
        futures=[pool.submit(execute,t) for t in tasks]
        for future in as_completed(futures):
            panel,report=future.result();panels.append((panel,report))
            if progress:progress({"completed":len(panels),"total":len(tasks),"panel":panel["id"],**panel["audit"]})
    panels.sort(key=lambda x:x[0]["id"])
    # No outcomes enter an agent task. Score only after every decision is saved.
    rows=[]
    for panel,report in panels:rows.extend(score_panel(report,source,panel["id"],panel["profile_id"],panel["session"]))
    numeric=[]
    cohort=deepcopy(source);cohort["securities"]=[x for x in source["securities"] if x["symbol"] in plan["symbols"]]
    for profile in plan["profiles"]:
        r,p=profile_settings(registry,policy,profile)
        value=evaluate_saved(cohort,registry=r,policy=p,lookback_sessions=plan["numeric_lookback_sessions"])
        atomic_json(target/("numeric_"+profile["id"]+".json"),value)
        numeric.extend({"profile_id":profile["id"],"label":profile["label"],**s} for s in value["summary"])
    events=[e for _,r in panels for e in r.get("desk",{}).get("events",[])]
    searches=[r.get("discovery",{}).get("search",{}) for _,r in panels]
    usages=[e.get("usage") for e in events]+[s.get("usage") for s in searches if s]
    total_calls=sum(p["audit"]["call_count"] for p,_ in panels)
    if total_calls>plan["max_total_calls"]:raise DataError("LAB_CALL_BUDGET_EXCEEDED")
    result={"version":"decision-lab-v1","frozen_digest":digest(frozen),"source_digest":digest(source),"plan":plan,
        "agents":agents,"panels":[p for p,_ in panels],"rows":rows,"summary":summarize(rows) if agents else [],
        "numeric_sensitivity":numeric,"model_call_count":total_calls,"max_total_calls":plan["max_total_calls"],
        "direction_diagnostics":direction_diagnostics(rows,source) if agents else [],"paired_comparisons":paired_summaries(rows,plan) if agents else [],
        "symbol_summary":symbol_summaries(rows) if agents else [],
        "usage_complete":all(isinstance(u,dict) and "total_tokens" in u for u in usages),
        "recorded_total_tokens":sum(u.get("total_tokens",0) for u in usages if isinstance(u,dict)),
        "errors":[{"panel":p["id"],"status":p["audit"]["status"]} for p,_ in panels if p["audit"]["status"] in {"DEGRADED","FAILED"}],
        "empirical_edge_validated":False,"point_in_time_verified":False,"execution_enabled":False,"limitations":LIMITATIONS}
    atomic_json(target/"comparison.json",result)
    (target/"comparison.html").write_text(render_lab(result),encoding="utf-8")
    return result,target


def replay_lab(directory):
    d=Path(directory)
    read=lambda name:strict_json_loads((d/name).read_text(encoding="utf-8"))
    frozen,source,result=read("frozen.json"),read("source.json"),read("comparison.json")
    if digest(source)!=frozen["source_digest"] or digest(frozen)!=result["frozen_digest"]:raise DataError("LAB_FROZEN_INTEGRITY_FAILED")
    profiles={p["id"]:p for p in frozen["plan"]["profiles"]}
    rows=[]
    for panel in result["panels"]:
        pid=panel["id"]
        if not re.fullmatch(r"[a-z0-9_]+",pid):raise DataError("LAB_INVALID_ARTIFACT_ID")
        b,saved=read(pid+"_input.json"),read(pid+"_report.json")
        if digest(b)!=panel["input_digest"] or digest(saved)!=panel["report_digest"]:raise DataError("LAB_PANEL_INTEGRITY_FAILED")
        reg,policy=profile_settings(frozen["registry"],frozen["policy"],profiles[panel["profile_id"]])
        expected=build_prefix(source,frozen["plan"]["symbols"],panel["session"],registry=reg,policy=policy,
            communication=frozen["communication"],evaluated_at=frozen["evaluated_at"],study_id=frozen["plan"]["study_id"])
        base=deepcopy(b);base.pop("discovery",None)
        if digest(base)!=digest(expected):raise DataError("LAB_PREFIX_REPLAY_MISMATCH")
        # The model reviews are saved outputs, not fresh model requests.
        r=run(b,reviews=saved.get("reviews",[]))
        if r["snapshot_id"]!=saved["snapshot_id"] or [s.get("opportunity_decision") for s in r["securities"]]!=[s.get("opportunity_decision") for s in saved["securities"]]:
            raise DataError("LAB_DECISION_REPLAY_MISMATCH")
        rows.extend(score_panel(r,source,pid,panel["profile_id"],panel["session"]))
    if rows!=result["rows"] or (result["agents"] and summarize(rows)!=result["summary"]):raise DataError("LAB_OUTCOME_REPLAY_MISMATCH")
    if "direction_diagnostics" in result and result["direction_diagnostics"]!=(direction_diagnostics(rows,source) if result["agents"] else []):
        raise DataError("LAB_DIRECTION_REPLAY_MISMATCH")
    if "paired_comparisons" in result and result["paired_comparisons"]!=(paired_summaries(rows,result["plan"]) if result["agents"] else []):
        raise DataError("LAB_PAIRED_REPLAY_MISMATCH")
    if "symbol_summary" in result and result["symbol_summary"]!=(symbol_summaries(rows) if result["agents"] else []):
        raise DataError("LAB_SYMBOL_REPLAY_MISMATCH")
    return {"status":"MATCH","panels":len(result["panels"]),"model_calls":0,"network_calls":0,"point_in_time_verified":False}


def render_lab(r):
    pct=lambda x:"—" if x is None else f"{x:.1%}"
    outcomes={"TARGET":"先達+2%","STOP":"先停損","STOP_GAP":"跳空停損","AMBIGUOUS_STOP_FIRST":"同K先後不明／保守停損",
        "TIME_EXIT":"到期退出","NO_AI_BUY_PROPOSAL":"未提出試單","NO_FUTURE_DATA":"無後續資料",
        "NOT_FILLED_GAP_ABOVE_ENTRY_BAND":"跳空超限未成交","TRIGGER_NOT_REACHED":"未觸發",
        "INTRADAY_TRIGGER_TOUCHED_FILL_UNVERIFIED":"盤中觸及／成交未驗證","INCOMPLETE_HORIZON":"期限未完整",
        "LEGACY_PLAN_REPRICE_REQUIRED_NOT_SCORED_AS_2P":"H-002須重報價／不混入2%結果"}
    summaries=''.join(f'<tr><td>{escape(s["profile_id"])}</td><td>{escape(s["month"])}</td><td>{s["holding_sessions"]}</td>'
        f'<td>{s["cases"]}</td><td>{s["search_selected"]}</td><td>{s["buy_proposals"]} ({pct(s["buy_proposal_rate"])})</td>'
        f'<td>{s["targets"]}/{s["resolved_simulated_entries"]}</td><td>{pct(s["target_rate_stop_first"])}</td><td>{s["ambiguous_stop_first"]}</td>'
        f'<td>{pct(s["mean_price_return"])}</td><td>{s["missed_frozen_primary_targets"]}</td></tr>' for s in r['summary'])
    directions=''.join(f'<tr><td>{escape(x["profile_id"])}</td><td>{x["holding_sessions"]}</td><td>{x["cases"]}</td>'
        f'<td>{x["reviewed_cases"]}</td><td>{x["buy_proposals"]} ({pct(x["buy_rate_among_reviewed"])})</td>'
        f'<td>{x["price_touch_2p"]}/{x["observable_price_windows"]} ({pct(x["price_touch_rate_ignore_execution"])})</td>'
        f'<td>{x["cohort_price_touch_2p"]}/{x["cohort_observable_price_windows"]} ({pct(x["cohort_price_touch_rate_ignore_execution"])})</td>'
        f'<td>{x["rule_primary_targets"]}/{x["rule_primary_resolved_entries"]} ({pct(x["rule_primary_target_rate"])})</td></tr>'
        for x in r.get('direction_diagnostics',[]) if x['month']=='ALL')
    paired=''.join(f'<tr><td>{escape(x["profile_id"])}</td><td>{escape("、".join(x["sessions"]))}</td><td>{x["holding_sessions"]}</td>'
        f'<td>{x["buy_proposals"]}</td><td>{x["targets"]}/{x["resolved_simulated_entries"]}</td><td>{pct(x["mean_price_return"])}</td></tr>' for x in r.get('paired_comparisons',[]))
    symbols=''.join(f'<tr><td>{escape(x["symbol"])}</td><td>{x["cases"]}</td><td>{x["search_selected"]}</td>'
        f'<td>{x["buy_proposals"]}</td><td>{x["targets"]}/{x["resolved_simulated_entries"]}</td><td>{x["missed_frozen_primary_targets"]}</td></tr>'
        for x in r.get('symbol_summary',[]) if x['profile_id']=='baseline' and x['holding_sessions']==3)
    cases=[]
    for x in r['rows']:
        if x['outcome']['holding_sessions']!=3:continue
        opinions='；'.join(role+'：'+v['reason'] for role,v in x['agent_views'].items())
        p=x.get('plan') or {}
        cases.append(f'<tr><td>{escape(x["profile_id"])}</td><td>{escape(x["session"])}</td><td>{escape(x["symbol"])}</td>'
            f'<td>{x["latest_close"]}</td><td>{"有" if x["selected_by_search"] else "未選"}</td><td>{escape(x["action"] or "未完成")}</td>'
            f'<td>{escape(x["buy_type_label"] or "—")}</td><td>{escape(outcomes.get(x["outcome"]["status"],x["outcome"]["status"]))}</td>'
            f'<td>{pct(x["outcome"]["gross_return"])}</td><td><details><summary>價位與兩方理由</summary>'
            f'計畫進場／失效／目標：{p.get("entry_reference","—")}／{p.get("invalidation_reference","—")}／{p.get("target_reference","—")}<br>'
            f'假設次日進場／退出：{x["outcome"].get("entry_reference") or "—"}／{x["outcome"].get("exit_reference") or "—"}<br>{escape(opinions or "未完成雙方評估，不能當成已否決。")}</details></td></tr>')
    sensitivity=''.join(f'<tr><td>{escape(x["label"])}</td><td>{escape(x["pattern_id"])}</td><td>{x["holding_sessions"]}</td>'
        f'<td>{x["signal_count"]}</td><td>{x["completed_scenarios"]}</td><td>{pct(x["target_hit_rate_stop_first"])}</td><td>{pct(x["mean_price_return"])}</td></tr>' for x in r['numeric_sensitivity'])
    return ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>真實行情：多時段三Agent決策實驗</title><style>body{font:15px/1.7 system-ui;max-width:1600px;margin:auto;padding:24px;color:#203449}aside{background:#fff1ce;padding:18px}table{border-collapse:collapse;width:100%}td,th{padding:10px;border:1px solid #ddd;vertical-align:top}.scroll{overflow:auto}details{min-width:250px}small{color:#567}</style>'
        '<h1>真實行情：多時段三Agent決策實驗</h1><aside>Agent只看截點前的真實價量，不提供後續結果；全部決策保存後才評分。'
        '歷史財報／新聞沒有完整重建，因此是價格型態實驗，不能直接冒充正式觀察流程的買入率。今日版Yahoo、今日模型與看過案例後的規則也不等於歷史當時預測。'
        '下列達標比例是小樣本描述，未校準為未來勝率。2%為股價目標，不計券商成本作選股門檻。</aside>'
        f'<p>股票：{escape("、".join(r["plan"]["symbols"]))}；模型請求 {r["model_call_count"]}/{r["max_total_calls"]}；已記錄token {r["recorded_total_tokens"]:,}；用量完整：{r["usage_complete"]}。</p>'
        '<p>買入須SEARCH交接、有效型態、風報與風險合格、研究與反證對同一型態皆支持；任一否決或缺交接均不買。次日開盤跳空超限不成交；盤中僅觸發不假設成交；同K碰停損與目標保守算停損。H-002非2%計畫另列，不捏造其成交。</p>'
        '<h2>價格訊號診斷：不是交易成功率</h2><p>提案之後，以次日開盤為比較基準，觀察區間最高價曾否漲2%。此表刻意忽略進場上限、是否觸發及中途停損；即使先停損後上漲也會算價格觸及，因此不可拿來取代下表的交易情境結果。未研究者不等於反證否決。</p>'
        '<div class="scroll"><table><tr><th>參數</th><th>交易日</th><th>股票×日期樣本</th><th>完成雙方研究</th><th>提案／已研究比例</th><th>AI提案：價格曾+2%</th><th>同組全部股票：價格曾+2%</th><th>固定規則：先達標／進場</th></tr>'+directions+'</table></div>'
        '<p>全部股票的價格觸及率供方向基準比較；固定規則對照仍檢查進場與停損，且採事前優先型態。兩者涵蓋交易不同，不能只拿百分比大小就宣稱AI增加收益。</p>'
        '<h2>實際三Agent：分時段結果</h2><p>分母為已結束的假設進場；買入提案、未成交、未成熟資料與H-002另列。3日與5日不可相加。漏失僅依事前固定優先型態，不事後挑最會漲的一種。</p>'
        '<div class="scroll"><table><tr><th>參數</th><th>月份</th><th>交易日</th><th>樣本</th><th>SEARCH選中</th><th>試單提案／比例</th><th>達標／已結束進場</th><th>保守達標比例</th><th>其中先後歧義</th><th>平均價格報酬</th><th>未提案而固定型態達標</th></tr>'
        +summaries+'</table></div><h2>各標的基準結果（3日）</h2><p>0/0表示沒有可確認的模擬進場，不能換算成0%勝率；未選入深入研究也不是模型已判斷不會上漲。</p>'
        '<div class="scroll"><table><tr><th>標的</th><th>截點數</th><th>SEARCH選中</th><th>試單提案</th><th>達標／可確認進場</th><th>未提案而固定型態達標</th></tr>'+symbols+'</table></div>'
        '<h2>逐筆決策與3日結果</h2><div class="scroll"><table><tr><th>參數</th><th>截點</th><th>標的</th><th>收盤</th><th>尋標</th><th>結論</th><th>類型</th><th>後續情境</th><th>價格報酬</th><th>依據</th></tr>'
        +''.join(cases)+'</table></div><h2>同日期參數對照</h2><p>只比較相同兩個日期；模型可能選不同股票，不能把差異全歸因於停損參數。</p><div class="scroll"><table><tr><th>參數</th><th>日期</th><th>交易日</th><th>試單提案</th><th>達標／已結束進場</th><th>平均價格報酬</th></tr>'
        +paired+'</table></div><h2>規則參數敏感度：不是模型預測</h2><p>相同真實OHLCV、最近80個訊號交易日。重疊型態數值情境，沒有AI篩選或資金排程，不能稱為AI勝率；不自動把表中最好參數改成正式設定。</p>'
        '<div class="scroll"><table><tr><th>參數</th><th>型態</th><th>交易日</th><th>訊號</th><th>已結束情境</th><th>先達標比例</th><th>平均價格報酬</th></tr>'
        +sensitivity+'</table></div><p>完整JSON保存所有未成交原因、來源digest、凍結參數、封包、模型用量與反證，支援零網路重播。未建立訂單、持倉、正式帳本或歷史郵件。</p></html>')
