"""Offline inactivity audit. Saved fills and market snapshots only; no API/orders."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import json
from pathlib import Path
import sqlite3
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from market_calendar import twse_closed_reason
from execution.auto_advice_executor import LiveExecutionConfig
from entry_screen import entry_screen
from trade_economics import evaluate_buy
from pretrade_policy import buy_block
from trade_plan_context import live_plans

JST = timezone(timedelta(hours=9))


def probe(decision):
    return ('DAILY_PARTICIPATION_PROBE' in decision.get('warnings', [])
            or '每日參與試單' in decision.get('reason', ''))


def arithmetic_upper_bound(candidate, account, rules, minimum_fee):
    """Optimistic sensitivity only: allow every legal target, ignoring intervening
    resistance. Passing this is NOT a tradable plan, forecast or approval.
    Keep the NT$20 funding reserve, allocation and loss limits unchanged.
    """
    c = candidate
    if (c.get('quote_status') != 'LIVE' or c.get('is_trial') or buy_block(c)
            or not c.get('eligible_strategies')):
        return None
    budget = Decimal(str(account['available_cash_twd']))
    risk = Decimal(str(account['capital_limit_twd'])) * Decimal('.005')
    ask = Decimal(str(c['ask']))
    levels = c.get('price_levels') or []
    prices = sorted({Decimal(str(row['price'])) for row in levels})
    entries = {ask}
    if c.get('pricing_policy') == 'PLANNED_LIMIT':
        entries |= {Decimal(str(r['price'])) for r in levels if r['id'].startswith('low_')
                    or r['id'] in ('previous_close','today_open','today_low')}
    best = None
    for entry in sorted(e for e in entries if 0 < e <= ask):
        for target in (p for p in prices if p > entry):
            for stop in (p for p in prices if p < entry and p < Decimal(str(c['last_price']))):
                for tier, fraction in (('Early','.15'),('Confirmed','.25'),('Strong','.30')):
                    allocation=budget*Decimal(fraction)
                    upper=min(int(c.get('max_buy_qty') or 0),int(allocation/entry),999)
                    for qty in range(upper,0,-1):
                        gross=entry*qty
                        reserve=max(rules.min_buy_fee_reserve_twd,gross*rules.buy_cash_buffer_rate,
                                    (gross*Decimal('.001425')).to_integral_value(rounding=ROUND_FLOOR))
                        if gross+reserve>allocation:
                            continue
                        try:
                            e=evaluate_buy(c,qty=qty,limit_price=float(entry),target_price=float(target),
                                           invalidation_price=float(stop),minimum_commission_twd=minimum_fee)
                        except (ValueError, ArithmeticError):
                            continue
                        if Decimal(str(e['estimated_net_downside_twd']))>risk:
                            continue
                        net=Decimal(str(e['estimated_net_upside_twd']))/Decimal(str(e['estimated_buy_cost_twd']))
                        if net<Decimal('.02') or (e['reward_risk_ratio'] or 0)<1.5:
                            break
                        row=dict(symbol=c['symbol'],tier=tier,entry=float(entry),target=float(target),
                            invalidation=float(stop),qty=qty,net_return=float(net),
                            estimated_net_upside_twd=e['estimated_net_upside_twd'],
                            estimated_net_downside_twd=e['estimated_net_downside_twd'],
                            omitted_intervening_resistance_check=True)
                        if best is None or row['estimated_net_upside_twd']>best['estimated_net_upside_twd']:
                            best=row
                        break
    return best


def audit(root, today):
    with sqlite3.connect((root/'data/analysis/market_history.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:
        records=db.execute('SELECT r.run_id,r.manifest_json,a.decision_json FROM scan_runs r '
                           'LEFT JOIN selection_analyses a USING(run_id)').fetchall()
    decisions={};daily={}
    for rid,raw,decision in records:
        manifest=json.loads(raw);day=manifest['started_at'][:10]
        parsed=json.loads(decision or '{}').get('decisions',[])
        decisions[rid]={d['symbol']:d for d in parsed}
        count=daily.setdefault(day,Counter())
        count['saved_runs']+=1;count['ai_'+str(manifest.get('ai_validation'))]+=1
        for d in parsed:
            count['probe_decision' if probe(d) else d['decision']]+=1
    ledger=root/'data/execution/live_execution.sqlite3'
    with sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True) as db:
        attempts=db.execute('SELECT run_id,symbol,side,started_at,status,detail_json '
                            'FROM execution_attempts ORDER BY started_at').fetchall()
        held=dict(db.execute('SELECT symbol,qty FROM ai_positions WHERE qty>0'))
    normal=[];excluded=[]
    for rid,symbol,side,stamp,status,raw in attempts:
        detail=json.loads(raw or '{}');qty=int(detail.get('filled_quantity') or 0)
        if qty<=0:
            continue
        original=decisions.get(rid,{}).get(symbol,{})
        row=dict(run_id=rid,symbol=symbol,side=side,filled_qty=qty,status=status,
                 order_time_jst=datetime.fromisoformat(stamp).astimezone(JST).isoformat())
        (excluded if probe(original) or '每日參與試單' in (detail.get('buy_plan') or {}).get('reason','') else normal).append(row)
    last=normal[-1];last_day=date.fromisoformat(last['order_time_jst'][:10])
    days=[];closed=[];day=last_day+timedelta(days=1)
    while day<=today:
        if twse_closed_reason(day):
            closed.append(dict(date=day.isoformat(),reason=twse_closed_reason(day)))
        else:
            days.append(day.isoformat())
        day+=timedelta(days=1)
    run_dirs=sorted(p for p in (root/'data/analysis/runs').glob(today.strftime('%Y%m%d')+'*')
                    if (p/'agent_response.json').is_file())
    current=run_dirs[-1]
    cs=json.loads((current/'analysis_set.json').read_text('utf-8'))
    account=json.loads((current/'paper_account_snapshot.json').read_text('utf-8'))
    rules=LiveExecutionConfig.load(root/'config/execution_live.json')
    current_screen={c['symbol']:entry_screen(c,account,rules) for c in cs}
    sensitivity={}
    for minimum in (20,1):
        sensitivity[str(minimum)]=[row for c in cs
            if (row:=arithmetic_upper_bound(c,account,rules,minimum))]
    plans=live_plans(ledger,held,root/'data/analysis/market_history.sqlite3',as_of=today)
    return dict(as_of_jst=datetime.now(JST).isoformat(),last_normal_fill=last,
        last_normal_buy=next(row for row in reversed(normal) if row['side']=='BUY'),
        excluded_forced_fills=excluded,no_normal_trade_sessions=days,
        complete_no_trade_sessions=sum(d<today.isoformat() for d in days),
        today_is_partial=True,closed_dates=closed,
        daily={d:dict(daily.get(d,{})) for d in days},
        daily_totals=dict(sum((daily.get(d,Counter()) for d in days),Counter())),
        latest_snapshot=current.name,available_budget_twd=account['available_cash_twd'],
        max_new_position_twd=account['available_cash_twd']*.30,
        modeled_loss_limit_twd=account['capital_limit_twd']*.005,
        selected_candidate_count=len(cs),
        unaffordable_one_share_at_ask=[dict(symbol=c['symbol'],ask=c['ask']) for c in cs
            if c['ask']+float(rules.min_buy_fee_reserve_twd)>account['available_cash_twd']*.30],
        current_screen=current_screen,
        optimistic_fee_sensitivity=sensitivity,
        restored_remaining_plans=plans,
        limitations=[
            'Filled orders use submission dates; exact historical broker fill timestamps are unavailable.',
            'Only the explicitly marked participation probe is excluded; ordinary one-share trades would count.',
            'Sensitivity keeps funding reserve, allocation, return/risk and loss limits fixed, but removes intervening resistance checks. Passing is not a valid trade.',
            'No live fees, trading limits or orders were changed by this audit.'])


if __name__=='__main__':
    result=audit(CODE,date(2026,11,3))
    (CODE/'reports/inactivity_20261103.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('current_screen','closed_dates','restored_remaining_plans')},ensure_ascii=False))
