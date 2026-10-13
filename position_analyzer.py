"""Union-wide structured advice. No execution or position mutation."""
import json
import math
import os
from decimal import Decimal, ROUND_FLOOR
from config import ROOT, now, redact
from market_calendar import trading_day_context
from candidate_analyzer import object_schema, call_openai
from paper_portfolio import MAX_ODDLOT_QTY, fee_twd, price_cents
from strategy_evidence import STRATEGY_IDS
from trade_economics import evaluate_buy
from web_research import enable_search

ITEM = object_schema(dict(
    symbol={'type': 'string'}, decision={'type': 'string', 'enum': ['BUY', 'HOLD', 'SELL', 'WAIT']},
    confidence={'type': 'number', 'minimum': 0, 'maximum': 1},
    action_ratio={'type': 'number', 'minimum': 0, 'maximum': 1},
    suggested_qty={'type': ['integer', 'null'], 'minimum': 0},
    reference_price={'type': ['number', 'null']}, suggested_price={'type': ['number', 'null']},
    strategy_id={'type': 'string', 'enum': ['NONE', *STRATEGY_IDS]},
    target_price={'type': ['number', 'null']},
    invalidation_price={'type': ['number', 'null']},
    holding_days={'type': ['integer', 'null'], 'minimum': 1, 'maximum': 22},
    reason={'type': 'string'}, data_quality={'type': 'string', 'enum': ['LIVE', 'LAST_KNOWN', 'UNAVAILABLE']},
    warnings={'type': 'array', 'items': {'type': 'string'}}))
SCHEMA = object_schema(dict(market_view={'type': 'string'}, decisions={'type': 'array', 'items': ITEM}))


def sell_qty(qty, ratio):
    return int((Decimal(qty)*Decimal(str(ratio))).to_integral_value(rounding=ROUND_FLOOR))


def build_request(candidates, paper_account=None, strategy_prompt=None, model=None):
    source = str((paper_account or {}).get('source', ''))
    live_account = source.startswith('FRESH_BROKER_BUYING_POWER_AFTER_')
    matched_rules = live_account or source == 'SIMULATION_MATCHED_LIVE_RULES'
    if matched_rules:
        from execution.auto_advice_executor import LiveExecutionConfig
        live_config = LiveExecutionConfig.load(ROOT/'config'/'execution_live.json')
    instructions = (strategy_prompt if strategy_prompt is not None else
                    (ROOT/'strategies'/'prompt_positions.md').read_text(encoding='utf-8'))
    instructions += ('\n\n交易日資料判讀：連續日 K 指連續「交易日」，週末與證交所休市日不構成缺口。'
                     '讀取輸入的 trading_calendar.previous_trading_date 與 skipped_closed_dates；'
                     '不可因前一曆日休市，就說缺少前一交易日資料。'
                     'market_history 是盤中快照，不是完整日 K；若實際沒有足夠的多交易日 OHLC 與成交量，'
                     '仍須明確說明跨日驗證不足，不可憑休市理由虛構日 K、量比或均線。'
                     'history_status=INTRADAY_SNAPSHOTS_ONLY 只描述資料種類，不是禁止 BUY 的條件。'
                     '不得僅因沒有日 K 就一律 WAIT；若當前 LIVE 行情的開高低現價、買賣價差與成交量、'
                     '具體進場價、失效條件、交易成本和預算共同支持交易，可提出較低信心度及保守股數的 BUY。'
                     '有持倉時，亦不得僅因缺少多日 K 而自動 HOLD 或阻止有即時風控依據的 SELL。'
                     '若證據仍不足，就說明缺少哪項決策依據並 WAIT 或 HOLD；不可把盤中量宣稱為跨日放量。')
    instructions += ('\n\n每檔有 eligible_strategies 與 strategy_evidence。BUY 的 strategy_id 必須從該檔'
                     ' eligible_strategies 選，並填目標價 target_price、失效價 invalidation_price、'
                     '預計交易日 holding_days（1 至 22）；非 BUY 填 strategy_id=NONE，三個欄位填 null。'
                     '策略可用只代表有資料可評估，不代表買點成立。盤中快照不能當完整日 K。'
                     'BUY 的目標價須在保守計入雙邊手續費、股票賣出 0.3% 或 ETF 0.1% 交易稅、'
                     '目前買賣價差及下行失效風險後，仍有正淨收益與合理報酬風險比。'
                     '不可只因缺少跨日資料就放棄其他有證據的策略。')
    request = dict(model=model or os.getenv('OPENAI_MODEL', 'gpt-5.6-luna'), store=False,
                instructions=instructions,
                input=json.dumps(dict(purpose='ADVICE_FOR_DETERMINISTIC_LIVE_EXECUTION' if live_account else 'ADVICE_ONLY_NO_ORDERS', analysis_set=candidates,
                                     trading_calendar=trading_day_context(now().date()),
                                     paper_account=paper_account,
                                     account_source='FRESH_BROKER_CHECKED' if live_account else 'PAPER',
                                     paper_simulation_rules=dict(
                                         commission_rate=0.001425,
                                         commission_rounding='floor_to_whole_TWD',
                                         minimum_commission_twd=float(live_config.min_buy_fee_reserve_twd) if matched_rules else 1,
                                         buy_cash_buffer_rate=float(live_config.buy_cash_buffer_rate) if matched_rules else 0,
                                         max_oddlot_qty_per_trade=MAX_ODDLOT_QTY,
                                         fill_price='candidate.last_price; LIVE quotes only'),
                                     available_buy_budget_twd=(paper_account or {}).get('available_cash_twd')),
                                 ensure_ascii=False, allow_nan=False),
                text={'format': {'type': 'json_schema', 'name': 'position_advice', 'strict': True, 'schema': SCHEMA}},
                max_output_tokens=min(24000, max(6000, len(candidates)*650)))
    return enable_search(request)


