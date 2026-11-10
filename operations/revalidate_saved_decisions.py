"""Revalidate captured model replies offline; never rerun a model or approve new trades."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys


def revalidate(record,account,rules):
    import position_analyzer
    raw=record['response'].get('raw_response')
    base=dict(original_validation=record['response']['validation'],api_calls=0,orders_sent=False,
        scope='CURRENT_VALIDATOR_WITH_SAVED_QUOTES_AND_CANDIDATES; NO_NEW_RESEARCH_OR_EXECUTION_APPROVAL')
    if not raw:return dict(base,status='NO_SAVED_MODEL_REPLY')
    base['raw_response_sha256']=hashlib.sha256(raw.encode()).hexdigest()
    try:
        decision=json.loads(raw)
        selected=[c for c in deepcopy(record['all_candidates']) if c['symbol'] in record['selected_symbols']]
        position_analyzer.validate(decision,selected,account['available_cash_cents'],
            rules.economic_minimum_commission_twd,rules.buy_cash_buffer_rate,
            capital_twd=account.get('capital_limit_twd'),funding_fee_reserve_twd=rules.min_buy_fee_reserve_twd)
        final={d['symbol']:d for d in record['decision']['decisions']};preserved=[]
        identity=('decision','suggested_qty','suggested_price','strategy_id','target_price','invalidation_price','holding_days')
        for item in decision['decisions']:
            if item['decision'] not in ('BUY','SELL'):continue
            old=final.get(item['symbol'],{})
            if (record['response']['validation']!='VALID' or any(item.get(k)!=old.get(k) for k in identity)):
                preserved.append(item['symbol'])
                # A saved raw BUY may have been rejected by research/pretrade.
                # Offline validation must not remove that veto or mint approval.
                if old.get('decision') in ('WAIT','HOLD'):item.update(deepcopy(old))
                else:
                    position_analyzer._veto_trade(item,'OFFLINE_NO_SAVED_APPROVAL',
                        '離線重放沒有相同的原交易批准，維持不交易')
        return dict(base,status='VALID_SAVED_REPLY',decision=decision,preserved_vetoes=preserved,
            action_changes=[dict(symbol=d['symbol'],before=final.get(d['symbol'],{}).get('decision'),after=d['decision'])
                for d in decision['decisions'] if d['decision']!=final.get(d['symbol'],{}).get('decision')])
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        return dict(base,status='VALIDATION_ERROR',error=f'{type(exc).__name__}: {exc}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--benchmark',type=Path,required=True);parser.add_argument('--results',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();code=args.code_root.resolve()
    if any((code/name).exists() for name in ('.env','.trading-state.json','.git')):
        raise ValueError('USE_ISOLATED_CODE_SNAPSHOT_WITHOUT_ACCOUNT_BINDING')
    sys.path.insert(0,str(code))
    from execution.auto_advice_executor import LiveExecutionConfig
    from operations.expanded_opportunity_replay import read,save
    benchmark=read(args.benchmark);rules=LiveExecutionConfig.load(code/'config/execution_live.json');rows=[]
    for path in sorted(args.results.glob('*/result.json')):
        record=read(path)
        if record['benchmark_sha256']!=benchmark['benchmark_sha256']:raise ValueError('BENCHMARK_MISMATCH')
        rows.append(dict(group=record['group']['group_id'],**revalidate(record,benchmark['account'],rules)))
    save(args.output,dict(source=str(args.results),records=rows,api_calls=0,orders_sent=False,
        live_execution_authorized=False,new_model_decisions_generated=False))
    from collections import Counter
    print(dict(records=len(rows),statuses=dict(Counter(r['status'] for r in rows)),api_calls=0))


if __name__=='__main__':main()
