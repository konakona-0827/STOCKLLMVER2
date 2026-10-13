"""SDK replaces the LLM input/output step; all execution stays in existing Python."""
import asyncio
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
from typing import Literal

from config import now, redact
from main import save_json
import paper_portfolio
import position_analyzer

AGENT_VERSION = 'agent-decision-v1'
AGENT_INSTRUCTIONS = '''你取代現有 LLM 的決策角色，輸入與最終 JSON 欄位沿用既有契約。
先呼叫 get_portfolio、get_trade_history，再自行選擇標的呼叫 get_market_data；可多次查詢。
BUY / SELL 前必須查詢該標的行情。只依據提供的真實行情、資金與持倉，不得虛構資料。
每檔都要回傳一筆 BUY / SELL / HOLD / WAIT 決策、股數、理由與原有其他欄位。
未持倉時不可 HOLD / SELL；觀望填 WAIT。HOLD / WAIT 的股數與 action_ratio 都是 0。
SELL 股數與 action_ratio 必須符合目前 AI 管理持倉；不可賣出人工持股。
BUY 須遵守 eligible_strategies，填目標價、失效價與持有天數，所有 BUY 合計含費用不得超過預算。
只讀、非 LIVE、試撮合或過期行情只回傳 HOLD / WAIT。
你只產生與現有 LLM 相同格式的決策 JSON；既有 Python 負責預算複檢、風控及下單。
不得聲稱委託已送出或成交。請以繁體中文說明理由。
'''


def execution_metadata(model):
    try:
        sdk_version = version('openai-agents')
    except PackageNotFoundError:
        sdk_version = None
    digest = hashlib.sha256()
    for name in ('trading_agent.py','agent_decision.py','analysis_service.py',
                 'position_analyzer.py','execution/auto_advice_executor.py'):
        digest.update((Path(__file__).parent/name).read_bytes())
    return dict(trading_mode='agent',agent_version=AGENT_VERSION,sdk_version=sdk_version,model=model,
                implementation_sha256=digest.hexdigest(),
                instructions_sha256=hashlib.sha256(AGENT_INSTRUCTIONS.encode()).hexdigest())


class TradingTools:
    def __init__(self, candidates, rules, run_id, log_path, quote_reader, portfolio_reader,
                 history_reader, stopped=lambda:False, read_only=False, initial_input=None):
        self.candidates = {c['symbol']:c for c in candidates}
        self.rules,self.run_id,self.log_path = rules,run_id,Path(log_path)
        self.quote_reader,self.portfolio_reader,self.history_reader = quote_reader,portfolio_reader,history_reader
        self.stopped,self.read_only,self.initial_input = stopped,read_only,initial_input
        self.calls,self.observed,self.account = [],set(),None
        self.portfolio_read = self.history_read = False

    def _call(self, name, args, operation):
        item = dict(call_id=f'{self.run_id}:{len(self.calls)+1}',tool=name,arguments=args,
                    started_at=now().isoformat(),status='STARTED')
        self.calls.append(item)
        save_json(self.log_path,self.calls)
        try:
            if self.stopped():
                raise ValueError('AGENT_STOP_REQUESTED')
            if len(self.calls)>60:
                raise ValueError('AGENT_TOOL_CALL_LIMIT')
            result = operation()
            item.update(status='COMPLETE',result=result)
        except Exception as exc:
            result = dict(error=redact(f'{type(exc).__name__}: {exc}'))
            item.update(status='ERROR',result=result)
        item['finished_at'] = now().isoformat()
        save_json(self.log_path,self.calls)
        return result

    def get_portfolio(self):
        def read():
            self.account = self.portfolio_reader()
            held = {p['symbol']:p for p in self.account['positions']}
            for symbol,c in self.candidates.items():
                c['paper_position'] = held.get(symbol,dict(symbol=symbol,qty=0,cost_cents=0))
                c['position']['ai_managed_qty'] = c['paper_position']['qty']
            self.portfolio_read = True
            return self.account
        return self._call('get_portfolio',{},read)

    def get_trade_history(self):
        def read():
            result = self.history_reader()
            self.history_read = True
            return result
        return self._call('get_trade_history',{},read)

    def get_market_data(self, symbol):
        def read():
            if symbol not in self.candidates:
                raise ValueError('SYMBOL_OUTSIDE_ANALYSIS_SET')
            row = self.quote_reader(symbol,self.candidates[symbol])
            if row.get('symbol') != symbol:
                raise ValueError('QUOTE_SYMBOL_MISMATCH')
            self.candidates[symbol].update(row)
            c = self.candidates[symbol]
            c['budget_price'] = c.get('ask') or c.get('last_price')
            if self.account is not None:
                c['max_buy_qty'] = (paper_portfolio.max_affordable_qty(self.account['available_cash_cents'],c['budget_price'])
                                    if c['budget_price'] and c['quote_status']=='LIVE' and not c.get('is_trial') else 0)
            self.observed.add(symbol)
            return dict(c)
        return self._call('get_market_data',dict(symbol=symbol),read)