def validate(result, candidates, available_cash_cents=None, minimum_buy_fee_twd=1,
             buy_cash_buffer_rate=0, commission_rate='0.001425'):
    if not isinstance(result, dict) or set(result) != set(SCHEMA['required']) or not isinstance(result['market_view'], str) or not result['market_view'].strip():
        raise ValueError('INVALID_ROOT')
    if not isinstance(result['decisions'], list):
        raise ValueError('INVALID_DECISIONS')
    lookup, seen = {c['symbol']: c for c in candidates}, set()
    planned_buy_cost_cents = 0
    for d in result['decisions']:
        if not isinstance(d, dict) or set(d) != set(ITEM['required']):
            raise ValueError('INVALID_FIELDS')
        s = d['symbol']
        if not isinstance(s, str) or s not in lookup or s in seen:
            raise ValueError('UNKNOWN_OR_DUPLICATE_SYMBOL')
        seen.add(s)
        c = lookup[s]
        paper_qty = c.get('paper_position', {}).get('qty')
        qty = paper_qty if paper_qty is not None else c['position']['ai_managed_qty']
        if d['decision'] not in ('BUY', 'HOLD', 'SELL', 'WAIT') or (qty == 0 and d['decision'] in ('HOLD', 'SELL')):
            raise ValueError('INVALID_POSITION_DECISION')
        if d['decision'] in ('BUY', 'SELL') and (c['quote_status'] != 'LIVE' or c.get('is_trial')):
            raise ValueError('SIMULATED_TRADE_REQUIRES_LIVE_QUOTE')
        for key in ('confidence', 'action_ratio'):
            if type(d[key]) not in (int, float) or not math.isfinite(d[key]) or not 0 <= d[key] <= 1:
                raise ValueError('INVALID_' + key)
        if d['suggested_qty'] is not None and (type(d['suggested_qty']) is not int or d['suggested_qty'] < 0):
            raise ValueError('INVALID_QTY')
        if d['decision'] == 'SELL':
            expected = sell_qty(qty, d['action_ratio'])
            if expected <= 0 or expected > MAX_ODDLOT_QTY or d['suggested_qty'] != expected:
                raise ValueError('SELL_REJECTED_LOCAL_OWNERSHIP_OR_RATIO')
        elif d['decision'] == 'BUY':
            if ('max_buy_qty' in c and
                    (type(d['suggested_qty']) is not int or d['suggested_qty'] < 1 or
                     d['suggested_qty'] > min(MAX_ODDLOT_QTY, c['max_buy_qty']))):
                raise ValueError('BUY_EXCEEDS_AVAILABLE_CASH')
            if 'max_buy_qty' not in c and d['suggested_qty'] is not None:
                raise ValueError('INVALID_BUY_QUANTITY')
        elif d['action_ratio'] != 0 or d['suggested_qty'] != 0 or d['suggested_price'] is not None:
            raise ValueError('NON_ACTION_QUANTITY')
        if d['decision'] != 'BUY' and (d['strategy_id'] != 'NONE' or
                any(d[key] is not None for key in ('target_price', 'invalidation_price', 'holding_days'))):
            raise ValueError('NON_BUY_TRADE_PLAN')
        ref = d['reference_price']
        if c['last_price'] is None:
            if ref is not None:
                raise ValueError('INVENTED_REFERENCE')
        elif type(ref) not in (int, float) or not math.isfinite(ref) or abs(ref-c['last_price']) > 1e-8:
            raise ValueError('INCORRECT_REFERENCE')
        price = d['suggested_price']
        if price is not None and (type(price) not in (int, float) or not math.isfinite(price) or price <= 0):
            raise ValueError('INVALID_PRICE')
        if d['data_quality'] != c['quote_status'] or (c['quote_status'] == 'UNAVAILABLE' and d['decision'] != 'WAIT'):
            raise ValueError('INVALID_DATA_QUALITY_DECISION')
        if c.get('is_trial') and d['decision'] != 'WAIT':
            raise ValueError('TRIAL_QUOTE_MUST_WAIT')
        if not isinstance(d['reason'], str) or not d['reason'].strip() or not isinstance(d['warnings'], list) or any(not isinstance(w, str) for w in d['warnings']):
            raise ValueError('INVALID_EXPLANATION')
        if d['decision'] == 'BUY' and d['action_ratio'] != 0:
            # BUY quantity is explicit. A nonzero ratio has no execution meaning;
            # normalize it only after the quantity and price checks have passed.
            d['action_ratio'] = 0
            d['warnings'].append('BUY 的 action_ratio 已忽略；股數以 suggested_qty 與風控檢查為準。')
        if c['quote_status'] == 'LAST_KNOWN' and not any('不是即時行情' in w for w in d['warnings']):
            raise ValueError('MISSING_STALE_WARNING')
        if d['decision'] == 'BUY' and not any('預算' in w for w in d['warnings']):
            # The numeric budget checks above are authoritative. Add the
            # missing disclosure from the checked amount rather than turning
            # every recommendation into ANALYSIS_FAILED_WAIT for prose alone.
            budget = ('未知' if available_cash_cents is None
                      else str(Decimal(available_cash_cents) / 100))
            d['warnings'].append(f'預算：本輪可用買進上限 NT$ {budget}；實際委託仍由券商資金風控重查。')
        if d['decision'] == 'BUY':
            plan_error = None
            economics = None
            if d['strategy_id'] not in STRATEGY_IDS or d['strategy_id'] not in c.get('eligible_strategies', []):
                plan_error = 'BUY_STRATEGY_EVIDENCE_UNAVAILABLE'
            elif type(d['holding_days']) is not int or not 1 <= d['holding_days'] <= 22:
                plan_error = 'INVALID_BUY_HOLDING_HORIZON'
            else:
                try:
                    economics = evaluate_buy(c, qty=d['suggested_qty'],
                                             limit_price=d['suggested_price'],
                                             target_price=d['target_price'],
                                             invalidation_price=d['invalidation_price'],
                                             minimum_commission_twd=minimum_buy_fee_twd,
                                             commission_rate=commission_rate)
                    if economics['estimated_net_upside_twd'] <= 0 or (
                            economics['reward_risk_ratio'] is None or economics['reward_risk_ratio'] < 1):
                        plan_error = 'BUY_NET_UPSIDE_OR_RISK_REWARD_INSUFFICIENT'
                except (ValueError, TypeError) as exc:
                    plan_error = str(exc)
            if plan_error:
                # A bad plan affects this symbol only. Keep valid opportunities
                # from other evidence paths in the same response.
                d.update(decision='WAIT', confidence=0, action_ratio=0,
                         suggested_qty=0, suggested_price=None, strategy_id='NONE',
                         target_price=None, invalidation_price=None, holding_days=None)
                d['reason'] += f'；本檔買進計畫未通過獨立檢查：{plan_error}'
                d['warnings'].append(plan_error)
            else:
                d['warnings'].append(
                    f"目標情境淨利估算 NT$ {economics['estimated_net_upside_twd']:.2f}；"
                    f"失效情境淨損估算 NT$ {economics['estimated_net_downside_twd']:.2f}；"
                    '未含無法驗證的未來成交滑價，實際報酬不保證。')
                if available_cash_cents is not None:
                    entry_price = max(c.get('budget_price') or c.get('ask') or c['last_price'],
                                      d['suggested_price'] or 0)
                    gross_cents = price_cents(entry_price) * d['suggested_qty']
                    reserve_cents = max(Decimal(str(minimum_buy_fee_twd)) * 100,
                                        Decimal(gross_cents) * Decimal(str(buy_cash_buffer_rate)),
                                        Decimal(fee_twd(gross_cents) * 100))
                    planned_buy_cost_cents += gross_cents + reserve_cents
    if seen != set(lookup):
        raise ValueError('MISSING_ANALYSIS_SYMBOL')
    if available_cash_cents is not None and planned_buy_cost_cents > available_cash_cents:
        raise ValueError('AGGREGATE_BUY_COST_EXCEEDS_AVAILABLE_CASH')
    return result


