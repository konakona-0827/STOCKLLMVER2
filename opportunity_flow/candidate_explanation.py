"""Turn saved FLOW evidence into explanations without changing eligibility."""
from .common import stamp


REASONS = {
    'ODDLOT_BID_MISSING': ('券商尚未回傳可用買一價。', '等買賣盤欄位補齊後重新核對；目前不形成可執行買單。'),
    'ODDLOT_ASK_MISSING': ('券商尚未回傳可用賣一價，無法核對目前可買進的報價。', '等有效賣一價；缺價本身不足以判定券商故障或漲停。'),
    'ODDLOT_LAST_PRICE_MISSING': ('券商尚未回傳可核對的最新成交價。', '等非試撮成交及完整行情補齊。'),
    'LIVE_INTRADAY_PLAN_DATA_REQUIRED': ('已找到初步路徑，正在補齊券商即時零股報價與當日低點。', '取得行情後重算有效失效價及股數，再交兩位代理敲定計畫。'),
    'COMPLETED_CALENDAR_MISSING': ('尚無可核對的交易日資料。', '等交易日曆與完成日線補齊。'),
    'CALENDAR_COVERAGE_NOT_CURRENT': ('交易日曆尚未更新到所需日期。', '等官方日曆更新後重算。'),
    'INSUFFICIENT_COMPLETED_HISTORY': ('已完成的日 K 筆數不足。', '等日線補齊至策略要求的筆數。'),
    'RECENT_HISTORY_GAP_OR_INVALID_BAR': ('近期有日 K 缺漏或數值無法驗證。', '等缺漏補齊或來源更正後重算。'),
    'STALE_HISTORY_OR_EXCHANGE_CHANGED': ('日線日期或上市櫃市場有變動，現有資料無法接續。', '等最新交易日與正確市場資料核對完成。'),
    'LATEST_SESSION_HAS_NO_TRADES': ('最近一個已完成交易日沒有成交。', '等有成交的新日線，再評估型態與流動性。'),
    'LIQUIDITY_UNKNOWN_OR_BELOW_FLOOR': ('近 20 日流動性資料未達篩選要求，或資料不足。', '等新日線補齊，並重新核對成交股數、成交金額與波動。'),
    'NO_CURRENT_SETUP': ('目前沒有符合規則的趨勢、回檔、回升或突破型態。', '等新日線或盤中價格形成合格型態。'),
    'PENDING_BROKER_ORDER': ('這檔仍有券商委託待核對。', '等券商確認成交、撤單或到期，並完成帳本對帳。'),
    'AI_TRADING_DISABLED': ('這檔的 AI 交易已停用。', '啟用此標的的 AI 交易後，再依新資料評估。'),
    'EXISTING_AI_COST_UNKNOWN': ('既有 AI 持股成本未能核實。', '等持股成本核對完成，再評估加碼。'),
    'EXISTING_POSITION_RISK_UNKNOWN': ('現有持股的合計風險無法核實。', '等成本、有效零股報價與失效價足夠重算風險。'),
    'BUY_LOSING_POSITION_ADD_BLOCKED': ('既有持股處於虧損，規則限制加碼。', '等最新行情與持倉重新符合加碼條件。'),
    'BUY_REENTRY_COOLDOWN': ('這檔仍在重新進場的冷卻期。', '等冷卻期結束，再用最新資料評估。'),
    'BUY_SESSION_UNKNOWN': ('尚未核實這檔的交易日期或紀錄。', '等交易紀錄核對完成。'),
    'STRUCTURAL_STOP_MISSING': ('找不到距入場價合理、可核對的失效位置。', '等新支撐形成，或價差變動後出現合格失效價。'),
    'SESSION_STRUCTURAL_STOP_BREACHED': ('既有失效位置已被今天低點碰到或跌破，尚無符合風險距離的替代位置。', '用未被今天低點跌破的支撐重算股數、費後收益與風險。'),
    'REVIEWED_PLAN_INVALIDATED_THIS_SESSION': ('研究後取得的當日低點已碰到或跌破原計畫失效價。', '依最新日內低點重新制定失效位置與股數，再交兩位代理審查。'),
    'QUOTE_SESSION_RANGE_INVALID': ('券商日內高低價的範圍無法核實。', '等券商重新回傳有效的日內高低價。'),
    'OBSERVED_TARGET_MISSING': ('入場價上方缺少已觀測的目標價。', '等新價格結構提供可核對的上方目標。'),
    'CAPITAL_OR_RISK_CAP': ('目前找不到同時符合資金與損失風險限制的股數。', '等資金、入場位置或風險距離改變後重算。'),
    'NET_RETURN_BELOW_FLOOR': ('試算的費後報酬率偏低。', '等入場價、價差或目標變動，使費後報酬率達標。'),
    'REWARD_RISK_BELOW_FLOOR': ('費後潛在收益相對於失效損失偏低。', '等費後收益相對失效損失的比例達標。'),
    'NET_PROFIT_BELOW_FLOOR': ('試算的費後淨利偏低。', '等入場價、價差或目標變動，使可用股數的費後淨利達標。'),
    'ODDLOT_QUOTE_MISSING': ('這輪未取得券商零股即時報價。', '等券商回傳有效且非試撮的零股買賣價。'),
    'LIVE_NON_TRIAL_QUOTE_REQUIRED': ('這輪取得的行情未達即時、非試撮的零股要求。', '等券商回傳有效且非試撮的零股買賣價。'),
    'VERIFIED_ODDLOT_QUOTE_BASIS_REQUIRED': ('這輪報價來源未能核實為券商盤中零股。', '等取得可核實的券商零股報價。'),
    'QUOTE_SYMBOL_OR_EXCHANGE_MISMATCH': ('行情的代號或上市櫃市場不符。', '等正確市場的零股行情重新核對。'),
    'QUOTE_STALE_OR_FUTURE': ('這輪零股報價的日期或時間不符合要求。', '等當日的新鮮零股報價。'),
    'QUOTE_STALE_AFTER_REVIEW': ('研究完成時，零股報價已過期。', '重新取價並用最新價差核對原計畫。'),
    'CROSSED_QUOTE': ('券商回傳的買一高於賣一，價差無法使用。', '等買賣價重新核對。'),
    'FROZEN_PLAN_ECONOMICS_CHANGED_REQUIRES_REVIEW': ('刷新行情後，原計畫的費後收益或風險已不符。', '用新行情重新驗證計畫。'),
    'COMPANY_EVENTS_AND_TRADING_STATUS_NOT_CHECKED': ('研究資料仍缺少完整的公司事件與交易狀態核對。', '等公司事件與交易狀態更新後再核對。'),
    'COMPANY_EVENT_CHANGED_REQUIRES_REVIEW': ('下單前的公司事件資料與研究時不同。', '依最新公司事件重新研究原計畫。'),
    'CORPORATE_ACTIONS_NOT_VERIFIED': ('研究卡的歷史價格尚未完成公司行動驗證。', ''),
    'EVENT_CHECK_STALE_OR_FUTURE': ('公司事件查核時間已過期或異常。', '等公司事件查核更新。'),
    'EVENT_CHECK_TIME_INVALID': ('公司事件查核時間無法辨識。', '等公司事件資料重新取得。'),
}