async def sdk_run(context,instructions,model,max_turns,timeout_seconds):
    # Optional SDK: original LLM installations do not import it.
    from agents import Agent,ModelSettings,Runner,RunConfig,OpenAIResponsesModel,function_tool
    from openai import AsyncOpenAI
    from pydantic import BaseModel,ConfigDict

    class Decision(BaseModel):
        model_config = ConfigDict(extra='forbid')
        symbol: str
        decision: Literal['BUY','SELL','HOLD','WAIT']
        confidence: float
        action_ratio: float
        suggested_qty: int | None
        reference_price: float | None
        suggested_price: float | None
        strategy_id: Literal['NONE','intraday_recovery','short_term_reversal','breakout','multi_day_trend']
        target_price: float | None
        invalidation_price: float | None
        holding_days: int | None
        reason: str
        data_quality: Literal['LIVE','LAST_KNOWN','UNAVAILABLE']
        warnings: list[str]

    class FinalReport(BaseModel):
        model_config = ConfigDict(extra='forbid')
        market_view: str
        decisions: list[Decision]

    @function_tool
    def get_market_data(symbol:str)->dict:
        """Read fresh real market data and strategy evidence for one allowed symbol."""
        return context.get_market_data(symbol)

    @function_tool
    def get_portfolio()->dict:
        """Read the existing account's checked budget and current AI-managed positions."""
        return context.get_portfolio()

    @function_tool
    def get_trade_history()->list:
        """Read thirty recent attempts/fills from the existing execution database."""
        return context.get_trade_history()

    async with AsyncOpenAI(timeout=min(timeout_seconds,60),max_retries=0) as client:
        agent = Agent(name='Trading Decision Agent',instructions=instructions,
                      model=OpenAIResponsesModel(model=model,openai_client=client),
                      tools=[get_market_data,get_portfolio,get_trade_history],output_type=FinalReport,
                      model_settings=ModelSettings(parallel_tool_calls=False,store=False,
                                                   max_tokens=min(24000,max(6000,len(context.candidates)*650))))
        result = await asyncio.wait_for(Runner.run(agent,
            json.dumps(dict(input=context.initial_input,read_only=context.read_only),ensure_ascii=False),
            max_turns=max_turns,run_config=RunConfig(tracing_disabled=True)),timeout_seconds)
        save_json(context.log_path.with_name('agent_sdk_tools.json'),
                  [item.to_input_item() for item in result.new_items
                   if item.type in ('tool_call_item','tool_call_output_item')])
        usage = result.context_wrapper.usage
        return result.final_output.model_dump(),dict(input_tokens=usage.input_tokens,output_tokens=usage.output_tokens,
                                                     requests=usage.requests)


def analyze(context,strategy_prompt,model=None,runner=None):
    model = model or os.getenv('OPENAI_AGENT_MODEL') or os.getenv('OPENAI_MODEL','gpt-5.6-luna')
    response = dict(request_started_at=now().isoformat(),api=execution_metadata(model),raw_response='')
    try:
        turns,timeout = int(os.getenv('AGENT_MAX_TURNS','24')),float(os.getenv('AGENT_TIMEOUT_SECONDS','180'))
        if not 2<=turns<=60 or not 1<=timeout<=600:
            raise ValueError('INVALID_AGENT_LIMITS')
        decision,usage = asyncio.run((runner or sdk_run)(context,
            AGENT_INSTRUCTIONS+'\n\n'+strategy_prompt,model,turns,timeout))
        response['api']['usage'] = usage
        response['raw_response'] = json.dumps(decision,ensure_ascii=False)
        if context.stopped() or not context.portfolio_read or not context.history_read:
            raise ValueError('AGENT_STOPPED_OR_DID_NOT_READ_ACCOUNT_TOOLS')
        for item in decision.get('decisions',[]):
            if item.get('decision') in ('BUY','SELL'):
                if context.read_only or item.get('symbol') not in context.observed:
                    raise ValueError('TRADE_REQUIRES_MARKET_TOOL_AND_NON_READ_ONLY_MODE')
        position_analyzer.validate(decision,list(context.candidates.values()),context.account['available_cash_cents'],
                                   context.rules.min_buy_fee_reserve_twd,context.rules.buy_cash_buffer_rate)
        response['validation'] = 'VALID'
    except Exception as exc:
        response.update(validation='ERROR')
        response['api']['error'] = redact(f'{type(exc).__name__}: {exc}')
        decision = dict(market_view='Agent 決策失敗，本輪全部 WAIT。',decisions=[dict(
            symbol=c['symbol'],decision='WAIT',confidence=0,action_ratio=0,suggested_qty=0,
            reference_price=c.get('last_price'),suggested_price=None,strategy_id='NONE',
            target_price=None,invalidation_price=None,holding_days=None,reason='AGENT_FAILED_WAIT',
            data_quality=c['quote_status'],warnings=['不是即時行情'] if c['quote_status']=='LAST_KNOWN' else [])
            for c in context.candidates.values()])
    response.update(response_received_at=now().isoformat(),tool_calls=context.calls)
    save_json(context.log_path.with_name('agent_response.json'),response)
    return decision,response
