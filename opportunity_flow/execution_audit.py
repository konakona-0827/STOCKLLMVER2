"""Durable READY-to-execution observations; never read as trading permission."""
from datetime import datetime
import json
from contextlib import closing
from pathlib import Path
import re
import sqlite3

from .common import TAIPEI
from .state import Ledger


def record(state,candidates,stage,reason='',detail=None):
    rows=[]
    for c in candidates:
        if c.get('state')!='RESEARCH_READY':continue
        packet=(c.get('review') or {}).get('packet') or c.get('packet') or {}
        if packet.get('packet_id') and c.get('symbol'):
            rows.append((packet['packet_id'],c['symbol'],c.get('selected_route_id')))
    if not rows:return
    ledger=None
    try:
        ledger=Ledger(state);at=datetime.now(TAIPEI).isoformat()
        for packet,symbol,route in rows:
            ledger.db.execute('''INSERT INTO execution_paths VALUES (?,?,?,?,?,?,?,?,1)
                ON CONFLICT(packet_id,stage,reason) DO UPDATE SET last_at=excluded.last_at,
                observations=observations+1,data=excluded.data''',
                (packet,symbol,stage,str(reason)[:200],at,at,route,json.dumps(detail or {})))
        ledger.db.commit()
    except Exception:
        print('FLOW_EXECUTION_PATH_AUDIT_UNAVAILABLE',flush=True)
    finally:
        if ledger is not None:
            try:ledger.close()
            except Exception:print('FLOW_EXECUTION_PATH_AUDIT_UNAVAILABLE',flush=True)


def outcome(state,candidates,report):
    try:_outcome(state,candidates,report)
    except Exception:print('FLOW_EXECUTION_PATH_AUDIT_UNAVAILABLE',flush=True)


def _outcome(state,candidates,report):
    for c in candidates:
        if c.get('state')!='RESEARCH_READY':continue
        symbol=c['symbol']
        found=[]
        packet_id=((c.get('review') or {}).get('packet') or {}).get('packet_id')
        for order in report.get('reports',[]):
            matching=[r for r in order.get('results',[]) if r.get('symbol')==symbol]
            if packet_id and order.get('flow_research_packet_id')==packet_id:
                found.append(dict(status=order.get('status'),results=[{k:r.get(k) for k in
                    ('symbol','status','broker_order_sent','filled_quantity')} for r in matching]))
        if found:record(state,[c],'HANDOFF_RECORDED',detail=dict(reports=found))
        else:
            blocks=[b for b in report.get('blocked',[]) if b.get('symbol')==symbol or
                (b.get('symbol') is None and not str(b.get('reason','')).startswith('POSITION_REVIEW_ERROR'))]
            if blocks:
                for block in blocks:record(state,[c],'BLOCKED',block.get('reason','UNSPECIFIED'))
            else:record(state,[c],'DEFERRED','NEXT_RECONCILED_EXECUTION_CYCLE')


def public_paths(state,day):
    """Current-day allowlisted observations, independent of bounded heartbeats."""
    path=Path(state)/'flow.sqlite3'
    if not path.exists():return dict(date=day,paths=[],truncated=False)
    rows=[]
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_paths'").fetchone():
            return dict(date=day,paths=[],truncated=False)
        for packet,symbol,stage,reason,first,last,route,count in db.execute('''SELECT packet_id,symbol,stage,reason,first_at,last_at,route_id,observations
                FROM execution_paths WHERE substr(last_at,1,10)=? ORDER BY first_at,packet_id,stage LIMIT 5001''',(day,)):
            if not (re.fullmatch('[0-9a-f]{64}',packet) and re.fullmatch('[0-9]{4,6}',symbol)
                    and stage in ('OFFER_RECEIVED','QUEUED','STARTED','BLOCKED','SUPERSEDED','INTERRUPTED','HANDOFF_RECORDED','DEFERRED')):continue
            rows.append(dict(packet_id=packet,symbol=symbol,stage=stage,
                reason=reason if re.fullmatch('[A-Z0-9_]{0,100}',reason) else 'DETAIL_IN_LOCAL_AUDIT',
                first_at=first,last_at=last,observations=count))
    return dict(date=day,scope='PERSISTED_CURRENT_DAY_PATHS_NOT_ORDER_COUNTS',paths=rows[:5000],truncated=len(rows)>5000)
