"""Replay entry qualification from archived quotes. No model/broker calls or fills."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from analysis_service import apply_buy_limits
from daily_history import daily_trend
from entry_screen import entry_screen
from execution.auto_advice_executor import LiveExecutionConfig
from market_calendar import previous_trading_day
from opportunity_engine import policy,select
from pretrade_policy import price_levels
from strategy_evidence import assess


def cached_bars(symbol,session):
    rows={}
    for path in (ROOT/'data/analysis/daily_history').glob(symbol+'_*.json'):
        record=json.loads(path.read_text('utf-8'))
        for bar in record['bars']:
            if bar['date']<session:
                rows[bar['date']]=bar
    # Completed prices only; data were repaired/retrieved after some archived
    # scans. This is a repaired-data scenario comparison, not a contemporaneous
    # information-set backtest and not evidence of achievable profit.
    return [rows[d] for d in sorted(rows)[-22:]]


def replay():
    universe=json.loads((ROOT/'config/universe_tw.json').read_text('utf-8'))
    rules=LiveExecutionConfig.load(ROOT/'config/execution_live.json')
    p=policy();results=[]
    for folder in sorted((ROOT/'data/analysis/runs').glob('2026110[23]T*')):
        if not (folder/'agent_response.json').is_file():continue
        original=json.loads((folder/'analysis_set.json').read_text('utf-8'))
        account=json.loads((folder/'paper_account_snapshot.json').read_text('utf-8'))
        manifest=json.loads((folder/'run.json').read_text('utf-8'))
        session=manifest['started_at'][:10]
        quotes=json.loads((folder/'multi_market_snapshot.json').read_text('utf-8'))['quotes']
        existing={c['symbol']:c for c in original}
        held={row['symbol'] for row in account['positions'] if row['qty']>0}
        ranks={r['symbol']:r.get('quant_rank') for r in json.loads((folder/'candidate_ranking.json').read_text('utf-8'))['ranking']}
        baseline=[c['symbol'] for c in original if entry_screen(deepcopy(c),account,
                   replace(rules,minimum_commission_twd=Decimal(20)))['scenarios']]
        pool=[]
        for quote in quotes:
            symbol=quote['symbol']
            if symbol not in universe['symbols'] and symbol not in held:continue
            c=deepcopy(existing.get(symbol,quote))
            if symbol not in existing:
                c.update(asset_type='ETF' if symbol in universe['etf_symbols'] else 'STOCK',
                    position=dict(ai_managed_qty=0),paper_position=dict(qty=0),market_history=[],
                    quant_rank=ranks.get(symbol))
            c['opportunity_buy_eligible']=symbol in universe['symbols']
            bars=cached_bars(symbol,session)
            c['daily_bars']=bars;c['daily_trend']=daily_trend(bars)
            dates={b['date'] for b in bars};cursor=date.fromisoformat(session);required=[]
            for _ in range(21):
                cursor=previous_trading_day(cursor);required.append(cursor.isoformat())
            c['daily_history_status']=dict(missing_20d_trading_dates=sorted(set(required)-dates))
            c['price_levels']=price_levels(c)
            e=assess(c,c.get('market_history'));c['strategy_evidence']=e;c['eligible_strategies']=e['eligible_strategies']
            pool.append(c)
        apply_buy_limits(pool,account,rules)
        selected,summary=select(pool,account,rules,p)
        results.append(dict(run=folder.name,baseline_qualified_symbols=baseline,
            baseline_model_candidate_count=len(original),opportunity_model_candidate_count=len(selected),
            account_cash=account['available_cash_twd'],search=summary,
            plans={c['symbol']:c['trade_setups'] for c in selected if c['trade_setups']}))
    return dict(scope='Fixed research universe and AI holdings only; manual holdings do not expand the buy universe.',
        limitations=['Repaired completed daily data, including later retrievals, are used. Not a performance backtest.',
                     'Qualified plans still require model research, two specialists, independent review and fresh execution guards.',
                     'No fill, profit, hit-rate or exact API saving can be inferred from this replay.'],runs=results)


if __name__=='__main__':
    result=replay()
    (ROOT/'reports/opportunity_replay_20261103.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for row in result['runs']:
        print(row['run'],'pool',row['search']['pool_count'],'old',row['baseline_qualified_symbols'],
              'new',row['search']['qualified_symbols'],'model_candidates',row['opportunity_model_candidate_count'])