STATE_TEXT = {
    'REVIEW_DEFERRED_EVENT_RISK': ('已知公司事件使原價位計畫暫不可用。', '等事件價格基礎釐清並重建合格計畫；新分鐘行情不會單獨解除此阻擋。'),
    'REVIEW_DEFERRED_LIVE_QUOTE': ('初步機會已列入券商取價佇列。', '等即時零股報價與當日低點核對後，進入正式計畫研究。'),
    'REVIEW_PENDING': ('數值條件已通過，等待研究員與反證員。', '等兩位代理完成對同一計畫的審查。'),
    'REVIEW_IN_FLIGHT': ('這檔已交給代理研究。', '等兩份意見完成，再核對是否同意同一條路徑。'),
    'RESEARCH_WATCH': ('至少一位代理認為證據仍不足，先觀察。', '等研究意見所列的條件出現後再評估。'),
    'RESEARCH_REJECT': ('至少一位代理提出反證，目前研究未通過。', '等新證據能解除反證後再評估。'),
    'PLAN_DISAGREEMENT': ('兩位代理選了不同的進場計畫。', '等新的研究證據足以讓雙方對同一計畫達成一致。'),
    'RESEARCH_READY': ('兩位代理已同意同一份研究計畫。', '接續核對最新券商行情、帳戶與下單前風控。'),
    'REVIEW_ERROR': ('這輪模型研究未完成。', '等模型服務恢復並完成重試。'),
    'REVIEW_ERROR_BACKOFF': ('上一輪模型研究出錯，正在等待重試。', '等重試間隔結束後再研究。'),
    'REVIEW_SERVICE_BACKOFF': ('模型服務暫時無法完成研究。', '等重試間隔結束後再研究。'),
    'REVIEW_DEFERRED_SERVICE_ERROR': ('模型服務出錯，本檔研究延後。', '等服務恢復後再排入研究。'),
}

