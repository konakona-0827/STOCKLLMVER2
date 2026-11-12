from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import SYSTEM,VERSION
from .common import TAIPEI,policy,read,save,stamp,number
from .data import load_dataset,quote_map
from .pipeline import run_cycle
from .engine import build


def refresh_due(at,manifest):
    from datetime import timedelta
    if at.weekday()>=5 or at.strftime("%H:%M")<"08:30":
        return None
    target=at.date() if at.hour>=18 else at.date()-timedelta(days=1)
    through=manifest.get("calendar_verified_through",manifest.get("end",""))
    return target.isoformat() if through<target.isoformat() else None


def record_feed(folder,kind,data):
    from .state import Ledger
    ledger=Ledger(folder)
    try:
        ledger.feed(datetime.now(TAIPEI).isoformat(),kind,data)
    finally:
        ledger.close()


def main(argv=None,coordinator=None,external_stop=None):
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description=f"{SYSTEM} {VERSION} — 獨立研究與既有執行器，使用 Python 與既有 Codex CLI")
    sub=parser.add_subparsers(dest="command",required=True)
    demo=sub.add_parser("demo",help="合成控制案例，不連網、不呼叫模型")
    demo.add_argument("--output",default="local_data/opportunity_flow_demo")
    collect=sub.add_parser("collect",help="從官方免費端點收集上市、上櫃日線")
    collect.add_argument("--date",required=True,help="最新完整交易日，YYYY-MM-DD；必須早於台灣今日")
    collect.add_argument("--sessions",type=int,default=65)
    collect.add_argument("--output",default="local_data/opportunity_flow_market")
    quotes_cmd=sub.add_parser("quotes",help="官方免費整股參考價；不作為零股成交證據")
    quotes_cmd.add_argument("--symbols",nargs="+",required=True,help="TWSE:2330 TPEX:1815，最多40檔")
    quotes_cmd.add_argument("--output",default="local_data/opportunity_flow_quotes.json")
    status_cmd=sub.add_parser("status",help="顯示最新流程狀態與累計模型用量")
    status_cmd.add_argument("--state",default="local_data/opportunity_flow_state")
    doctor_cmd=sub.add_parser('doctor',help='檢查既有帳本、設定與相依套件；不登入券商、不下單')
    doctor_cmd.add_argument('--state-root')
    live_cmd=sub.add_parser('live',help='正式執行器與人工持股交接介面')
    live_cmd.add_argument('--state-root')
    live_cmd.add_argument('--dataset',default='local_data/official_market_20261111')
    live_cmd.add_argument('--state',default='local_data/opportunity_flow_live')
    live_cmd.add_argument('--model')
    live_cmd.add_argument('--read-only',action='store_true')
    live_cmd.add_argument('--headless',action='store_true')
    for name in ("cycle","watch"):
        cmd=sub.add_parser(name,help="執行一輪" if name=="cycle" else "前景監看；Ctrl+C 結束")
        cmd.add_argument("--dataset",required=True)
        cmd.add_argument("--state",default="local_data/opportunity_flow_state")
        cmd.add_argument("--quotes",help="最新零股買賣報價 JSON；不會自動登入券商")
        cmd.add_argument("--events",help="逐股有來源與時間的公司事件檢查 JSON")
        cmd.add_argument("--policy")
        cmd.add_argument("--capital",type=float)
        cmd.add_argument("--model")
        cmd.add_argument("--llm",action="store_true",help="使用現有 Codex ChatGPT 方案額度；無 API 備援")
        cmd.add_argument("--diagnose-blocked",action="store_true",help="讓模型檢閱經濟條件未通過的證據，不能覆寫門檻")
        cmd.add_argument("--review-symbols",nargs="+",help="本次指定的研究子集；省略時遍歷所有合格候選")
        if name=="cycle":
            cmd.add_argument("--as-of",required=True,help="帶時區的判斷時間")
        else:
            cmd.add_argument("--poll-seconds",type=float)
            cmd.add_argument("--public-quotes",action="store_true",help="輪替40檔官方整股參考行情，供研究觸發")
            cmd.add_argument("--refresh-daily",action="store_true",help="08:30補齊日線，18:00更新當日日線")
            cmd.add_argument('--live',action='store_true',help='接上已授權的既有正式執行器')
            cmd.add_argument('--state-root')
            cmd.add_argument('--read-only',action='store_true',help='可連券商查詢，但不送委託或撤單')
    args=parser.parse_args(argv)
    if args.command=='doctor':
        from .live import doctor
        print(json.dumps(doctor(args.state_root),ensure_ascii=False,indent=2));return 0
    if args.command=='live':
        if not args.headless:
            from .control_panel import launch
            return launch(args)
        forwarded=['watch','--dataset',args.dataset,'--state',args.state,'--live','--llm','--public-quotes','--refresh-daily']
        if args.state_root:forwarded+=['--state-root',args.state_root]
        if args.read_only:forwarded+=['--read-only']
        if args.model:forwarded+=['--model',args.model]
        return main(forwarded)
    if args.command=="status":
        from .state import Ledger
        from collections import Counter
        state=read(Path(args.state)/"latest.json")
        print(json.dumps(state["summary"],ensure_ascii=False,indent=2))
        db=Ledger(args.state)
        try:
            usage=Counter();unknown=0;calls=0
            for (raw,) in db.db.execute("SELECT data FROM calls"):
                calls+=1
                u=json.loads(raw).get("usage")
                if u is None:
                    unknown+=1
                else:
                    usage.update({k:v for k,v in u.items() if isinstance(v,(int,float))})
            print(json.dumps(dict(cumulative_calls=calls,usage=dict(usage),usage_unknown_calls=unknown),ensure_ascii=False))
        finally:
            db.close()
        for c in state["candidates"]:
            if c["state"] not in ("NO_SETUP","SCREENED_OUT"):
                print(c["symbol"],c["state"],"；".join(c["reasons"]),json.dumps(c.get("execution",{}),ensure_ascii=False))
                if c.get("review"):
                    for role in ("researcher","challenger"):
                        opinion=c["review"][role]
                        print(" ",role,opinion["verdict"],opinion["thesis"],"重審條件：","；".join(opinion["wait_conditions"]))
        return 0
    if args.command=="quotes":
        from .public_quotes import fetch
        symbols=[tuple(x.split(":")) for x in args.symbols]
        result=fetch(symbols)
        save(args.output,result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    if args.command=="collect":
        from .collector import collect as download
        result=download(args.output,args.date,args.sessions)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    if args.command=="demo":
        from .demo import create
        root=Path(args.output)
        at=create(root/"dataset")
        result=run_cycle(load_dataset(root/"dataset"),at,policy(),root/"state")
        print(json.dumps(dict(summary=result["summary"],output=result["output"]),ensure_ascii=False,indent=2))
        return 0
    p=policy(args.policy)
    if args.capital is not None:
        try:
            p["capital_twd"]=number(args.capital,True)
        except ValueError:
            parser.error("capital 必須為有限正數")
    if args.model:
        p["model"]=args.model
    interval=(args.poll_seconds if args.poll_seconds is not None else p["poll_seconds"]) if args.command=="watch" else None
    if interval is not None and interval<1:
        parser.error("poll-seconds 必須至少 1 秒")
    last_signature=None
    last_numeric=0
    quote_cursor=0
    last_public=0
    executor=ThreadPoolExecutor(max_workers=1) if args.command=="watch" and args.llm else None
    worker=None
    review_cursor=0
    stop_event=external_stop or threading.Event()
    live=coordinator
    if args.command=='watch' and args.live:
        if not args.llm:parser.error('--live 必須啟用 --llm，研究不得跳過')
        if live is None:
            from .live import LiveCoordinator,resolve_account_root
            live=LiveCoordinator(resolve_account_root(args.state_root),args.state,p,stop_event,read_only=args.read_only)
    previous_result=None
    data_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='flow-daily-data') if live is not None else None
    data_worker=None
    data_retry_after=0
    try:
        while not stop_event.is_set():
            if worker is not None and worker.done():
                try:
                    reviewed=worker.result()
                    print(json.dumps(dict(background_review=reviewed["summary"]),ensure_ascii=False),flush=True)
                    if live is not None:live.offer(reviewed,live.dataset or load_dataset(args.dataset))
                except Exception as exc:
                    print(json.dumps(dict(background_review_error=type(exc).__name__+":"+str(exc)[:180])),flush=True)
                worker=None
            at=stamp(args.as_of) if args.command=="cycle" else datetime.now(TAIPEI)
            if live is not None:
                if live.dataset is None:
                    try:live.dataset=load_dataset(args.dataset)
                    except (OSError,ValueError):pass
                live.maintenance(force=True)
            if args.command=="watch" and args.refresh_daily:
                if data_worker is not None and data_worker.done():
                    try:
                        report=data_worker.result()
                        record_feed(args.state,'DAILY_REFRESH',dict(date=report['calendar_verified_through'],http_requests=report['http_requests'],cache_hits=report['cache_hits']))
                    except Exception as exc:
                        data_retry_after=time.monotonic()+60
                        record_feed(args.state,'DAILY_REFRESH_ERROR',dict(error=type(exc).__name__+':'+str(exc)[:180],http_requests=None))
                    finally:data_worker=None
                manifest_path=Path(args.dataset)/"manifest.json"
                try:manifest=read(manifest_path) if manifest_path.exists() else {}
                except (ValueError,OSError):manifest={}
                target=refresh_due(at,manifest)
                if target:
                    from .collector import collect as download
                    try:
                        if data_executor is not None:
                            if data_worker is None and time.monotonic()>=data_retry_after:
                                data_worker=data_executor.submit(download,args.dataset,target,65,stopped=stop_event.is_set)
                            live.maintenance(force=True)
                            stop_event.wait(min(interval,30));continue
                        else:report=download(args.dataset,target,65)
                        record_feed(args.state,"DAILY_REFRESH",dict(date=target,http_requests=report["http_requests"],cache_hits=report["cache_hits"]))
                        print(json.dumps(dict(data_refresh=target,http_requests=report["http_requests"],cache_hits=report["cache_hits"])),flush=True)
                    except Exception as exc:
                        if data_executor is not None:
                            data_worker=None;data_retry_after=time.monotonic()+60;live.maintenance(force=True)
                        record_feed(args.state,"DAILY_REFRESH_ERROR",dict(date=target,error=type(exc).__name__+":"+str(exc)[:180],http_requests=None))
                        print(json.dumps(dict(data_refresh_error=type(exc).__name__+":"+str(exc)[:180])),flush=True)
                        stop_event.wait(min(interval,30)) if live is not None else time.sleep(min(interval,30))
                        continue
                at=datetime.now(TAIPEI)
            if args.command=="watch" and not (at.weekday()<5 and "08:30"<=at.strftime("%H:%M")<="13:30"):
                if live is not None and time.monotonic()-live.last_refresh>=60:
                    try:live.refresh_account()
                    except Exception as exc:print(json.dumps(dict(broker_health_error=type(exc).__name__+':'+str(exc)[:180])),flush=True)
                stop_event.wait(min(interval,30)) if live is not None else time.sleep(min(interval,30))
                continue
            if args.command=="watch" and args.public_quotes and not args.quotes:
                from .public_quotes import fetch
                try:
                    ds=load_dataset(args.dataset)
                except (ValueError,RuntimeError,OSError) as exc:
                    record_feed(args.state,"DATASET_READ_ERROR",dict(error=str(exc)[:180]))
                    if live is not None:live.maintenance(force=True)
                    print(json.dumps(dict(dataset_error=str(exc)[:180])),flush=True)
                    stop_event.wait(min(interval,30)) if live is not None else time.sleep(min(interval,30))
                    continue
                pool=[]
                priority=[]
                for symbol,bars in sorted(ds["grouped"].items()):
                    c=build(symbol,ds,at,p)
                    if c["state"] not in ("SCREENED_OUT","DATA_BLOCKED") and bars:
                        pool.append((bars[-1]["exchange"],symbol))
                        if c["state"] in ("REVIEW_PENDING","ECONOMIC_BLOCKED"):
                            priority.append(c)
                if pool:
                    priority=sorted(priority,key=lambda c:(c["state"]!="REVIEW_PENDING",-c.get("rank_score",0),c["symbol"]))[:16]
                    urgent=[(c["exchange"],c["symbol"]) for c in priority]
                    exploration=[key for key in pool if key not in urgent]
                    rotated=[]
                    if exploration:
                        quote_cursor%=len(exploration)
                        rotated=(exploration+exploration)[quote_cursor:quote_cursor+min(40-len(urgent),len(exploration))]
                        quote_cursor=(quote_cursor+len(rotated))%len(exploration)
                    focus=urgent+rotated
                    try:
                        feed=fetch(focus,last_public)
                        last_public=time.monotonic()
                        public_path=Path(args.state)/"public_quotes.json"
                        old=quote_map(public_path) if public_path.exists() else {}
                        old.update({(q["exchange"],q["symbol"]):q for q in feed["quotes"]})
                        feed["quotes"]=list(old.values())
                        feed["rotation"]=dict(focus_count=len(focus),priority_count=len(urgent),rotation_count=len(rotated),pool_count=len(pool),next_cursor=quote_cursor)
                        save(public_path,feed)
                        record_feed(args.state,"PUBLIC_QUOTES",{k:v for k,v in feed.items() if k!="quotes"})
                        print(json.dumps(dict(public_quote_http_requests=1,focus=len(focus),pool=len(pool),errors=len(feed["errors"]))),flush=True)
                    except Exception as exc:
                        record_feed(args.state,"PUBLIC_QUOTES_ERROR",dict(error=type(exc).__name__+":"+str(exc)[:180],symbols=focus,http_requests=1))
                        print(json.dumps(dict(public_quote_error=type(exc).__name__+":"+str(exc)[:180])),flush=True)
            if args.command=="watch":
                # Network calls may take seconds: validate against time after receipt.
                at=datetime.now(TAIPEI)
            paths=[Path(args.dataset)/"bars.csv",Path(args.dataset)/"manifest.json"]
            active_quote=args.quotes
            if args.command=="watch" and args.public_quotes and not active_quote:
                public_path=Path(args.state)/"public_quotes.json"
                active_quote=str(public_path) if public_path.exists() else None
            paths += [Path(x) for x in (active_quote,args.events) if x]
            signature=tuple((str(x),x.stat().st_mtime_ns if x.exists() else None) for x in paths)
            if args.command=="cycle" or signature!=last_signature or time.monotonic()-last_numeric>=p["numeric_recheck_seconds"]:
                events=read(args.events) if args.events else {}
                try:
                    ds=load_dataset(args.dataset)
                    observed_quotes=quote_map(active_quote)
                    account_context=None
                    if live is not None:
                        p,broker_quotes,event_facts,account_context=live.pulse(ds,previous_result)
                        observed_quotes.update({(q['exchange'],s):q for s,q in broker_quotes.items()})
                        events.update(event_facts)
                        at=datetime.now(TAIPEI)
                    result=run_cycle(ds,at,p,args.state,observed_quotes,events,
                        use_llm=args.llm and args.command=="cycle",diagnose_blocked=args.diagnose_blocked,review_symbols=args.review_symbols,
                        account_context=account_context)
                except (ValueError,RuntimeError,OSError) as exc:
                    if args.command!="watch":
                        raise
                    print(json.dumps(dict(cycle_error=str(exc)[:180])),flush=True)
                    if live is not None:live.maintenance(force=True)
                    stop_event.wait(min(interval,30)) if live is not None else time.sleep(min(interval,30))
                    continue
                if executor is not None and worker is None:
                    pool=sorted([c for c in result["candidates"] if c["state"]=="REVIEW_PENDING" or
                        (args.diagnose_blocked and c["state"]=="ECONOMIC_BLOCKED")],
                        key=lambda c:(-c.get("rank_score",0),c["symbol"]))
                    if args.review_symbols is not None:
                        pool=[c for c in pool if c["symbol"] in args.review_symbols]
                    if pool:
                        review_cursor%=len(pool)
                        chosen=(pool+pool)[review_cursor:review_cursor+min(len(pool),p["model_batch_size"])]
                        review_cursor=(review_cursor+len(chosen))%len(pool)
                        selected={c["symbol"] for c in chosen}
                        def review_job(ds=ds,at=at,observed_quotes=observed_quotes,events=events,selected=selected,
                                       account_context=account_context,p=dict(p)):
                            from .review import CodexRunner
                            return run_cycle(ds,at,p,args.state,observed_quotes,events,
                                runner=CodexRunner(p["model"],p["model_timeout_seconds"],stop_event),use_llm=True,
                                diagnose_blocked=args.diagnose_blocked,review_symbols=selected,publish_latest=False,account_context=account_context)
                        worker=executor.submit(review_job)
                if live is not None:live.offer(result,ds)
                previous_result=result
                print(json.dumps(dict(summary=result["summary"],output=result["output"]),ensure_ascii=False),flush=True)
                last_signature,last_numeric=signature,time.monotonic()
            if args.command=="cycle":
                break
            stop_event.wait(min(interval,30)) if live is not None else time.sleep(min(interval,30))
    except KeyboardInterrupt:
        print("TW Opportunity Flow 已停止。")
    finally:
        stop_event.set()
        if executor is not None:
            executor.shutdown(wait=True,cancel_futures=True)
        if data_executor is not None:data_executor.shutdown(wait=True,cancel_futures=True)
        if live is not None:live.close()
    return 0


if __name__=="__main__":
    raise SystemExit(main())
