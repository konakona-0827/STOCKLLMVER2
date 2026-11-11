"""Known positives used during adjustment: regression, never holdout evidence."""
from operations.validate_target_reachability import ROOT,REPORT,read,save,candidate,calendar_context,path_outcome
from operations.replay_archived_plans import load_daily
from opportunity_engine import refresh,policy
from execution.auto_advice_executor import LiveExecutionConfig


def run():
    prior=REPORT/'revision2'
    labels=read(prior/'validation.json')
    universe={c['symbol']:c['name'] for c in read(prior/'validation_universe.json')['selections']}
    dates=read(prior/'verified_calendar.json')['dates']
    account=dict(available_cash_cents=1000000,available_cash_twd=10000,capital_limit_twd=10000,budget_status='READY')
    rows=[]
    for record in labels['rows']:
        if not record.get('reference_positive'):continue
        symbol,day=record['symbol'],record['day'];bars=load_daily(prior/'sources',symbol)
        opening=next(b['open'] for b in bars if b['date']==day)
        with calendar_context(dates):
            c=candidate(symbol,universe[symbol],1,day,opening,bars,dates)
            refresh(c,account,LiveExecutionConfig(minimum_commission_twd=1),policy(),research_proxy=True)
        setup=next((s for s in c['trade_setups'] if s['entry_basis']=='CURRENT_ASK'),None)
        outcome=path_outcome(day,setup,bars,dates) if setup else dict(status='NO_CURRENT_ASK_PLAN')
        rows.append(dict(symbol=symbol,day=day,current_setup=setup,outcome=outcome,
                         pending=c['pending_entry_setups'],status=c['opportunity_status']))
    save(REPORT/'corroborated/known_positive_regression.json',dict(
        scope='KNOWN_POSITIVES_USED_DURING_ADJUSTMENT_NOT_HOLDOUT',reference_positives=len(rows),
        retained=sum(r['outcome']['status']=='SUCCESS' for r in rows),
        missed=sum(r['outcome']['status']!='SUCCESS' for r in rows),rows=rows))
    print('known positives',len(rows),'retained',sum(r['outcome']['status']=='SUCCESS' for r in rows))


if __name__=='__main__':run()
