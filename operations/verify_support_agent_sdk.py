"""Real SDK/model smoke using synthetic data; no broker or execution invocation."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path

from config import ROOT, load_env, now
from execution.auto_advice_executor import LiveExecutionConfig
from support_scout.demo import demo_bundle
from support_scout.pipeline import run
from support_scout.trading import SupportCoordinator
from trading_agent import TradingTools, agent_model, analyze


def main():
    load_env()
    os.environ.setdefault('AGENT_TIMEOUT_SECONDS', '240')
    folder = ROOT/'data/analysis/agent'/('support-sdk-smoke-'+now().strftime('%Y%m%dT%H%M%S'))
    folder.mkdir(parents=True, exist_ok=False)
    bundle = demo_bundle()
    technical = run(bundle)['securities'][0]['technical']
    candidate = dict(symbol='9001', name='合成驗證標的，不是真實行情', asset_type='STOCK',
        last_price=107, bid=106.5, ask=107, open=106, high=108, low=104, volume=900000,
        quote_status='LIVE', quote_timestamp=now().isoformat(), is_trial=False,
        paper_position=dict(qty=10, cost_cents=100000), position=dict(ai_managed_qty=10),
        max_buy_qty=9, max_sell_qty=10, eligible_strategies=['intraday_recovery'])
    context = TradingTools([candidate], LiveExecutionConfig(), folder.name, folder/'agent_tools.json',
        lambda symbol, previous: dict(previous, quote_timestamp=now().isoformat()),
        lambda: dict(available_cash_cents=100000, capital_limit_twd=10000,
                     positions=[dict(symbol='9001', qty=10, cost_cents=100000)]),
        lambda: [], read_only=True,
        initial_input=dict(purpose='SYNTHETIC_SDK_VALIDATION_NO_BROKER_NO_ORDERS', analysis_set=[candidate]))
    config = dict(enabled=True, policy='advisory', history_bars=100, max_symbols=1, max_calls=2,
                  timeout_seconds=90, max_output_tokens=2000)
    observation = dict(status='SYNTHETIC_FIXTURE_NOT_LIVE_MARKET', technical=technical,
        daily_bars=bundle['securities'][0]['bars'], history_status=dict(synthetic=True),
        limitations=['SYNTHETIC_FIXTURE_NOT_LIVE_MARKET', 'NO_CORPORATE_ACTION_OR_EVENT_VERIFICATION'])
    context.support = SupportCoordinator(context, ROOT, config, lambda *args: deepcopy(observation))
    decision, response = analyze(context,
        '這是合成資料唯讀驗證，9001不是實際行情。依序讀帳戶、歷史，呼叫研究與反證各一次；'
        '不得略過子代理。最後只輸出HOLD或WAIT，沒有下單能力，不宣稱投資優勢。', model=agent_model())
    calls = response['support_research']['calls']
    passed = (response['validation']=='VALID' and
              {c['role'] for c in calls if c['status']=='COMPLETE'} == {'researcher','reviewer'} and
              all(d['decision'] in ('WAIT','HOLD') for d in decision['decisions']))
    summary = dict(passed=passed, model=agent_model(), validation=response['validation'],
        subagents=[dict(role=c['role'], status=c['status'], verdict=(c.get('review') or {}).get('verdict')) for c in calls],
        real_broker_connected=False, real_order_sent=False, artifact_directory=str(folder),
        error=response['api'].get('error'))
    (folder/'verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
