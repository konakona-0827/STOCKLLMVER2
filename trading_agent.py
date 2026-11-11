"""SDK replaces the LLM input/output step; all execution stays in existing Python."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
from typing import Literal

from config import now, redact
from main import save_json
import position_analyzer
from web_research import search_audit
from token_budget import compact, candidate_view, decision_input, usage_snapshot

AGENT_VERSION = 'agent-decision-v13-plan-handoff'
DEFAULT_AGENT_MODEL = 'gpt-6-sol'
AGENT_INSTRUCTIONS = '''你取代現有 LLM 的決策角色，輸入與最終 JSON 欄位沿用既有契約。
帳戶及歷史由程式預讀；account_and_history_preloaded=true 時不用再呼叫帳戶與歷史工具。
需要更新時可呼叫 get_portfolio、get_trade_history。BUY / SELL 前必須查最新 get_market_data，
多檔可用 get_market_data_batch 一次查詢；刷新帳戶後須重新查交易標的行情。
BUY / SELL 前必須查詢該標的行情。只依據提供的真實行情、資金與持倉，不得虛構資料。
每檔都要回傳一筆 BUY / SELL / HOLD / WAIT 決策、股數、理由與原有其他欄位。
未持倉時不可 HOLD / SELL；觀望填 WAIT。HOLD / WAIT 的股數與 action_ratio 都是 0。
SELL 股數與 action_ratio 必須符合目前 AI 管理持倉；不可賣出人工持股。
盤中零股不得當沖，SELL 另須遵守最新 max_sell_qty；成本與買賣日期依 ai_cost_basis、ai_trade_dates。
daily_bars、daily_trend 與 daily_history_status 是既有日線資料，不得在重新查價後忽略。
初始 daily_bars 僅最近五筆、market_history 僅最近三筆；coverage 標明完整筆數，並非缺資料。
需要更早原始資料驗證結構時呼叫 get_research_data；不可從截短顯示推論歷史缺漏。
entry_screen／trade_setups 是 Python 按現行資料、成本、配置及風控算出的有限情境，非交易批准。
有機會候選時主動比較回測限價、現價與等待觸發路徑，選出最能用最新證據驗證的一案。
recovery_watch 是觀察基準；確認守住已收復價後，程式會依現行風控生成可選計畫。
機會策略的 BUY 須有兩位子代理引用所選 ENTRY_PLAN 證據；刷新後若限價或結構已改變，重新研究或改為 WAIT。
entry_structure、trade_setups.target_evidence 與 entry_evidence.stop_origin 是引擎與研究共用的結構依據。
兩日回升已把前一完整日高點與已跌破波段支撐列為近端阻力；較長趨勢計畫不把每筆日高當硬上限。
對通過計畫選 WAIT 時，提出該計畫尚未涵蓋的具體反證、來源日期與重新評估條件；不得把數字合格當批准。
失效價須核對形成日期；形成前的低價不能當成形成後已失效。過期舊支撐不等於當前新支撐。
Early 情境不要求尚未存在的今日完整日線；可描述未確認風險，不把另一策略的確認條件自動套用。
previous_candidate_rejections 是本輪已研究並否決的其他標的；不要重試舊判斷來湊交易，繼續比較當前新候選。
PLANNED_LIMIT 的進場基準是具證據的 suggested_price，不是強制按 ask 追買；
可提出等待回落的當日限價，須說明回落後仍有效的條件，成交與獲利均不保證。
其他模式按 ask 與建議價較高者估算。不得為通過門檻跳過近端壓力或貼近失效價。
WAIT/HOLD 的 reason 簡述主要未通過條件，以及可重新評估的具體價位/事件；
有持倉先評估原計畫與退出風險，不為沒有數字可行性的候選反覆研究。
每檔 reason 精簡重複歷史、預算與策略條文；BUY/SELL 保留必要計算與反證，不為縮短篇幅省略關鍵依據。
BUY 須遵守 eligible_strategies，填目標價、失效價與持有天數，所有 BUY 合計含費用不得超過預算。
只讀、非 LIVE、試撮合或過期行情只回傳 HOLD / WAIT。
data_quality 必須逐字等於該檔 quote_status。LAST_KNOWN 的 warnings 至少一項須逐字包含「不是即時行情」，並列出 quote_timestamp；UNAVAILABLE 只能 WAIT 且 reference_price=null。
你只產生與現有 LLM 相同格式的決策 JSON；既有 Python 負責預算複檢、風控及下單。
不得聲稱委託已送出或成交。請以繁體中文說明理由。
'''


def agent_model():
    """Select the Agent model independently from the legacy LLM setting."""
    return os.getenv('CODEX_MODEL') or os.getenv('OPENAI_AGENT_MODEL') or DEFAULT_AGENT_MODEL


def execution_metadata(model):
    from codex_cli import metadata
    try:
        sdk_version = version('openai-agents')
    except PackageNotFoundError:
        sdk_version = None
    digest = hashlib.sha256()
    for name in ('trading_agent.py','codex_cli.py','token_budget.py','research_runtime.py','runtime_skills.py',
                 'skills/tw-entry-research/SKILL.md','config/research_skills.json',
                 'trade_economics.py','pretrade_policy.py','strategies/strategy_opportunity_v2.md',
                 'entry_decision.py','config/entry_decision.json',
                 'entry_screen.py','entry_structure.py','opportunity_engine.py','opportunity_search_cycle.py','opportunity_gate.py',
                 'opportunity_watch.py','opportunity_diagnostics.py',
                 'config/opportunity_search.json','config/market_discovery.json','market_discovery.py','weekly_recovery.py',
                 'config/execution_live.json','advice_interface.py',
                 'daily_history.py','agent_decision.py','analysis_service.py',
                 'position_analyzer.py','trade_plan_context.py','execution/auto_advice_executor.py',
                 'execution/broker_runtime.py','execution/capital_reply_session.py',
                 'support_scout/trading.py', 'support_scout/market_sessions.py',
                 'support_scout/agents.py', 'support_scout/support.py',
                 'config/support_agents.json', 'daily_participation.py',
                 'config/daily_participation.json'):
        digest.update((Path(__file__).parent/name).read_bytes())
    return dict(trading_mode='agent',agent_version=AGENT_VERSION,sdk_version=sdk_version,model=model,**metadata(),
                implementation_sha256=digest.hexdigest(),
                instructions_sha256=hashlib.sha256(AGENT_INSTRUCTIONS.encode()).hexdigest())


class TradingTools:
    def __init__(self, candidates, rules, run_id, log_path, quote_reader, portfolio_reader,
                 history_reader, stopped=lambda:False, read_only=False, initial_input=None,
                 live_enabled=True, search_request=None, quote_batch_reader=None,entry_skill_enabled=None):
        self.candidates = {c['symbol']:c for c in candidates}
        self.rules,self.run_id,self.log_path = rules,run_id,Path(log_path)
        self.quote_reader,self.portfolio_reader,self.history_reader = quote_reader,portfolio_reader,history_reader
        self.quote_batch_reader = quote_batch_reader
        self.stopped,self.read_only,self.initial_input = stopped,read_only,initial_input
        self.calls,self.observed,self.account = [],set(),None
        self.portfolio_read = self.history_read = False
        self.live_enabled = live_enabled
        self.search_request = search_request or {}
        self.search_output = []
        self.support = None
        self.history = []
        self.usage_requests = []
        self.entry_research_skill = None
        self.entry_skill_enabled=entry_skill_enabled

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
            item.update(status='COMPLETE',result=deepcopy(result))
        except Exception as exc:
            result = dict(error=redact(f'{type(exc).__name__}: {exc}'))
            item.update(status='ERROR',result=result)
        item['finished_at'] = now().isoformat()
        save_json(self.log_path,self.calls)
        return result

    def get_portfolio(self):
        self.portfolio_read = False
        self.account = None
        self.observed.clear()
        def read():
            from analysis_service import apply_buy_limits
            self.account = self.portfolio_reader()
            self.account.setdefault('available_cash_twd', self.account['available_cash_cents']/100)
            held = {p['symbol']:p for p in self.account['positions']}
            for symbol,c in self.candidates.items():
                c['paper_position'] = held.get(symbol,dict(symbol=symbol,qty=0,cost_cents=0))
                c['position']['ai_managed_qty'] = c['paper_position']['qty']
            apply_buy_limits(list(self.candidates.values()), self.account,
                             self.rules if self.live_enabled else None)
            from opportunity_engine import refresh
            for c in self.candidates.values():
                if c.get('opportunity_policy'):refresh(c,self.account,self.rules)
            self.portfolio_read = True
            return self.account
        return self._call('get_portfolio',{},read)

    def get_trade_history(self):
        self.history_read = False
        def read():
            self.history = []
            result = self.history_reader()
            self.history = deepcopy(result)
            self.history_read = True
            return result
        return self._call('get_trade_history',{},read)

    def get_market_data(self, symbol):
        self.observed.discard(symbol)
        def read():
            if symbol not in self.candidates:
                raise ValueError('SYMBOL_OUTSIDE_ANALYSIS_SET')
            if not self.portfolio_read or self.account is None:
                raise ValueError('PORTFOLIO_MUST_BE_READ_FIRST')
            row = self.quote_reader(symbol,self.candidates[symbol])
            if row.get('symbol') != symbol:
                raise ValueError('QUOTE_SYMBOL_MISMATCH')
            return self._accept_quote(symbol, row)
        return self._call('get_market_data',dict(symbol=symbol),read)

    def _accept_quote(self, symbol, row):
        from analysis_service import apply_buy_limits
        self.candidates[symbol].update(row)
        c = self.candidates[symbol]
        apply_buy_limits([c], self.account, self.rules if self.live_enabled else None)
        if c.get('opportunity_policy'):
            from opportunity_engine import refresh
            refresh(c,self.account,self.rules)
        self.observed.add(symbol)
        return dict(c)

    def get_market_data_batch(self, symbols):
        if isinstance(symbols, list):
            self.observed.difference_update(s for s in symbols if isinstance(s,str))
        def read():
            if (not isinstance(symbols,list) or not 1 <= len(symbols) <= 5
                    or any(not isinstance(s,str) for s in symbols) or len(set(symbols)) != len(symbols)):
                raise ValueError('EXPECTED_1_TO_5_DISTINCT_SYMBOLS')
            if any(s not in self.candidates for s in symbols):
                raise ValueError('SYMBOL_OUTSIDE_ANALYSIS_SET')
            if not self.portfolio_read or self.account is None:
                raise ValueError('PORTFOLIO_MUST_BE_READ_FIRST')
            previous = {s:self.candidates[s] for s in symbols}
            rows = (self.quote_batch_reader(symbols, previous) if self.quote_batch_reader else
                    {s:self.quote_reader(s, previous[s]) for s in symbols})
            # Validate the entire handoff before accepting any replacement.
            if (not isinstance(rows,dict) or set(rows) != set(symbols)
                    or any(not isinstance(rows[s],dict) or rows[s].get('symbol') != s for s in symbols)):
                raise ValueError('BATCH_QUOTE_SYMBOL_MISMATCH')
            return [self._accept_quote(s,rows[s]) for s in symbols]
        result = self._call('get_market_data_batch',dict(symbols=symbols),read)
        if isinstance(result,dict) and isinstance(symbols,list):
            self.observed.difference_update(s for s in symbols if isinstance(s,str))
        return result


async def sdk_run(context,instructions,model,max_turns,timeout_seconds):
    # Optional SDK: original LLM installations do not import it.
    from agents import Agent,Model,ModelSettings,Runner,RunConfig,OpenAIResponsesModel,WebSearchTool,function_tool
    from codex_cli import AsyncCodexClient
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
        row = context.get_market_data(symbol)
        return row if 'error' in row else candidate_view(row, context.account, context.rules)

    @function_tool
    def get_market_data_batch(symbols:list[str])->list:
        """Refresh 1-5 distinct symbols in one turn, after research and before trading."""
        rows = context.get_market_data_batch(symbols)
        if isinstance(rows,dict):return [rows]
        return [row if 'error' in row else candidate_view(row, context.account, context.rules) for row in rows]

    @function_tool
    def get_research_data(symbol:str)->dict:
        """Read full stored daily bars and intraday samples to verify older structure; not a quote refresh."""
        def read():
            if symbol not in context.candidates:
                raise ValueError('SYMBOL_OUTSIDE_ANALYSIS_SET')
            return deepcopy(context.candidates[symbol])
        return compact(context._call('get_research_data', dict(symbol=symbol), read))

    @function_tool
    def get_portfolio()->dict:
        """Read the existing account's checked budget and current AI-managed positions."""
        return context.get_portfolio()

    @function_tool
    def get_trade_history()->list:
        """Read thirty recent attempts/fills from the existing execution database."""
        return compact(context.get_trade_history())

    tools = [get_market_data, get_market_data_batch, get_research_data, get_portfolio, get_trade_history]
    search = next((tool for tool in context.search_request.get('tools', [])
                   if tool.get('type') == 'web_search'), None)
    search_limit = context.search_request.get('max_tool_calls')
    if search is not None:
        if search_limit is not None and (type(search_limit) is not int or not 1 <= search_limit <= 10):
            raise ValueError('WEB_SEARCH_MAX_TOOL_CALLS_MUST_BE_1_TO_10')
        tools.append(WebSearchTool(user_location=search.get('user_location'),
                                   search_context_size=search.get('search_context_size', 'medium'),
                                   external_web_access=search.get('external_web_access', True)))

    class SearchBudgetModel(Model):
        """Apply the existing per-run search cap across every SDK model turn."""
        def __init__(self, delegate):
            self.delegate = delegate

        async def get_response(self, *args, **kwargs):
            used = sum(item.get('type') == 'web_search_call' for item in context.search_output)
            remaining = search_limit - used if search_limit is not None else None
            if remaining is None:
                pass
            elif remaining:
                settings = kwargs['model_settings']
                kwargs['model_settings'] = replace(
                    settings, extra_args={**(settings.extra_args or {}), 'max_tool_calls': remaining})
            else:
                kwargs['tools'] = [tool for tool in kwargs['tools'] if tool.name != 'web_search']
            response = await self.delegate.get_response(*args, **kwargs)
            context.search_output.extend(item.model_dump(mode='json') for item in response.output)
            if search_limit is not None and sum(item.get('type') == 'web_search_call' for item in context.search_output) > search_limit:
                raise ValueError('AGENT_WEB_SEARCH_LIMIT_EXCEEDED')
            return response

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError('Trading decisions use the non-streaming runner')
            yield

    class MeteredModel(Model):
        """Persist every completed call, including specialists, before parsing output."""
        def __init__(self, delegate):
            self.delegate = delegate

        async def get_response(self, *args, **kwargs):
            before=len(context.usage_requests)
            try:
                response = await self.delegate.get_response(*args, **kwargs)
            except BaseException:
                # A failed/timeout provider call can still be billable; never
                # report the previous successful calls as the full invoice.
                context.usage_incomplete |= not recorder.accounting_complete or len(context.usage_requests)==before
                raise
            row = dict(usage_snapshot(response.usage), response_id=response.response_id,
                       input_characters=len(json.dumps(kwargs.get('input', ''), ensure_ascii=False)))
            # Real provider responses were recorded before SDK validation;
            # custom/test Model implementations still need this fallback.
            if not any(r.get('response_id')==response.response_id for r in context.usage_requests):
                context.usage_requests.append(row)
            save_json(context.log_path.with_name('api_usage_requests.json'), context.usage_requests)
            return response

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError('Trading decisions use the non-streaming runner')
            yield

    # Read deterministic account/history before spending a model turn asking
    # for them. The same methods/guards/logs are used by later tool refreshes.
    context.usage_requests = []
    context.usage_incomplete = False
    for read in (context.get_portfolio, context.get_trade_history):
        if 'error' in read():
            raise ValueError('AGENT_ACCOUNT_PREFLIGHT_FAILED')
    model_input = dict(input=decision_input(context), read_only=context.read_only)
    save_json(context.log_path.with_name('agent_model_input.json'), model_input)
    from research_runtime import ResponseRecorder,totals
    async with AsyncCodexClient(timeout=timeout_seconds or 180) as client:
        recorder=ResponseRecorder(context.log_path.with_name('api_usage_requests.json'),
            'trading_and_support',context.usage_requests,context.stopped,
            on_incomplete=lambda response:context.search_output.extend(
                item.model_dump(mode='json') for item in response.output)).install(client)
        provider = MeteredModel(OpenAIResponsesModel(model=model,openai_client=client))
        if context.support is not None:
            tools.extend(context.support.sdk_tools(provider))
            instructions += context.support.instructions()
        agent = Agent(name='Trading Decision Agent',instructions=instructions,
                      model=SearchBudgetModel(provider) if search else provider,
                      tools=tools,output_type=FinalReport,
                      model_settings=ModelSettings(parallel_tool_calls=False,store=False,
                                                   response_include=context.search_request.get('include'),
                                                   max_tokens=None))
        task=Runner.run(agent,
            json.dumps(model_input,ensure_ascii=False,separators=(',', ':')),
            max_turns=max_turns,run_config=RunConfig(tracing_disabled=True))
        result=await asyncio.wait_for(task,timeout_seconds) if timeout_seconds else await task
        context.usage_incomplete |= not recorder.accounting_complete
        save_json(context.log_path.with_name('agent_sdk_tools.json'),
                  [item.to_input_item() for item in result.new_items
                   if item.type in ('tool_call_item','tool_call_output_item')])
        return result.final_output.model_dump(), totals(context.usage_requests)


