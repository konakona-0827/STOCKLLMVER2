"""Persistent paired PAPER worker. Owns a quote-only STA, never an order object."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import time

from .common import ROOT,ARMS,Market,read,save,digest,stamp,protocol,instant,freeze_sources,verify_sources
from .engine import Book
from .capital_feed import CapitalFeed
from .journal import Journal,checkpoint
from .live import live_frame
from .llm import Provider,build_request,decide


def pointer(path,value):
    temp=Path(path).with_suffix(".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    temp.replace(path)


def next_session(sources,at):
    market=Market();clock=instant(at).astimezone(market.tz)
    for day in sources["sessions"]:
        first=market.effective("oddlot_sessions",day)["first_match"]
        start=datetime.fromisoformat(day+"T"+first).replace(tzinfo=market.tz)
        if start>clock:return start.isoformat()
    return None


def error_text(exc):
    lines=str(exc).splitlines()
    return (lines[0] if lines else type(exc).__name__)[:200]


def advance_books(books,frame,sessions,journal):
    """Persist causal fill evidence and both books in one transaction."""
    before={arm:b.state() for arm,b in books.items()};events=[]
    quotes={q['symbol']:q for q in frame['quotes']}
    try:
        for arm,book in books.items():
            count=len(book.fills)
            book.advance(frame['quotes'],frame['at'],sessions,strict_depth=True)
            for fill in book.fills[count:]:
                events.append(dict(kind='fill',payload=dict(arm=arm,fill=fill,frame_id=frame['id'],observation=quotes[fill['symbol']])))
        if events:journal.append_many(events+[dict(kind='checkpoint',payload=checkpoint(books))])
    except BaseException:
        for arm,state in before.items():books[arm]=Book(state['config'],state)
        raise
    return len(events)


def commit_decision(books,arm,decisions,result,ready,slot,journal):
    before=books[arm].state();book=books[arm]
    try:
        first=len(book.orders);book.submit(decisions,ready,slot+arm)
        journal.append_many([dict(kind='decision_result',payload=dict(arm=arm,slot=slot,ready_at=ready,result=result)),
            dict(kind='orders',payload=dict(arm=arm,slot=slot,orders=book.orders[first:])),
            dict(kind='checkpoint',payload=checkpoint(books))])
    except BaseException:
        books[arm]=Book(before['config'],before)
        raise


def finish_session(books,day,at,journal):
    if instant(at).astimezone(Market().tz).strftime('%H:%M')<Market().rules['regular_close']:
        raise ValueError('SESSION_NOT_CLOSED')
    if journal.contains('closed-'+day):return False
    before={arm:b.state() for arm,b in books.items()}
    try:
        for book in books.values():book.close_session(day,at)
        journal.append_many([dict(kind='checkpoint',payload=checkpoint(books)),
            dict(kind='session_closed',payload=dict(day=day,at=at),event_id='closed-'+day)])
    except BaseException:
        for arm,state in before.items():books[arm]=Book(state['config'],state)
        raise
    return True


def drain_jobs(futures,job_inputs,slot,books,journal,control_path,version,config,sessions,at):
    for arm,future in list(futures.items()):
        if not future.done():continue
        try:
            result=future.result()
            input_asof=json.loads(job_inputs[arm][1]['input'])['replay_scope']['as_of']
            if not read(control_path).get('active'):raise ValueError('CONTROL_PAUSED_BEFORE_PAPER_SUBMISSION')
            if not Market().session_open(at,sessions):raise ValueError('SESSION_CLOSED_BEFORE_PAPER_SUBMISSION')
            if not 0<=(instant(at)-instant(input_asof)).total_seconds()<=config['max_quote_age_seconds']:
                raise ValueError('MODEL_DECISION_EXPIRED_BEFORE_PAPER_SUBMISSION')
            by_symbol={c['symbol']:c for c in job_inputs[arm][0]}
            decisions=[dict(d,asset_type=by_symbol[d['symbol']].get('asset_type','STOCK')) for d in result['decision']['decisions']]
            commit_decision(books,arm,decisions,result,at,slot,journal)
            review=result.get('meta',{}).get('pretrade_review',{})
            if review.get('status')=='ERROR':
                journal.append('review_error',dict(arm=arm,slot=slot,error=review.get('error','UNKNOWN')))
                if any(x in str(review) for x in ('QUOTA','BUDGET_EXHAUSTED','LOGIN_REQUIRED','MODEL_UNAVAILABLE')):
                    pointer(control_path,dict(version=version,mode='PAPER_ONLY',active=False,stop=False,reason='MODEL_UNAVAILABLE_NO_FALLBACK'))
        except Exception as exc:
            journal.append('decision_error',dict(arm=arm,slot=slot,error_type=type(exc).__name__,error=error_text(exc)))
            if any(x in str(exc) for x in ('QUOTA','BUDGET_EXHAUSTED','LOGIN_REQUIRED','MODEL_UNAVAILABLE')):
                pointer(control_path,dict(version=version,mode='PAPER_ONLY',active=False,stop=False,reason='MODEL_UNAVAILABLE_NO_FALLBACK'))
        del futures[arm]


def run(output,strategy_file,transport_file,sources_file,credentials_source,*,once=False,protocol_file=None):
    out=Path(output).resolve()
    if not out.is_relative_to(ROOT/"research-runs"):
        raise ValueError("ISOLATED_FORWARD_DIRECTORY_REQUIRED")
    out.mkdir(parents=True,exist_ok=True)
    from state_lock import StateLease
    lease=StateLease(out,"llm-paper-forward").acquire()
    config=protocol(protocol_file);strategy=Path(strategy_file).read_text(encoding="utf-8-sig")
    journal=Journal(out/"paper.sqlite3");journal.verify()
    if not (out/"source_manifest.json").exists():source=freeze_sources(out,strategy_file)
    else:source=read(out/"source_manifest.json")
    verify_sources(source)
    version=digest(dict(protocol=config,source=source,strategy_sha256=digest(strategy.encode())))[:20]
    registration=journal.latest("registration")
    if registration and registration["version"]!=version:raise ValueError("NEW_SOURCE_REQUIRES_NEW_FORWARD_ACCOUNT")
    if not registration:
        journal.append("registration",dict(version=version,mode="PAPER_ONLY",protocol=config,source=source,strategy_sha256=digest(strategy.encode()),
                                          arms=list(ARMS),capital_per_arm=config["initial_cash_twd"],live_order_interface=None))
        pointer(out/"control.json",dict(version=version,mode="PAPER_ONLY",active=True,stop=False))
    saved=journal.latest("checkpoint")
    books={arm:Book(config,(saved or {}).get(arm)) for arm in ARMS}
    if not registration:journal.append('checkpoint',checkpoint(books))
    sources=read(sources_file)
    if set(sources["instruments"])!=set(read(ROOT/"config/universe_tw.json")["symbols"]):
        raise ValueError("MASTER_UNIVERSE_MISMATCH")
    journal.append("started",dict(version=version,pid=os.getpid(),sources_file=str(Path(sources_file).resolve()),sources_sha256=digest(Path(sources_file).read_bytes()),
                                  credentials_source_is_read_only=True,no_credentials_copied=True))
    market=Market();feed=None;futures={};last_snapshot=0;last_status=0;last_tape=0;last_checkpoint=0
    history={};last_state=None;provider=None;provider_day=None;pool=ThreadPoolExecutor(max_workers=2)
    report_attempts={};last_report_error=None
    universe=read(ROOT/"config/universe_tw.json")
    def status(state,**extra):
        nonlocal last_state,last_status
        changed=state!=last_state
        if changed:
            journal.append("runtime_status",dict(status=state,**extra));last_state=state
        if changed or time.monotonic()-last_status>=10 or once:
            pointer(out/"status.json",dict(at=stamp(),version=version,pid=os.getpid(),mode="PAPER_ONLY",status=state,
                next_session=next_session(sources,stamp()),source_file=str(Path(sources_file).resolve()),
                raw_quote_connection=feed is not None,active_model_jobs=len(futures),live_order_capability=False,
                accounts={arm:dict(cash_twd=str(b.cash),positions={s:b.quantity(s) for s in b.positions},pending=len(b.pending),metrics=b.metrics()) for arm,b in books.items()},
                last_report_error=last_report_error,**extra))
            last_status=time.monotonic()
    try:
        while True:
            verify_sources(source)
            control=read(out/"control.json")
            if control.get("version")!=version or control.get("mode")!="PAPER_ONLY":raise ValueError("CONTROL_MODE_OR_VERSION_MISMATCH")
            if control.get("stop"):
                status("STOPPED_BY_CONTROL");break
            now=datetime.now(market.tz);at=now.isoformat();day=now.date().isoformat()
            # Late/failed calls are recorded even when the market is closed.
            if futures:drain_jobs(futures,job_inputs,job_slot,books,journal,out/'control.json',version,config,sources['sessions'],at)
            if day in sources['sessions'] and now.strftime('%H:%M')>=market.rules['regular_close'] and not futures and journal.contains('opened-'+day):
                finish_session(books,day,at,journal)
                if not journal.contains('report-'+day) and report_attempts.get(day,0)<3:
                    report_attempts[day]=report_attempts.get(day,0)+1
                    try:
                        from .reporting import generate
                        report_dir=out/'reports'/day/now.strftime('%H%M%S%f')
                        report=generate(out,day,report_dir,at=at)
                        journal.append('comparison_report',dict(day=day,path=str(report_dir),sha256=digest(report)),event_id='report-'+day)
                        pointer(out/'latest-comparison.json',dict(day=day,phase=report['phase'],report_dir=str(report_dir),
                            evidence_gate=report['live_test_evidence_gate'],sha256=digest(report)))
                        last_report_error=None
                    except Exception as exc:
                        last_report_error=error_text(exc)
                        journal.append('comparison_report_error',dict(day=day,error=last_report_error,attempt=report_attempts[day]))
            control=read(out/'control.json')
            # A pause stops new decisions, but not observation of pending orders.
            if not control.get("active") and not any(b.pending or b.positions for b in books.values()):
                status("PAUSED_BY_CONTROL")
                if once:break
                time.sleep(10);continue
            in_day=day in sources["sessions"] and now.year==sources["calendar_valid_year"]
            connect_window=in_day and "08:50"<=now.strftime("%H:%M")<"13:35"
            if not connect_window:
                if feed is not None:feed.close();feed=None
                status("ARMED_WAITING_NEXT_SESSION")
                if once:break
                time.sleep(10);continue
            if not journal.contains('opened-'+day):
                journal.append('session_started',dict(day=day,at=at,accounts=checkpoint(books)),event_id='opened-'+day)
            if day!=sources["as_of"]:
                status("REFRESHING_CURRENT_OFFICIAL_DAILY_SOURCES",expected_as_of=day)
                from .data_sources import refresh
                source_out=out/"sources"/(now.strftime("%Y%m%dT%H%M%S%f"))
                try:
                    updated=refresh(source_out,ROOT/"config/universe_tw.json",now.date())
                    sources_file=source_out/"sources.json";sources=updated
                    journal.append("official_sources_updated",dict(path=str(sources_file),sha256=digest(sources_file.read_bytes()),as_of=day))
                except Exception as exc:
                    journal.append("official_sources_failed",dict(day=day,error_type=type(exc).__name__,error=error_text(exc)))
                    status("WAITING_FOR_CURRENT_OFFICIAL_DAILY_SOURCES",expected_as_of=day)
                    if once:break
                    time.sleep(60);continue
            if feed is None:
                # Credentials are read only by this quote collector's process;
                # existing CLI transport filters them out of model children.
                from config import load_env
                load_env(Path(credentials_source))
                feed=CapitalFeed(out/"capital",universe["symbols"],universe.get("etf_symbols",[]))
                try:
                    feed.connect();feed.subscribe_depth(universe["symbols"][:10])
                    journal.append("quote_connected",dict(day=day,api_codes=feed.api_codes,objects=["SKCenterLib","SKReplyLib","SKQuoteLib"]))
                except Exception as exc:
                    feed.close();feed=None
                    journal.append("quote_error",dict(day=day,error_type=type(exc).__name__,error=error_text(exc)))
                    status("QUOTE_CONNECTION_FAILED_NO_MODEL_CALLS")
                    if once:break
                    time.sleep(60);continue
            feed.pump(.1)
            if feed.stopped():
                journal.append('quote_disconnected',dict(day=day,at=at))
                feed.close();feed=None
                status('QUOTE_RECONNECT_WAIT')
                if once:break
                time.sleep(10);continue
            if time.monotonic()-last_snapshot<1:continue
            last_snapshot=time.monotonic()
            snapshot=feed.snapshot();at=datetime.now(market.tz).isoformat()
            if not market.session_open(at,sources["sessions"]):
                status("CONNECTED_WAITING_FOR_ODDLOT_MATCH")
                if once:break
                continue
            frame=live_frame(snapshot,sources,history,at)
            advance_books(books,frame,sources['sessions'],journal)
            for q in frame["quotes"]:
                if q["quote_status"]!="LIVE":continue
                rows=history.setdefault(q["symbol"],[])
                if not rows or rows[-1]["exchange_time"]!=q["quote_timestamp"]:
                    rows.append(dict(exchange_time=q["quote_timestamp"],price=q["last_price"],received_at=q["received_at"],evidence_id=q["evidence_id"]))
                    del rows[:-15]
            minutes=now.hour*60+now.minute
            slot_number=(minutes-(9*60+5))//config["decision_interval_minutes"]
            slot=f"{day}-slot-{slot_number}"
            if read(out/'control.json').get('active') and not futures and slot_number>=0 and not journal.contains("claim-"+slot) and any(q["quote_status"]=="LIVE" for q in frame["quotes"]):
                journal.append("decision_slot",dict(slot=slot,at=at),event_id="claim-"+slot)
                holders=sorted(set().union(*(set(b.positions) for b in books.values())))
                ranked=[c["symbol"] for c in sorted(frame["candidates"],key=lambda c: c.get("quant_rank") or 9999)]
                wanted=list(dict.fromkeys(holders+ranked))[:10]
                feed.subscribe_depth(wanted)
                # Capture a separate observation after any monitor rotation.
                feed.pump(.2);snapshot=feed.snapshot();at=datetime.now(market.tz).isoformat()
                frame=live_frame(snapshot,sources,history,at)
                if provider_day!=day:
                    limits=dict(config,maximum_model_calls_replay=config["maximum_model_calls_per_forward_day"])
                    provider=Provider(transport_file,out/"model_calls"/day,limits)
                    existing=list((out/"model_calls"/day).glob("call-*"))
                    provider.calls=max((int(p.name.split("-")[-1]) for p in existing),default=0)
                    provider_day=day
                job_slot=slot
                job_inputs={arm:build_request(frame,books[arm],strategy,arm) for arm in ARMS}
                journal.append("decision_input",dict(slot=slot,frame=frame,requests={arm:x[1] for arm,x in job_inputs.items()}))
                futures={arm:pool.submit(decide,*job_inputs[arm],provider) for arm in ARMS}
                journal.append("checkpoint",checkpoint(books))
            if time.monotonic()-last_tape>=30:
                quote_file=out/"quotes"/(datetime.now(market.tz).strftime("%Y%m%dT%H%M%S%f")+".json")
                save(quote_file,snapshot)
                keys=('symbol','source','quote_status','quote_timestamp','received_at','depth_received_at','depth_status','is_trial')
                journal.append_many([dict(kind='market_quality',payload=dict(day=day,at=at,
                    quotes=[{k:q.get(k) for k in keys} for q in frame['quotes']],snapshot_file=str(quote_file),snapshot_sha256=digest(quote_file.read_bytes()))),
                    dict(kind='equity_mark',payload={arm:b.equity[-1] for arm,b in books.items()})])
                last_tape=time.monotonic()
            if time.monotonic()-last_checkpoint>=300:
                journal.append("checkpoint",checkpoint(books));last_checkpoint=time.monotonic()
                for b in books.values():b.equity=b.equity[-1:]
            status("PAPER_FORWARD_RUNNING" if control.get('active') else 'PAUSED_NEW_DECISIONS_OBSERVING_ORDERS',quotes=len(frame["quotes"]),fresh_quotes=sum(q["quote_status"]=="LIVE" for q in frame["quotes"]),model_calls_today=provider.calls if provider else 0)
            if once:break
    except BaseException as exc:
        status("STOPPED_ERROR",error_type=type(exc).__name__,reason=error_text(exc))
        raise
    finally:
        if feed is not None:feed.close()
        pool.shutdown(wait=True,cancel_futures=True)
        if futures:journal.append("unapplied_model_jobs_at_shutdown",dict(arms=list(futures),no_orders_submitted_from_these_jobs=True))
        journal.append("checkpoint",checkpoint(books))
        journal.append("worker_exited",dict(version=version,pid=os.getpid()))
        lease.close()
    return read(out/"status.json")
