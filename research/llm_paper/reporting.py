"""Read-only, as-of journal audit and paired forward comparison artifacts."""
from collections import Counter
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import statistics

from .common import ARMS,Market,instant,decimal,read,save,digest,stamp,output_path
from .engine import Book


def distribution(values):
    values=sorted(values)
    return dict(n=len(values),mean=statistics.fmean(values),p95=values[max(0,(len(values)*95+99)//100-1)],maximum=values[-1]) if values else None


def generate(paper_dir,day,output,*,at=None):
    paper=Path(paper_dir).resolve();out=output_path(output);at=at or stamp();market=Market()
    with closing(sqlite3.connect((paper/'paper.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        rows=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq')]
    events=[];issues=[];previous='0'*64
    def check(condition,code):
        if not condition:issues.append(code)
    for row in rows:
        payload=json.loads(row['payload'])
        check(row['previous_sha256']==previous and row['sha256']==digest(dict(id=row['id'],at=row['at'],kind=row['kind'],payload=payload,previous_sha256=previous)),
              'HASH_CHAIN:'+str(row['seq']))
        previous=row['sha256'];events.append(dict(row,payload=payload))
    registration=next(e['payload'] for e in events if e['kind']=='registration')
    config=registration['protocol'];check(config['mode']=='PAPER_ONLY' and registration.get('live_order_interface') is None,'PAPER_ONLY_REGISTRATION')
    source_manifest=paper/'source_manifest.json'
    if source_manifest.exists():
        for name,expected in read(source_manifest).items():
            code_file=paper.parents[1]/name;frozen_file=paper/'source'/name
            check(code_file.is_file() and frozen_file.is_file() and digest(code_file.read_bytes())==digest(frozen_file.read_bytes())==expected,'SOURCE_VERSION:'+name)
    # Historical day reports stop at that day's durable close, even if called
    # after a later session has already changed the account.
    boundaries=[e['seq'] for e in events if e['kind']=='session_closed' and e['payload']['day']==day]
    boundaries += [e['seq']-1 for e in events if e['kind']=='session_started' and e['payload']['day']>day]
    if boundaries:events=[e for e in events if e['seq']<=min(boundaries)]
    receipts=[e['payload'] for e in events if e['kind'] in ('started','official_sources_updated')]
    sessions=None
    if receipts:
        receipt=receipts[-1];source_file=Path(receipt.get('sources_file',receipt.get('path','')))
        if source_file.is_file():
            check(digest(source_file.read_bytes())==receipt.get('sources_sha256',receipt.get('sha256')),'OFFICIAL_SOURCE_HASH')
            sessions=read(source_file)['sessions']
    states={arm:Book(config).state() for arm in ARMS};account_checks=0
    for event in events:
        if event['kind']!='checkpoint':continue
        states=event['payload']
        for arm,state in states.items():
            fills=state['fills'];unsettled=sum((decimal(r['amount']) for r in state['receivables']),Decimal(0))
            bought=sum((decimal(f['amount']) for f in fills if f['side']=='BUY'),Decimal(0))
            sold=sum((decimal(f['amount']) for f in fills if f['side']=='SELL'),Decimal(0))
            check(decimal(state['cash'])==decimal(config['initial_cash_twd'])-bought+sold-unsettled,'CASH_IDENTITY:'+arm)
            if sessions and state.get('current_at'):
                current_day=instant(state['current_at']).astimezone(market.tz).date().isoformat()
                expected=[]
                for fill in fills:
                    if fill['side']!='SELL':continue
                    index=sessions.index(fill['at'][:10])+config['settlement_sessions']
                    check(index<len(sessions),'SETTLEMENT_CALENDAR_COVERAGE')
                    if index<len(sessions) and sessions[index]>current_day:expected.append((fill['amount'],sessions[index]))
                check(Counter(expected)==Counter((r['amount'],r['release_date']) for r in state['receivables']),'T_PLUS_TWO:'+arm)
            qty=Counter()
            for f in fills:qty[f['symbol']]+=f['qty']*(1 if f['side']=='BUY' else -1)
            held={s:sum(lot['qty'] for lot in p['lots']) for s,p in state['positions'].items()}
            check(all(q>=0 for q in qty.values()) and {s:q for s,q in qty.items() if q}==held,'INVENTORY_IDENTITY:'+arm)
            basis=sum((decimal(lot['basis']) for p in state['positions'].values() for lot in p['lots']),Decimal(0))
            realized=sum((decimal(f['amount'])-decimal(f['basis']) for f in fills if f['side']=='SELL'),Decimal(0))
            check(abs(decimal(state['cash'])+unsettled+basis-decimal(config['initial_cash_twd'])-realized)<decimal('.000001'),'PNL_IDENTITY:'+arm)
            book=Book(config,state)
            check(0<=book.reserved()<=book.cash,'RESERVATION:'+arm)
            account_checks+=1
    starts=[e['payload'] for e in events if e['kind']=='session_started' and e['payload']['day']==day]
    closed=any(e['kind']=='session_closed' and e['payload']['day']==day for e in events)
    inputs=[e['payload'] for e in events if e['kind']=='decision_input' and e['payload']['slot'].startswith(day)]
    qualities=[e['payload'] for e in events if e['kind']=='market_quality' and e['payload']['day']==day]
    results=[e['payload'] for e in events if e['kind']=='decision_result' and e['payload']['slot'].startswith(day)]
    errors=[dict(kind=e['kind'],**e['payload']) for e in events if e['kind'] in ('decision_error','review_error') and e['payload'].get('slot','').startswith(day)]
    # Check every arm, not just whether either arm completed a slot.
    unresolved=[dict(slot=x['slot'],arm=arm) for x in inputs for arm in ARMS
                if not any(y.get('slot')==x['slot'] and y.get('arm')==arm for y in results+errors)]
    for case in inputs:
        common=[];arm_market=[]
        for arm in ARMS:
            req=case['requests'][arm];payload=json.loads(req['input'])
            check(req['model']==config['model'] and not req.get('tools'),'CONTROLLED_MODEL_TOOLS:'+arm)
            common.append(dict(model=req['model'],schema=req['text'],calendar=payload['trading_calendar'],rules=payload['paper_simulation_rules'],frame=payload['replay_scope']))
            keys=('last_price','bid','ask','open','high','low','volume','quote_status','quote_timestamp','asset_type','daily_bars','metrics','strategy_evidence')
            arm_market.append({c['symbol']:{key:c.get(key) for key in keys} for c in payload['analysis_set']})
            for c in payload['analysis_set']:
                check(not any(b['date']>=day for b in c.get('daily_bars',[])),'FUTURE_DAILY_BAR:'+c['symbol'])
        check(common[0]==common[1],'PAIRED_EXOGENOUS_RULES:'+case['slot'])
        for symbol in arm_market[0].keys() & arm_market[1].keys():
            check(arm_market[0][symbol]==arm_market[1][symbol],'PAIRED_MARKET_INPUT:'+case['slot']+symbol)
    for quality in qualities:
        for q in quality['quotes']:
            if q['quote_status']!='LIVE':continue
            clock=instant(quality['at'])
            check(0<=(clock-instant(q['quote_timestamp'])).total_seconds()<=config['max_quote_age_seconds'],'LIVE_TRADE_FRESHNESS:'+q['symbol'])
            check(0<=(clock-instant(q['received_at'])).total_seconds()<=config['max_quote_age_seconds'],'LIVE_RECEIPT_FRESHNESS:'+q['symbol'])
            check(q['is_trial'] is False and q['source']=='CAPITAL_SKCOM','LIVE_QUOTE_SOURCE:'+q['symbol'])
    fills=[e['payload'] for e in events if e['kind']=='fill' and e['payload']['fill']['at'][:10]==day]
    for row in fills:
        f,q=row['fill'],row.get('observation')
        check(q is not None,'MISSING_FILL_OBSERVATION')
        if not q:continue
        state=states[row['arm']];order=next(o for o in state['orders'] if o['id']==f['order_id'])
        check(instant(order['ready_at'])<instant(f['quote_at'])<=instant(f['at']),'FILL_CAUSALITY:'+f['order_id'])
        check(q['depth_received_at']==f['quote_at'] and q['evidence_id']==f['evidence_id'],'FILL_EVIDENCE_LINK:'+f['order_id'])
        check(q['quote_status']=='LIVE' and not q['is_trial'] and q['depth_status']=='ODDLOT_CALLBACK_SHARES','FILL_SOURCE:'+f['order_id'])
        check(0<=(instant(f['at'])-instant(q['quote_timestamp'])).total_seconds()<=config['max_quote_age_seconds'],'FILL_TRADE_AGE:'+f['order_id'])
        check(0<=(instant(f['at'])-instant(q['depth_received_at'])).total_seconds()<=config['max_depth_age_seconds'],'FILL_DEPTH_AGE:'+f['order_id'])
        check(decimal(q['limit_down'])<=decimal(f['price'])<=decimal(q['limit_up']),'FILL_PRICE_LIMITS:'+f['order_id'])
        check(decimal(f['price'])<=decimal(order['limit']) if f['side']=='BUY' else decimal(f['price'])>=decimal(order['limit']),'FILL_LIMIT:'+f['order_id'])
    selected={arm:set() for arm in ARMS};call_records={};all_records=[];unknown=[]
    for folder in sorted((paper/'model_calls'/day).glob('call-*')):
        request=read(folder/'request.json');key=digest(request)
        if (folder/'result.json').exists():record=read(folder/'result.json')
        else:record=dict(status='IN_FLIGHT_OR_INTERRUPTED',model=request['model'])
        check(record.get('request_sha256',key)==key,'MODEL_REQUEST_HASH:'+folder.name)
        call_records[key]=record
        all_records.append(record)
        if 'usage' not in record:unknown.append(folder.name)
    for case in inputs:
        for arm,req in case['requests'].items():selected[arm].add(digest(req))
    for result in results:
        review=result['result'].get('meta',{}).get('pretrade_review',{})
        if review.get('request'):selected[result['arm']].add(digest(review['request']))
    if config.get('data_role')!='SYNTHETIC_INTEGRATION_NOT_FORWARD':
        for case in inputs:
            for arm,req in case['requests'].items():
                if closed or any(r['slot']==case['slot'] and r['arm']==arm for r in results):
                    check(digest(req) in call_records,'MISSING_MODEL_REQUEST:'+case['slot']+arm)
        for result in results:
            case=next(x for x in inputs if x['slot']==result['slot'])
            key=digest(case['requests'][result['arm']]);record=call_records.get(key,{})
            response=result['result'].get('meta',{}).get('api',{})
            check(record.get('response_id') is not None and response.get('response_id')==record['response_id'],'MODEL_RESPONSE_LINK:'+result['slot']+result['arm'])
    arms={}
    for arm in ARMS:
        state=states[arm];book=Book(config,state);records=[call_records[k] for k in selected[arm] if k in call_records]
        start=(starts[0]['accounts'][arm] if starts else Book(config).state())
        start_nav=start['equity'][-1]['liquidation_equity_twd'] if start['equity'] else float(decimal(config['initial_cash_twd']))
        end_nav=state['equity'][-1]['liquidation_equity_twd'] if state['equity'] else float(book.cash)
        valid=[r['result']['elapsed_seconds'] for r in results if r['arm']==arm]
        arms[arm]=dict(cumulative_metrics=book.metrics(),session_net_return=end_nav/start_nav-1 if inputs else None,
            cash_twd=str(book.cash),liquidation_equity_twd=end_nav,positions={s:book.quantity(s) for s in book.positions},
            pending_orders=len(book.pending),decision_results=len(valid),decision_errors=sum(e.get('arm')==arm for e in errors),
            fills=sum(f['arm']==arm for f in fills),known_model_calls=len(records),
            known_input_tokens=sum(r.get('usage',{}).get('input_tokens',0) for r in records),
            known_output_tokens=sum(r.get('usage',{}).get('output_tokens',0) for r in records),
            unknown_usage_calls=sum('usage' not in r for r in records),latency_seconds=distribution(valid))
    phase='CLOSED' if closed else 'IN_PROGRESS' if starts or inputs else 'NOT_STARTED'
    synthetic=config.get('data_role')=='SYNTHETIC_INTEGRATION_NOT_FORWARD'
    gate='SYNTHETIC_ONLY' if synthetic else 'DEGRADED_ERRORS' if issues else 'NOT_READY_NO_FIRST_DAY_EVIDENCE' if phase=='NOT_STARTED' else 'AWAITING_CLOSE' if not closed else 'DEGRADED_ERRORS' if errors or unresolved else 'NO_VALID_LLM_DECISIONS' if any(not a['decision_results'] for a in arms.values()) else 'NO_FILL_EVIDENCE' if any(not a['fills'] for a in arms.values()) else 'PAPER_ENGINEERING_CHECKS_PASS_REAL_ORDER_PATH_UNVERIFIED'
    report=dict(as_of=at,day=day,phase=phase,data_role='SYNTHETIC_INTEGRATION_NOT_FORWARD' if synthetic else 'FORWARD_PAPER',
        version=registration['version'],model=config['model'],protocol=config,comparison_arms=arms,
        relative_to_formal_baseline=None if not inputs else arms['formal_with_skill']['session_net_return']-arms['formal_baseline']['session_net_return'],
        market_index_relative_return=None,probability_calibration=None,probability_calibration_reason='LLM_CONFIDENCE_IS_NOT_WIN_PROBABILITY',
        paired_slots=len(inputs),unresolved_decisions=unresolved,errors=errors,audit_issues=sorted(set(issues)),
        checkpoint_account_checks=account_checks,hash_chain_events=len(events),hash_chain_head=events[-1]['sha256'],
        verified_journal_events=len(rows),verified_journal_head=previous,settlement_calendar_verified=sessions is not None,
        connection_events={kind:sum(e['kind']==kind and e['payload'].get('day',instant(e['at']).astimezone(market.tz).date().isoformat())==day for e in events)
                           for kind in ('quote_connected','quote_error','quote_disconnected','official_sources_failed')},
        quote_quality_samples=len(qualities),fresh_quote_observations=sum(q['quote_status']=='LIVE' for s in qualities for q in s['quotes']),
        fresh_depth_observations=sum(q['quote_status']=='LIVE' and q.get('depth_status')=='ODDLOT_CALLBACK_SHARES' and q.get('depth_received_at') is not None
            and 0<=(instant(s['at'])-instant(q['depth_received_at'])).total_seconds()<=config['max_depth_age_seconds'] for s in qualities for q in s['quotes']),
        trade_age_seconds=distribution([(instant(s['at'])-instant(q['quote_timestamp'])).total_seconds() for s in qualities for q in s['quotes'] if q['quote_status']=='LIVE']),
        last_quality_at=qualities[-1]['at'] if qualities else None,
        fills_with_causal_evidence=sum('observation' in f for f in fills),model_calls=len(all_records),unknown_usage_call_folders=unknown,
        all_calls_known_input_tokens=sum(r.get('usage',{}).get('input_tokens',0) for r in all_records),
        all_calls_known_output_tokens=sum(r.get('usage',{}).get('output_tokens',0) for r in all_records),
        usage_is_lower_bound=bool(unknown),api_billed_usd=0,plan_credit_cost_unknown=True,
        live_test_evidence_gate=gate,real_orders_possible=False,real_order_authorized_by_report=False)
    out.mkdir(parents=True,exist_ok=False);save(out/'comparison.json',report)
    lines=[f'# {day} Paper 前向比較',f'截至：{at}',f'階段：**{phase}**；驗證狀態：**{gate}**。',
        '合成整合測試不屬於真實前向績效。' if synthetic else '本報告不構成真實下單授權。',
        '', '| 指標 | Baseline | Skill |','|---|---:|---:|']
    for name,field in [('決策回覆','decision_results'),('錯誤／審查錯誤','decision_errors'),('模擬成交','fills'),('現金','cash_twd'),('淨清算估值','liquidation_equity_twd'),('未成交委託','pending_orders'),('已回報 input tokens','known_input_tokens'),('已回報 output tokens','known_output_tokens')]:
        lines.append('| '+name+' | '+' | '.join(str(arms[a][field]) for a in ARMS)+' |')
    lines+=['',f'配對時段 {len(inputs)}，新鮮行情觀測 {report["fresh_quote_observations"]}，帳戶快照核對 {account_checks}。',
        f'未完成決策 {len(unresolved)}；稽核問題 {len(set(issues))}；缺 token 呼叫 {len(unknown)}（缺值不計為零）。',
        'LLM confidence 未當作勝率；無機率校準或市場指數超額報酬證據。',
        '完整資金、行情、成本、成交與延遲欄位見 comparison.json。']
    (out/'README.md').write_text('\n\n'.join(lines[:4])+'\n'+'\n'.join(lines[4:])+'\n',encoding='utf-8')
    return report
