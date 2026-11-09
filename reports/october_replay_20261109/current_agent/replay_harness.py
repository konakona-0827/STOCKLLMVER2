"""One real model evaluation with frozen historical tools and no executor."""
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path

# Load only the API credential/settings needed by this isolated research call.
env_path=Path(r'C:\Users\ho.chouchuan\Documents\HO\project\personalmono\STOCKLLM\.env')
lines=env_path.read_text(encoding='utf-8-sig').splitlines()
for i,line in enumerate(lines):
    key,sep,value=line.strip().partition('=')
    if sep and key in ('OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_ORG_ID','OPENAI_PROJECT_ID'):
        value=value.strip().strip('"').strip("'")
        if key=='OPENAI_API_KEY' and not value and i+1<len(lines):value=lines[i+1].strip()
        if value:os.environ[key]=value

from config import now
from execution.auto_advice_executor import LiveExecutionConfig
from trading_agent import TradingTools,analyze,agent_model
from support_scout.trading import SupportCoordinator
import strategy_catalog

out=Path(r'C:\Users\ho.chouchuan\Documents\HO\project\personalmono\STOCKLLM-agent\reports\october_replay_20261109\current_agent')
out.mkdir(parents=True,exist_ok=True)
rows=json.loads(Path('probe_candidates.json').read_text(encoding='utf-8'))
source_run=rows[0]['run']
rows=[row for row in rows if row['run']==source_run]
candidates=[deepcopy(row['candidate']) for row in rows]
account=deepcopy(rows[0]['account'])
rules=LiveExecutionConfig.load(Path('config/execution_live.json'))
frozen={c['symbol']:deepcopy(c) for c in candidates}
at=candidates[0]['quote_timestamp']
initial=dict(analysis_set=deepcopy(candidates),paper_account=deepcopy(account),
    evaluation=dict(mode='HISTORICAL_FROZEN_QUOTES_NO_EXECUTOR',as_of=at,
                    future_data_available=False,external_search=False))
ctx=TradingTools(candidates,rules,'october-frozen-'+source_run,out/'agent_tools.json',
    lambda symbol,previous:deepcopy(frozen[symbol]),lambda:deepcopy(account),lambda:[],
    initial_input=initial,search_request=dict(tools=[]),
    quote_batch_reader=lambda symbols,previous:{s:deepcopy(frozen[s]) for s in symbols})
cfg=json.loads(Path('config/support_agents.json').read_text(encoding='utf-8'))
def observation(symbol,candidate,count,root):
    # Only the original, frozen daily bars exist in this historical information
    # set. Report the gap; no provider may fetch today's/future market data.
    return dict(status='RAW_PRICE_OBSERVATION',technical=None,
        history_status=candidate.get('daily_history_status',{}),
        daily_bars=deepcopy(candidate.get('daily_bars',[])),
        limitations=['CORPORATE_ACTIONS_UNVERIFIED','ONLY_ARCHIVED_DAILY_HISTORY_AVAILABLE'])
ctx.support=SupportCoordinator(ctx,Path.cwd(),cfg,observation)
strategy=strategy_catalog.read_strategy(Path('strategies/strategy_opportunity_v2.md'))
instructions=('本輪是歷史凍結資訊研究回放，決策時間為 '+at+'。'
    '所有行情與帳戶工具都回傳該時點的真實保存快照；不要按今天日期判為過期，'
    '亦不得使用回放時點之後的知識、網路價格或未來結果。'
    '本環境沒有券商連線或下單器，仍按正常策略產生決策 JSON。\n'+strategy['prompt'])
(out/'frozen_input.json').write_text(json.dumps(initial,ensure_ascii=False,indent=2),encoding='utf-8')
started=datetime.now(timezone.utc).isoformat()
decision,response=analyze(ctx,instructions,model=agent_model())
result=dict(started_at=started,completed_at=datetime.now(timezone.utc).isoformat(),
    scope='TWO_ARCHIVED_QUALIFIED_CANDIDATES_ONE_FROZEN_RUN',source_run=source_run,
    broker_access=False,executor_available=False,orders_sent=False,
    decision=decision,response=response)
(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(validation=response['validation'],error=response['api'].get('error'),
    model=agent_model(),usage=response['api'].get('usage'),decision=decision),ensure_ascii=False,indent=2))