async def _sdk_review(request):
    from agents import Agent, ModelSettings, Runner, RunConfig, OpenAIResponsesModel
    from codex_cli import AsyncCodexClient
    from pydantic import BaseModel, ConfigDict

    class ReviewItem(BaseModel):
        model_config = ConfigDict(extra='forbid')
        symbol: str
        approved: bool
        reason: str

    class ReviewReport(BaseModel):
        model_config = ConfigDict(extra='forbid')
        reviews: list[ReviewItem]

    timeout = float(os.environ['AGENT_TIMEOUT_SECONDS']) if os.getenv('AGENT_TIMEOUT_SECONDS') else None
    from research_runtime import ResponseRecorder,totals
    async with AsyncCodexClient(timeout=timeout or 180) as client:
        recorder=ResponseRecorder(Path(request['usage_path']),'pretrade_review').install(client) if request.get('usage_path') else None
        agent = Agent(name='Trading Pretrade Review Agent', instructions=request['instructions'],
                      model=OpenAIResponsesModel(model=request['model'], openai_client=client),
                      output_type=ReviewReport,
                      model_settings=ModelSettings(store=False, max_tokens=None))
        task=Runner.run(agent, request['input'], max_turns=None,run_config=RunConfig(tracing_disabled=True))
        result=await asyncio.wait_for(task,timeout) if timeout else await task
        usage = result.context_wrapper.usage
        recorded=totals(recorder.rows) if recorder and recorder.rows else usage_snapshot(usage)
        return json.dumps(result.final_output.model_dump(), ensure_ascii=False), dict(
            model=request['model'], runtime='codex-cli', billing_source='chatgpt-plan', api_fallback=False, usage=dict(recorded,
                total_tokens=recorded['input_tokens']+recorded['output_tokens']),
            usage_accounting_complete=recorder.accounting_complete if recorder else True)


