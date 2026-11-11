"""One real Agent contract replay on a known positive; never a holdout claim."""
from copy import deepcopy
from datetime import datetime
import os
from pathlib import Path
from unittest.mock import patch

from operations.validate_target_reachability import REPORT,ROOT,read,save,sha,verified_freeze
from operations.validate_search_revision import offline_python,execution_rules
from operations.audit_estimated_targets import source_history
from operations.replay_archived_plans import load_daily


def run(report=REPORT, source_report=REPORT, symbol='2377', as_of='2026-10-19T11:35:05+08:00'):
    global REPORT
    from operations import validate_target_reachability as study
    REPORT=report;study.REPORT=report
    verified_freeze()
    output=REPORT/('development_agent' if symbol=='2377' else 'development_agent_'+symbol+'_'+as_of[:19].replace(':','').replace('-',''))
    if (output/'result.json').exists():raise ValueError('REPLAY_ALREADY_RECORDED_NO_AUTOMATIC_RETRY')
    from daily_history import daily_trend
    from opportunity_engine import refresh,policy
    from trading_agent import TradingTools,analyze,agent_model
    from support_scout.trading import SupportCoordinator
    from support_scout.market_sessions import load_sessions
    import daily_participation
    previous=ROOT/'reports/opportunity_quality_20261110/final'
    old=read(previous/'validation.json')
    trade=next(t for t in old['trades'] if t['symbol']==symbol and t['as_of']==as_of)
    saved=read(previous/'model_runs'/trade['group_id']/'result.json')
    c=deepcopy(next(c for c in saved['all_candidates'] if c['symbol']==symbol))
    c['daily_bars']=[dict(b,complete=True,source='TWSE_DAILY') for b in load_daily(source_report/'sources',symbol)
                     if b['date']<as_of[:10]][-240:]
    dates,calendar=load_sessions(c['daily_bars'][-1]['date'],240,REPORT/'market_sessions')
    c['daily_history_status']=dict(market_calendar=calendar,verified_trading_dates=dates if calendar['verified'] else [],
        missing_20d_trading_dates=sorted(set(dates[-21:])-{b['date'] for b in c['daily_bars']}))
    c['daily_trend']=daily_trend(c['daily_bars'])
    history=source_history(ROOT.parent/'STOCKLLM')[symbol]
    c['market_history']=[q for q in history if q['exchange_time']<c['quote_timestamp'] and q['available_at']<=trade['as_of']]
    account=deepcopy(read(previous/'frozen.json')['account']);account.setdefault('positions',[])
    refresh(c,account,execution_rules(),policy())
    at=datetime.fromisoformat(trade['as_of'])
    output.mkdir(parents=True,exist_ok=True)
    initial=dict(analysis_set=[deepcopy(c)],paper_account=account,
        daily_participation=dict(policy=dict(enabled=False)),
        evaluation=dict(as_of=trade['as_of'],scope='KNOWN_CASE_CONTRACT_SMOKE_ONLY',
                        external_search=False,executor_available=False))
    save(output/'input.json',initial)
    quote_keys=('symbol','last_price','bid','ask','open','high','low','volume',
                'quote_timestamp','quote_status','is_trial')
    def quote(symbol,previous):return {k:deepcopy(c[k]) for k in quote_keys if k in c}
    context=TradingTools([c],execution_rules(),'reachability-known-positive',output/'tools.json',
        quote,lambda:deepcopy(account),lambda:[],initial_input=initial,search_request=dict(tools=[]),
        quote_batch_reader=lambda symbols,previous:{s:quote(s,previous[s]) for s in symbols},entry_skill_enabled=False)
    def observation(symbol,candidate,count,root):
        return dict(status='RAW_PRICE_OBSERVATION',technical=None,daily_bars=deepcopy(candidate['daily_bars']),
            history_status=deepcopy(candidate['daily_history_status']),
            limitations=['CORPORATE_ACTIONS_UNVERIFIED','FROZEN_PAST_DAILY_HISTORY_ONLY'])
    config=read(ROOT/'config/support_agents.json');config.update(max_symbols=1,max_calls=4)
    context.support=SupportCoordinator(context,ROOT,config,observation)
    instructions=('歷史凍結回放，唯一決策時間 '+trade['as_of']+
        '。所有工具只提供該時點真實保存資訊，外部搜尋停用，沒有下單器。'
        '正常獨立判斷，不強制買進，不以今天日期判過期。\n'+
        (ROOT/'strategies/strategy_opportunity_v2.md').read_text('utf-8'))
    # Only the authenticated ChatGPT CLI can transport model requests; no Python
    # external network, broker, executor, API fallback, or old paid-run resume.
    offline_python()
    progress=dict(status='READY',filled=dict(BUY=0,SELL=0),pending=dict(BUY=0,SELL=0),needed=['BUY','SELL'])
    with patch('trading_agent.now',return_value=at),patch('support_scout.trading.now',return_value=at), \
         patch('position_analyzer.now',return_value=at), \
         patch.object(daily_participation,'policy',return_value=dict(enabled=False)), \
         patch.object(daily_participation,'progress',return_value=progress), \
         patch.dict(os.environ,dict(AGENT_MAX_TURNS='10',AGENT_TIMEOUT_SECONDS='360')):
        decision,response=analyze(context,instructions,model=agent_model())
    verified_freeze()
    save(output/'result.json',dict(scope='KNOWN_CASE_CONTRACT_SMOKE_ONLY_NOT_UNSEEN_ALPHA',
        freeze_sha256=sha(REPORT/'freeze.json'),input_sha256=sha(output/'input.json'),as_of=trade['as_of'],
        replay_program_sha256=sha(Path(__file__)),
        decision=decision,response=response,broker_access=False,executor_available=False,orders_sent=False))
    print(response['validation'],[(d['symbol'],d['decision']) for d in decision['decisions']],flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,default=REPORT)
    parser.add_argument('--source-report',type=Path,default=REPORT)
    parser.add_argument('--symbol',default='2377');parser.add_argument('--as-of',default='2026-10-19T11:35:05+08:00')
    args=parser.parse_args();run(args.report,args.source_report,args.symbol,args.as_of)
