"""Local fill drafts, historical proposals and live funding; never submits orders."""
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .contracts import DataError, TOKYO, strict_json_loads, timestamp, digest
from .japan import normalize_symbol
from .notifications import actionable
from .portfolio import load_fills, read_snapshot, record_fill
from .sizing import load_settings, attach_execution_plans
from .storage import atomic_json, pointer_lock


def _positive(value):
    return type(value) in (int,float) and math.isfinite(value) and 0<value<=1e12


def load_drafts(root):
    path=Path(root)/'fill_drafts.json'
    value=strict_json_loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema_version':1,'drafts':[]}
    if value.get('schema_version')!=1 or not isinstance(value.get('drafts'),list):raise DataError('INVALID_FILL_DRAFTS')
    seen=set()
    for row in value['drafts']:
        if (not isinstance(row,dict) or not isinstance(row.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',row['id'])
            or row['id'] in seen or normalize_symbol(row.get('symbol'))!=row['symbol']
            or type(row.get('reported_executed')) is not bool or not (_positive(row.get('amount_jpy')) or row.get('amount_jpy') is None and not row['reported_executed'])
            or row.get('fill_id')!='draft-'+row['id']):raise DataError('INVALID_FILL_DRAFT')
        timestamp(row['created_at']);seen.add(row['id'])
        if row.get('actual_price') is not None and not _positive(row['actual_price']):raise DataError('INVALID_FILL_DRAFT_PRICE')
    return value


def save_draft(root, value, *, now=None):
    if not isinstance(value,dict) or set(value)-{'id','symbol','amount_jpy','reported_executed','source_proposal_id','actual_price'}:
        raise DataError('INVALID_FILL_DRAFT_FIELDS')
    identifier=value.get('id') or uuid4().hex
    if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',identifier):raise DataError('INVALID_FILL_DRAFT_ID')
    symbol=normalize_symbol(value.get('symbol'));amount=value.get('amount_jpy');executed=value.get('reported_executed',False)
    if type(executed) is not bool or not (_positive(amount) or amount is None and not executed):raise DataError('INVALID_FILL_DRAFT_AMOUNT')
    source=value.get('source_proposal_id')
    actual_price=value.get('actual_price')
    if actual_price is not None and not _positive(actual_price):raise DataError('INVALID_FILL_DRAFT_PRICE')
    if source is not None and (not isinstance(source,str) or not re.fullmatch(r'[a-f0-9]{64}',source)):
        raise DataError('INVALID_PROPOSAL_REFERENCE')
    root=Path(root);path=root/'fill_drafts.json'
    with pointer_lock(root/'fill_drafts.lock'):
        data=load_drafts(root)
        if source and not value.get('id'):
            previous=next((r for r in reversed(data['drafts']) if r.get('source_proposal_id')==source and (r['reported_executed'] or not executed)),None)
            if previous:return deepcopy(previous)
        for row in data['drafts']:
            if row['id']==identifier:
                if any(row.get(k)!=v for k,v in {'symbol':symbol,'amount_jpy':amount,'reported_executed':executed,'source_proposal_id':source,'actual_price':actual_price}.items()):
                    raise DataError('FILL_DRAFT_IDEMPOTENCY_CONFLICT')
                return deepcopy(row)
        if len(data['drafts'])>=10000:raise DataError('FILL_DRAFT_HISTORY_LIMIT')
        row={'id':identifier,'symbol':symbol,'amount_jpy':amount,'reported_executed':executed,
            'source_proposal_id':source,'fill_id':'draft-'+identifier,'created_at':now or datetime.now(TOKYO).isoformat(),
            'status':'NEEDS_EXECUTION_DETAILS','quantity':None,'price':None,'filled_at':None,
            'actual_price':actual_price,
            'meaning':'使用者回報買入金額，成交明細待補。' if executed else '操作提案或手動草稿，尚未視為實際成交。'}
        data['drafts'].append(row);atomic_json(path,data)
        return deepcopy(row)


def pending_drafts(root, *, as_of=None):
    fills=load_fills(Path(root)/'manual_fills.json')['fills']
    cutoff=timestamp(as_of) if as_of else None
    completed={f['id']:f for f in fills if f['confirmed'] and (cutoff is None or timestamp(f['recorded_at'])<=cutoff)}
    def resolved(d):
        row=completed.get(d['fill_id']) or completed.get(confirmation_id(d['fill_id']))
        return row and row['symbol']==d['symbol'] and (not d['reported_executed'] or row['side']=='BUY')
    return [deepcopy(d) for d in load_drafts(root)['drafts'] if not resolved(d)
        and (cutoff is None or timestamp(d['created_at'])<=cutoff)]


def confirmation_id(identifier):
    return 'confirm-'+digest({'manual_fill_id':identifier})


def save_fill(root, value, *, now=None):
    value=deepcopy(value);draft_id=value.pop('draft_id',None)
    original=next((r for r in load_fills(Path(root)/'manual_fills.json')['fills'] if confirmation_id(r['id'])==value.get('id')),None)
    if original and (normalize_symbol(value.get('symbol'))!=original['symbol'] or value.get('side')!=original['side']):
        raise DataError('FILL_DRAFT_IDENTITY_MISMATCH')
    if draft_id is not None:
        draft=next((r for r in load_drafts(root)['drafts'] if r['id']==draft_id),None)
        if not draft or value.get('symbol') is None or normalize_symbol(value['symbol'])!=draft['symbol']:
            raise DataError('FILL_DRAFT_IDENTITY_MISMATCH')
        if value.get('id') not in {draft['fill_id'],confirmation_id(draft['fill_id'])}:
            raise DataError('FILL_DRAFT_IDENTITY_MISMATCH')
        if draft['reported_executed'] and value.get('side')!='BUY':raise DataError('FILL_DRAFT_IDENTITY_MISMATCH')
    return record_fill(Path(root)/'manual_fills.json',value,now=now)


def cash_reservations(root, *, as_of=None):
    return [{'id':d['id'],'symbol':d['symbol'],'amount_jpy':d['amount_jpy'],'reported_at':d['created_at'],
        'meaning':'已回報買入金額，成交明細待補；暫占資金，不視為已核對股數。'}
        for d in pending_drafts(root,as_of=as_of) if d['reported_executed']]


def historical_proposals(root, report=None):
    path=Path(root)/'notifications.sqlite3'
    if not path.exists():return []
    receipts={d['source_proposal_id']:d for d in load_drafts(root)['drafts'] if d.get('source_proposal_id')}
    fills={f['id']:f for f in load_fills(Path(root)/'manual_fills.json')['fills']}
    result=[]
    con=sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro',uri=True)
    try:
        for identifier,asof,status,payload in con.execute('SELECT id,as_of,status,payload FROM events ORDER BY as_of DESC LIMIT 200'):
            try:value=json.loads(payload)
            except (ValueError,TypeError):continue
            if not isinstance(value,dict):continue
            try:
                if not actionable(value):continue
            except (AttributeError,TypeError,ValueError):continue
            plan=value.get('decision_brief') or {};position=value.get('position') or {}
            draft=receipts.get(identifier);linked=(fills.get(confirmation_id(draft['fill_id'])) or fills.get(draft['fill_id'])) if draft else None
            result.append({'id':identifier,'symbol':value['symbol'],'name':plan.get('name',value['symbol']),
                'as_of':asof,'action':value['action'],'plan':deepcopy(plan),'held_quantity':position.get('quantity',0),
                'notification_status':status,'is_current':bool(report and asof==report.get('as_of')),
                'draft_id':draft['id'] if draft else None,'fill_id':linked['id'] if linked and linked['confirmed'] else None,
                'meaning':'保存的歷史操作提案，可回報當時已成交；不是新的即時交易建議。'})
            if len(result)>=40:break
    except sqlite3.Error:return []
    finally:con.close()
    return result


def live_funding(report, portfolio, settings, reservations):
    """Recompute only Python budget against current ledger; archived research stays immutable."""
    value=deepcopy(report) if report else {'securities':[],'synthetic':False}
    decision_asof=value.get('as_of')
    value['as_of']=portfolio['as_of']  # Internal sizing cutoff, explicitly separated in returned view.
    value['portfolio_snapshot']=portfolio
    value['cash_reservations']=deepcopy(reservations)
    original_positions=(report or {}).get('portfolio_snapshot',{}).get('positions',{})
    stale=decision_asof is not None and (timestamp(portfolio['as_of'])-timestamp(decision_asof)).total_seconds()>1800
    for item in value.get('securities',[]):
        position=portfolio['positions'].get(item['symbol'])
        if position and position!=original_positions.get(item['symbol']):
            item['manual_action']={'action':'REVIEW','label':'持倉已更新，等待下輪持有／退出評估'}
        if stale and not position:
            opportunity=item.get('opportunity_decision') or {}
            if opportunity.get('action')=='PROBE_BUY_REVIEW':
                opportunity['action']='WAIT';opportunity['label']='原提案需重新核對行情，暫不新增買入'
    attach_execution_plans(value,settings)
    brief=value['decision_brief']
    from .presentation import plain_code
    for plan in brief['rows']:
        plan['display_status']=plain_code(plan['sizing_status'])
        plan['current_position_note']='持倉已更新，等待下一輪研究' if plan['symbol'] in portfolio['positions'] else '買入金額已保留，成交明細待補' if any(r['symbol']==plan['symbol'] for r in reservations) else ''
    return {'funding_as_of':portfolio['as_of'],'research_as_of':decision_asof,
        'capital_jpy':settings['capital_jpy'],'available_cash_estimate_jpy':brief['available_cash_estimate_jpy'],
        'reserved_cash_jpy':sum(r['amount_jpy'] for r in reservations),'confirmed_cost_jpy':sum(p['total_cost'] for p in portfolio['positions'].values()),
        'proposed_total_jpy':brief['proposed_total_jpy'],'plans':brief['rows'],
        'meaning':'依使用者本金、確認成交與待補明細金額重算；不是券商餘額，沒有重新呼叫Agent。'}


def desk_state(root, report, now):
    root=Path(root);portfolio=read_snapshot(root/'manual_fills.json',now)
    drafts=pending_drafts(root,as_of=now);reservations=cash_reservations(root,as_of=now)
    history=load_fills(root/'manual_fills.json')['fills'][-100:]
    confirmed={r['id'] for r in load_fills(root/'manual_fills.json')['fills'] if r['confirmed']}
    return {'portfolio':portfolio,'drafts':drafts,'proposals':historical_proposals(root,report),
        'funding':live_funding(report,portfolio,load_settings(root/'sizing_settings.json'),reservations),
        'fill_history':[{**r,'confirmation_id':confirmation_id(r['id']),'confirmed_later':confirmation_id(r['id']) in confirmed} for r in history]}


ERROR_MESSAGES={
    'MANUAL_FILL_QUANTITY':'請填入實際成交股數，最多8位小數；20,000日圓應填在投入金額欄。',
    'MANUAL_FILL_PRICE_OR_FEES':'請填入每股實際成交價；費用不可為負數。',
    'MANUAL_FILL_CANNOT_BE_IN_FUTURE':'成交時間不能晚於現在，請確認日本時間。',
    'MANUAL_SELL_EXCEEDS_CONFIRMED_HOLDING':'賣出股數超過已確認持倉，請先補齊買入紀錄。',
    'MANUAL_FILL_IDEMPOTENCY_CONFLICT':'這筆成交已入帳，內容不同。請勿重複新增同一筆。',
    'INVALID_FILL_DRAFT_AMOUNT':'請填入正確的已投入金額。',
    'LOCAL_REQUEST_TOKEN_REQUIRED':'介面已更新或連線失效，請重新整理後再儲存。',
    'INVALID_AWARE_TIMESTAMP':'請填入完整成交日期與時間，系統以日本時間記錄。',
    'MANUAL_FILL_TIME_FORMAT':'請填入完整成交日期與時間，系統以日本時間記錄。',
    'FILL_DRAFT_IDENTITY_MISMATCH':'草稿與成交代號不一致，請按「改為新增另一筆」後再填寫。',
}


def error_message(error):
    return ERROR_MESSAGES.get(str(error),'儲存未完成，請核對股數、成交價及日本時間；技術原因：'+str(error))


def record_feedback(root, *, status, code, message, identifier=None, symbol=None):
    """Bounded local audit, without tokens, credentials or arbitrary request bodies."""
    root=Path(root);path=root/'fill_feedback.json'
    try:
        with pointer_lock(root/'fill_feedback.lock'):
            value=strict_json_loads(path.read_text(encoding='utf-8')) if path.exists() else []
            if not isinstance(value,list):value=[]
            value.append({'at':datetime.now(TOKYO).isoformat(),'status':status,'code':code,'message':message,
                'id':identifier[:80] if isinstance(identifier,str) else None,'symbol':symbol[:20] if isinstance(symbol,str) else None})
            atomic_json(path,value[-100:])
    except (OSError,ValueError):pass  # Feedback failure must never turn a committed fill into a reported failed write.
