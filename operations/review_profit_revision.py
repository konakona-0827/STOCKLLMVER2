"""Frozen archived observations; threshold comparison, not a performance backtest."""
from copy import deepcopy
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

from config import CODE_ROOT
from opportunity_engine import refresh,policy


def main():
    # Any accidental remote I/O in this diagnostic is a test failure.
    def forbidden(*unused,**kwargs):
        raise AssertionError('OFFLINE_REVIEW_FORBIDS_NETWORK')
    socket.create_connection=forbidden
    socket.socket.connect=forbidden
    source=CODE_ROOT/'reports/tw_trade_20261110_until_112203_Taipei'
    manifest=json.loads((source/'manifest.json').read_text('utf-8'))
    assert all(hashlib.sha256((source/name).read_bytes()).hexdigest()==digest
               for name,digest in manifest['files'].items())
    archived=json.loads((source/'policies.json').read_text('utf-8'))['effective_live_config']
    rules=SimpleNamespace(max_buy_qty=archived['max_buy_qty'],max_order_twd=Decimal(str(archived['max_order_twd'])),
        min_buy_fee_reserve_twd=Decimal(str(archived['min_buy_fee_reserve_twd'])),
        buy_cash_buffer_rate=Decimal(str(archived['buy_cash_buffer_rate'])),
        economic_minimum_commission_twd=Decimal(str(archived['minimum_commission_twd'])))
    current=policy()
    baseline={**current,'accept_small_positive_profit':False,'minimum_target_net_profit_twd':50,
              'minimum_target_profit_to_analysis_cost':5}
    rows=[]
    for folder in sorted((source/'runs').iterdir()):
        account=json.loads((folder/'run.json').read_text('utf-8'))['account_snapshot']
        original=json.loads((folder/'candidates.json').read_text('utf-8'))['before_selection']
        results={}
        for name,p in [('same_code_old_profit_hurdle',baseline),('positive_net_profit',current)]:
            candidates=[refresh(deepcopy(c),account,rules,p) for c in original]
            results[name]=dict(qualified=[c['symbol'] for c in candidates if c['trade_setups']],
                observed_target_plans=sum(str(s['target_kind']).startswith('OBSERVED_')
                    for c in candidates for s in c['trade_setups']),
                prepared_exit_plans=sum('prepared_exit' in s for c in candidates for s in c['trade_setups']))
            results[name]['blockers']=dict(Counter(reason for c in candidates
                for reason in c['opportunity_status']['blockers']))
        rows.append(dict(run_id=folder.name,observed_symbols=len(original),results=results))
    result=dict(scope='FROZEN_OBSERVED_POOL_ONLY_NOT_UNSEEN_PERFORMANCE',
        no_models=True,no_broker=True,no_orders=True,no_future_outcomes=True,archive_hashes_verified=True,rows=rows,
        limitation='不包含當時未搜尋的上櫃股票，也不證明成交率、獲利率或全市場機會召回。')
    out=CODE_ROOT/'reports/profit_exit_20261111/offline_review.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