def sdk_review(request):
    return asyncio.run(_sdk_review(request))


def analyze(context,strategy_prompt,model=None,runner=None,reviewer=None):
    model = model or agent_model()
    response = dict(request_started_at=now().isoformat(),api=execution_metadata(model),raw_response='')
    try:
        from runtime_skills import load_entry_research
        skill_instructions = load_entry_research(context)
        response['api']['research_skill_policy']=context.entry_research_policy
        if context.entry_research_skill is not None:
            response['api']['research_skills'] = [context.entry_research_skill]
        turns=None
        timeout=float(os.environ['AGENT_TIMEOUT_SECONDS']) if os.getenv('AGENT_TIMEOUT_SECONDS') else None
        if timeout is not None and timeout<1:
            raise ValueError('INVALID_AGENT_LIMITS')
        decision,usage = asyncio.run((runner or sdk_run)(context,
            AGENT_INSTRUCTIONS+'\n表格傳輸：daily_bars／market_history／price_levels 若為 {columns, rows}，每列依 columns 對應欄位。\n\n'+strategy_prompt+skill_instructions,model,turns,timeout))
        response['api']['usage'] = usage
        response['raw_response'] = json.dumps(decision,ensure_ascii=False)
        if context.stopped() or not context.portfolio_read or not context.history_read:
            raise ValueError('AGENT_STOPPED_OR_DID_NOT_READ_ACCOUNT_TOOLS')
        for item in decision.get('decisions',[]):
            if item.get('decision') in ('BUY','SELL'):
                if context.read_only or item.get('symbol') not in context.observed:
                    raise ValueError('TRADE_REQUIRES_MARKET_TOOL_AND_NON_READ_ONLY_MODE')
        position_analyzer.validate(decision,list(context.candidates.values()),context.account['available_cash_cents'],
                                   context.rules.economic_minimum_commission_twd,context.rules.buy_cash_buffer_rate,
                                   capital_twd=context.account.get('capital_limit_twd'),
                                   funding_fee_reserve_twd=context.rules.min_buy_fee_reserve_twd)
        from daily_participation import add_probes
        response['daily_participation'] = add_probes(context, decision)
        if response['daily_participation']['proposed']:
            position_analyzer.validate(decision, list(context.candidates.values()),
                context.account['available_cash_cents'], context.rules.economic_minimum_commission_twd,
                context.rules.buy_cash_buffer_rate, capital_twd=context.account.get('capital_limit_twd'),
                funding_fee_reserve_twd=context.rules.min_buy_fee_reserve_twd)
        plan_reviews = {}
        if context.support is not None:
            for item in decision['decisions']:
                if item['decision'] == 'BUY':
                    symbol = item['symbol']
                    reasons = context.support.buy_block_reasons(symbol, item)
                    if reasons:
                        position_analyzer._veto_trade(item, reasons[0], '；'.join(reasons))
                    else:
                        plan_reviews[symbol] = context.support.plan_handoff(symbol, item)
        review_input = dict(context.initial_input or {})
        review_input.update(analysis_set=list(context.candidates.values()), paper_account=context.account,
                            independent_plan_reviews=plan_reviews)
        from daily_participation import policy, progress
        review_input['daily_participation'] = dict(policy=policy(), progress=progress())
        review_input.setdefault('paper_simulation_rules', dict(
            minimum_commission_twd=float(context.rules.economic_minimum_commission_twd),
            buy_cash_buffer_rate=float(context.rules.buy_cash_buffer_rate)))
        def default_review(request):
            return sdk_review(dict(request,usage_path=str(context.log_path.with_name('pretrade_usage_requests.json'))))
        response['pretrade_review'] = position_analyzer.review_trades(decision,
            dict(model=model, input=json.dumps(review_input, ensure_ascii=False)), reviewer or default_review)
        response['daily_participation']['remaining_without_intent'] = [
            side for side in response['daily_participation']['before']['needed']
            if response['daily_participation'].get('enabled')
            and not any(d['decision'] == side for d in decision['decisions'])]
        remaining = response['daily_participation']['remaining_without_intent']
        if remaining:
            decision['market_view'] += '；每日參與尚缺動作：' + '、'.join(remaining) + '（請查看試單資格與交易前檢查原因）。'
        response['validation'] = 'VALID'
    except Exception as exc:
        response.update(validation='ERROR')
        response['failure_kind']='CODEX_QUOTA_EXHAUSTED' if 'CODEX_QUOTA_EXHAUSTED' in str(exc) else 'MODEL_OR_TOOL_ERROR'
        response['api']['error'] = redact(f'{type(exc).__name__}: {exc}')
        decision = dict(market_view='Agent 決策失敗，本輪全部 WAIT。',decisions=[dict(
            symbol=c['symbol'],decision='WAIT',confidence=0,action_ratio=0,suggested_qty=0,
            reference_price=c.get('last_price'),suggested_price=None,strategy_id='NONE',
            target_price=None,invalidation_price=None,holding_days=None,reason='AGENT_FAILED_WAIT',
            data_quality=c['quote_status'],warnings=['不是即時行情'] if c['quote_status']=='LAST_KNOWN' else [])
            for c in context.candidates.values()])
    if any(tool.get('type') == 'web_search' for tool in context.search_request.get('tools', [])):
        from codex_cli import metadata
        response['api']['web_search'] = search_audit({'output': context.search_output, 'metadata': metadata()})
    if context.usage_requests or getattr(context, 'usage_incomplete', False):
        response['api']['usage_requests'] = context.usage_requests
        response['api']['usage_accounting_complete'] = not getattr(context, 'usage_incomplete', False)
        if 'usage' not in response['api'] and context.usage_requests:
            response['api']['usage'] = {key: sum(row[key] for row in context.usage_requests)
                for key in ('input_tokens', 'output_tokens', 'requests', 'cached_input_tokens',
                            'cache_write_tokens', 'reasoning_tokens')}
    response.update(response_received_at=now().isoformat(),tool_calls=context.calls)
    if context.support is not None:
        response['support_research'] = context.support.summary()
    from runtime_skills import entry_research_report
    research = entry_research_report(context,decision,response)
    if research is not None:
        response['entry_research'] = research
        save_json(context.log_path.with_name('entry_research.json'),research)
    save_json(context.log_path.with_name('agent_response.json'),response)
    return decision,response