def analyze(candidates, request, provider=call_openai):
    started, raw, api = now().isoformat(), '', {}
    try:
        raw, api = provider(request)
        request_input = json.loads(request.get('input', '{}'))
        budget_twd = request_input.get('available_buy_budget_twd')
        budget_cents = int(Decimal(str(budget_twd)) * 100) if budget_twd is not None else None
        rules = request_input.get('paper_simulation_rules') or {}
        result = validate(json.loads(raw), candidates, budget_cents,
                          minimum_buy_fee_twd=rules.get('minimum_commission_twd', 1),
                          buy_cash_buffer_rate=rules.get('buy_cash_buffer_rate', 0),
                          commission_rate=rules.get('commission_rate', 0.001425))
        validation = 'VALID'
    except Exception as exc:
        api['error'] = redact(f'{type(exc).__name__}: {exc}')
        validation = 'ERROR'
        result = dict(market_view='分析失敗，本輪全部 WAIT。', decisions=[dict(
            symbol=c['symbol'], decision='WAIT', confidence=0, action_ratio=0, suggested_qty=0,
            reference_price=c['last_price'], suggested_price=None, strategy_id='NONE',
            target_price=None, invalidation_price=None, holding_days=None,
            reason='ANALYSIS_FAILED_WAIT',
            data_quality=c['quote_status'], warnings=['ANALYSIS_FAILED'] +
            (['不是即時行情'] if c['quote_status']=='LAST_KNOWN' else [])) for c in candidates])
    names = {c['symbol']: c.get('name') or '股票名稱未取得' for c in candidates}
    for item in result['decisions']:
        name = names.get(item['symbol'], '股票名稱未取得')
        prefix = f"{name}（{item['symbol']}）："
        if not item['reason'].startswith(prefix):
            item['reason'] = prefix + item['reason']
    return result, dict(request_started_at=started, response_received_at=now().isoformat(),
                        validation=validation, raw_response=redact(raw), api=api)