ROUTE_METRICS = {
    'NET_RETURN_BELOW_FLOOR': ('net_return', 'minimum_net_return', '費後報酬', '{:.2%}'),
    'REWARD_RISK_BELOW_FLOOR': ('reward_risk', 'minimum_reward_risk', '報酬風險比', '{:.2f} 倍'),
    'NET_PROFIT_BELOW_FLOOR': ('net_profit_twd', 'minimum_net_profit_twd', '費後淨利', 'NT$ {:,.2f}'),
}


def display_plan(candidate):
    """Use the reviewed route when selected, otherwise the current scan's first eligible example."""
    review = candidate.get('review') or {}
    selected = candidate.get('selected_route_id')
    packet = (review.get('packet') if selected else None) or candidate.get('packet') or review.get('packet') or {}
    routes = next((e['facts'] for e in packet.get('evidence', []) if e.get('evidence_id') == 'E_ECONOMICS'), [])
    if not routes:
        return packet, {}, False
    chosen = next((r for r in routes if r.get('route_id') == selected and selected), None)
    return packet, chosen or next((r for r in routes if r.get('eligible')), routes[0]), bool(chosen)


def _display_reason(code, route, policy):
    reason, wait = REASONS.get(code, ('這輪有其他檢查未通過，請查看研究卡原始代碼。', '等相關資料更新後重算。'))
    if code in ROUTE_METRICS and code in route.get('blockers', []):
        value, minimum, title, fmt = ROUTE_METRICS[code]
        actual = (route.get('economics') or {}).get(value)
        threshold = policy.get(minimum)
        if isinstance(actual, (int, float)) and isinstance(threshold, (int, float)):
            basis = '以 1 股試算，' if 'CAPITAL_OR_RISK_CAP' in route.get('blockers', []) else ''
            reason = f'{basis}{title} {fmt.format(actual)}，低於門檻 {fmt.format(threshold)}。'
            wait = f'等入場價、目標或費用變動後重算，{title}須達 {fmt.format(threshold)}。'
    return reason, wait


def _price(value):
    return f'{value:g} 元' if isinstance(value, (int, float)) else '未記錄'


