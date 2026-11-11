"""Development-only forensic replay of the seven known failed estimates."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from daily_history import daily_trend
from operations.replay_archived_plans import load_daily
from opportunity_engine import refresh, policy
from support_scout.market_sessions import load_sessions
from execution.auto_advice_executor import LiveExecutionConfig

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/target_reachability_20261111'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def source_history(archive):
    result = defaultdict(list)
    for path in sorted((archive/'data/analysis/runs').glob('*/multi_market_snapshot.json')):
        for quote in read(path).get('quotes', []):
            if quote.get('quote_status') != 'LIVE' or quote.get('is_trial') is not False:
                continue
            stamp = quote.get('quote_timestamp') or quote.get('exchange_time')
            available = quote.get('request_completed_at') or read(path).get('request_completed_at')
            if not stamp or not available:
                continue
            result[quote['symbol']].append(dict(symbol=quote['symbol'],exchange_time=stamp,
                price=quote.get('last_price',quote.get('price')),volume_shares=quote.get('volume'),
                quote_status='LIVE',is_trial=False,retrieval_mode=quote.get('retrieval_mode','BROKER'),
                available_at=available,evidence_file=str(path.relative_to(archive)),
                evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return result


def audit(archive, output=REPORT):
    previous = ROOT/'reports/opportunity_quality_20261110/final'
    old = read(previous/'validation.json')
    raw_history = source_history(archive)
    account = read(previous/'frozen.json')['account']
    rules = LiveExecutionConfig(minimum_commission_twd=Decimal(1),max_order_twd=Decimal(100000))
    records = []
    for trade in old['trades']:
        saved = read(previous/'model_runs'/trade['group_id']/'result.json')
        candidate = deepcopy(next(c for c in saved['all_candidates'] if c['symbol'] == trade['symbol']))
        day = candidate['quote_timestamp'][:10]
        bars = [dict(b,complete=True,source='TWSE_DAILY')
                for b in load_daily(REPORT/'sources',candidate['symbol']) if b['date'] < day][-240:]
        original = {b['date']:b for b in candidate['daily_bars']}
        revisions = [b['date'] for b in bars if b['date'] in original and any(
            float(b[k]) != float(original[b['date']][k]) for k in ('open','high','low','close','volume'))]
        candidate['daily_bars'] = bars
        candidate['daily_trend'] = daily_trend(bars)
        dates,calendar=load_sessions(bars[-1]['date'],240,REPORT/'market_sessions')
        candidate['daily_history_status']=dict(market_calendar=calendar,
            verified_trading_dates=dates if calendar['verified'] else [],
            missing_20d_trading_dates=sorted(set(dates[-21:])-{b['date'] for b in bars}))
        candidate['market_history'] = [deepcopy(q) for q in raw_history[candidate['symbol']]
            if q['exchange_time'] < candidate['quote_timestamp'] and q['available_at'] <= trade['as_of']]
        refresh(candidate,account,rules,policy())
        records.append(dict(as_of=trade['as_of'],symbol=candidate['symbol'],old_outcome=trade['outcome']['status'],
            old_setup=trade['setup'],new_setups=candidate['trade_setups'],
            pending_entry_setups=candidate['pending_entry_setups'],entry_watch=candidate.get('entry_watch'),
            status=candidate['opportunity_status'],reachability=candidate.get('target_reachability'),
            verified_prior_observations=len(candidate['market_history']),source_revisions=revisions))
    result = dict(scope='DEVELOPMENT_ONLY_NOT_HOLDOUT_VALIDATION',baseline_commit='9fdcd2b',cases=records,
        known_failures_with_remaining_plans=sum(r['old_outcome']=='FAILURE' and bool(r['new_setups']) for r in records),
        known_positive_with_remaining_plan=sum(r['old_outcome']=='SUCCESS' and bool(r['new_setups']) for r in records))
    output.mkdir(parents=True,exist_ok=True)
    (output/'development_forensics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    for r in records:
        reach=r.get('reachability') or {}
        print(r['as_of'],r['symbol'],r['old_outcome'],'plans',len(r['new_setups']),'pending',len(r['pending_entry_setups']),
              'range',round(reach.get('price_ceiling',0),3),'blockers',r['status']['blockers'])


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=REPORT)
    audit(ROOT.parent/'STOCKLLM',parser.parse_args().output)