def explain(candidate, route, policy):
    """Return current situation and next conditions as separate, ordered sentences."""
    state = candidate.get('state')
    reasons, waiting = [], []
    if state in STATE_TEXT:
        reason, wait = STATE_TEXT[state]
        reasons.append(reason)
        waiting.append(wait)
    if state=='REVIEW_DEFERRED_LIVE_QUOTE' and candidate.get('quote_problem') in REASONS:
        reason,wait=REASONS[candidate['quote_problem']]
        reasons.append(reason);waiting.append(wait)
    codes = candidate.get('reasons', [])
    codes = list(dict.fromkeys([*route.get('blockers', []), *codes])) if state == 'ECONOMIC_BLOCKED' else codes
    for code in codes:
        reason, wait = _display_reason(code, route, policy)
        if state not in STATE_TEXT or code in REASONS:
            reasons.append(reason)
            waiting.append(wait)

    review = candidate.get('review') or {}
    if state in ('RESEARCH_WATCH', 'RESEARCH_REJECT', 'PLAN_DISAGREEMENT'):
        opinions = [review.get(role) or {} for role in ('researcher', 'challenger')]
        verdict = {'RESEARCH_WATCH': 'WATCH', 'RESEARCH_REJECT': 'REJECT'}.get(state)
        relevant = [opinion for opinion in opinions if not verdict or opinion.get('verdict') == verdict]
        agent_waits = [condition for opinion in relevant for condition in opinion.get('wait_conditions', [])]
        if agent_waits:
            waiting = agent_waits
        if verdict:
            thesis = next((opinion['thesis'] for opinion in relevant if opinion.get('thesis')), None)
            if thesis:
                reasons.insert(0, thesis)

    execution = candidate.get('execution') or {}
    stage = execution.get('state')
    if stage == 'WAITING_FOR_PLANNED_ENTRY':
        reasons.insert(0, '研究已通過，當時賣一仍高於計畫入場價。')
        waiting.insert(0, f"觀察賣一是否回落到 {_price(route.get('entry'))} 或以下，再核對最新計畫。")
    elif stage == 'INVALIDATED':
        if execution.get('reason')=='REVIEWED_PLAN_INVALIDATED_THIS_SESSION':
            reasons.insert(0, f"當日最低價已到達或跌破失效價 {_price((route.get('stop') or {}).get('price'))}。")
        else:
            reasons.insert(0, f"當時買一已到達或跌破失效價 {_price((route.get('stop') or {}).get('price'))}。")
        waiting = ['依最新日內低點與有效支撐重算計畫。']
    elif stage == 'TARGET_ALREADY_REACHED':
        reasons.insert(0, f"當時賣一已到達或超過目標價 {_price((route.get('target') or {}).get('price'))}。")
        waiting = ['等新的入場位置與費後空間出現，再評估新計畫。']
    elif state == 'RESEARCH_READY':
        for code in execution.get('missing', []):
            if code in REASONS:
                reason, wait = _display_reason(code, route, policy)
                reasons.append(reason)
                waiting.append(wait)
        if stage == 'EXECUTION_DATA_NEEDED' and not any(code in REASONS for code in execution.get('missing', [])):
            reasons.append('初步執行檢查仍有待核對項目，詳見原始代碼。')

    if state in ('REVIEW_PENDING', 'RESEARCH_WATCH', 'RESEARCH_REJECT') and candidate.get('quote_problem') in REASONS:
        reason, wait = REASONS[candidate['quote_problem']]
        reasons.append(reason)
        waiting.append(wait)
    return (list(dict.fromkeys(reasons)) or ['這輪尚未記錄明確原因。'],
            list(dict.fromkeys(w for w in waiting if w)) or ['等下一輪資料更新。'])


def research_card(symbol, name, label, candidate):
    packet, route, chosen = display_plan(candidate)
    reasons, waiting = explain(candidate, route, packet.get('policy') or {})
    try:
        at = stamp(candidate.get('evaluated_at')).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        at = '未記錄'
    sections = [f'{symbol} {name}｜{label}', f'評估時間（台灣時間） {at}',
                '目前情況\n' + '\n'.join(reasons),
                '重新評估條件\n' + '\n'.join(f'• {line}' for line in waiting)]
    information=candidate.get('information_availability') or {}
    trace=information.get('oddlot_quote') or {}
    details=[]
    labels={'bid':'買一','ask':'賣一','last_price':'成交價','low':'日內低點'}
    for field,row in trace.get('fields',{}).items():
        origin=row.get('origin')
        if origin=='PRESENT':continue
        explanation={'SYSTEM_TRANSFORM_LOSS':'券商原始值存在，系統轉換遺失，屬程式缺失',
            'PROVIDER_EMPTY_FIELD':'券商原始欄位為空值／零值，尚不能認定市場沒有資料',
            'SOURCE_NO_RESPONSE':'本次已請求，但來源沒有有效回覆',
            'SOURCE_REQUEST_OR_VALIDATION_ERROR':'來源請求或資料核對失敗',
            'SOURCE_VALIDATION_REJECTED':'原始價格存在，但時間或資料身分驗證未通過',
            'SOURCE_TRACE_UNAVAILABLE':'尚無原始欄位證據，無法歸因為市場缺資料',
            'PROVIDER_FIELD_UNAVAILABLE':'券商回覆沒有可核對的欄位'}.get(origin,origin)
        details.append(f"{labels.get(field,field)}：{explanation}。")
    history=information.get('intraday_history') or {}
    if history:
        details.append(f"已交研究：{history.get('observation_count',0)} 筆盤中快照、{history.get('ticks_delivered',0)} 筆成交明細、{history.get('minute_bars_delivered',0)} 根已結束分鐘彙整；未認證完整連續成交。")
        if history.get('origin')=='SYSTEM_NOT_REQUESTED':details.append('分鐘成交資料本次尚未由系統取得，不能解讀為市場沒有資料。')
        elif history.get('origin')=='SOURCE_RESPONSE_INCOMPLETE':details.append('已向券商請求，但尚未收到足夠的有效成交明細，不能認定市場沒有資料。')
        elif str(history.get('origin','')).startswith('SYSTEM_'):details.append('分鐘資料有系統取得或解析缺口，需修復後重查，不能當成一般市場觀望。')
    if details:sections.append('資料來源與缺口\n'+'\n'.join(details))
    if route:
        title = '雙方選定的研究計畫' if chosen else '本輪試算範例'
        qty = (route.get('economics') or {}).get('quantity')
        quantity = '無合格股數' if 'CAPITAL_OR_RISK_CAP' in route.get('blockers', []) else f'{qty} 股' if qty else '未算出'
        sections.append(f"{title}\n入場 {_price(route.get('entry'))}｜失效 {_price((route.get('stop') or {}).get('price'))}"
                        f"｜目標 {_price((route.get('target') or {}).get('price'))}｜{quantity}")
        if candidate.get('hypothetical_reference'):
            sections[-1] += '\n這組價位以參考行情試算，送單前還須核對券商即時零股報價。'
    review = candidate.get('review') or {}
    for role, title in (('researcher', '研究員'), ('challenger', '反證員')):
        opinion = review.get(role) or {}
        if opinion:
            verdict = {'RESEARCH': '研究通過', 'WATCH': '觀察', 'REJECT': '否決'}.get(opinion.get('verdict'), '未記錄')
            lines = [f'{title}｜{verdict}']
            if opinion.get('thesis'):lines.append(opinion['thesis'])
            if opinion.get('counterevidence'):lines.append(f"反證　{opinion['counterevidence']}")
            if opinion.get('wait_conditions'):lines.extend(f'等候　{line}' for line in opinion['wait_conditions'])
            if opinion.get('execution_missing'):lines.extend(f'待核對　{line}' for line in opinion['execution_missing'])
            sections.append('\n'.join(lines))
    execution = candidate.get('execution') or {}
    codes = [*candidate.get('reasons', []), *execution.get('missing', [])]
    if candidate.get('quote_problem'):codes.append(candidate['quote_problem'])
    if codes:sections.append('原始檢查紀錄\n' + '\n'.join(dict.fromkeys(codes)))
    return '\n\n'.join(sections)
