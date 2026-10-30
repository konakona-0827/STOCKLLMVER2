"""Saved current-vintage signal evaluation, not point-in-time portfolio returns."""
from dataclasses import replace
from collections import Counter, defaultdict
from statistics import fmean
from html import escape

from .contracts import DataError, calendar_at, load_series, timestamp, digest
from .patterns import load_registry, HANDLERS
from .opportunity import load_policy, _reference_plan


def replay_trade(plan, future, horizon):
    if type(horizon) is not int or horizon not in (3,5):raise DataError("INVALID_REPLAY_HORIZON")
    base={"holding_sessions":horizon,"status":"NO_FUTURE_DATA","gross_return":None,"cost_adjusted_return":None,
        "entry_reference":None,"exit_reference":None,"days_held":None,"ambiguous":False}
    if not future:return base
    first=future[0];entry=first.open
    trigger=plan.get("entry_trigger_reference")
    if entry>plan["entry_ceiling_reference"]:
        return {**base,"status":"NOT_FILLED_GAP_ABOVE_ENTRY_BAND"}
    if trigger and entry<trigger:
        return {**base,"status":"INTRADAY_TRIGGER_TOUCHED_FILL_UNVERIFIED" if first.high>=trigger else "TRIGGER_NOT_REACHED"}
    stop=entry*(1-plan["stop_price_fraction"])
    target=entry*(1+plan["target_return_fraction"])
    if plan["target_basis"]=="NET_AFTER_COST":
        target=entry*(1+plan["entry_cost_fraction"])*(1+plan["target_return_fraction"])/(1-plan["exit_cost_fraction"])
    status,exit_price,days,ambiguous=None,None,None,False
    for i,bar in enumerate(future[:horizon]):
        if i and bar.open<=stop:
            status,exit_price,days="STOP_GAP",bar.open,i+1;break
        if bar.low<=stop and bar.high>=target:
            status,exit_price,days,ambiguous="AMBIGUOUS_STOP_FIRST",stop,i+1,True;break
        if bar.low<=stop:
            status,exit_price,days="STOP",stop,i+1;break
        if bar.high>=target:
            status,exit_price,days="TARGET",target,i+1;break
    if status is None:
        if len(future)<horizon:return {**base,"status":"INCOMPLETE_HORIZON","entry_reference":entry}
        status,exit_price,days="TIME_EXIT",future[horizon-1].close,horizon
    gross=exit_price/entry-1
    net=exit_price*(1-plan["exit_cost_fraction"])/(entry*(1+plan["entry_cost_fraction"]))-1
    return {**base,"status":status,"gross_return":gross,"cost_adjusted_return":net,"entry_reference":entry,
        "exit_reference":exit_price,"days_held":days,"ambiguous":ambiguous,"repriced_target":target,"repriced_stop":stop,
        "entry_method":"NEXT_SESSION_OPEN_HYPOTHESIS_NOT_ACTUAL_FILL"}


def evaluate_saved(bundle, *, registry=None, policy=None, lookback_sessions=80):
    if type(lookback_sessions) is not int or not 10<=lookback_sessions<=250:raise DataError("INVALID_SHORT_EVALUATION_WINDOW")
    registry=load_registry(registry);policy=load_policy(policy)
    cutoff=timestamp(bundle["as_of"]);calendar=calendar_at(bundle,cutoff)
    rows,skipped,counts=[],[],Counter()
    recent_series={}
    definitions=[d for d in registry["definitions"] if d["handler"]=="short_cycle" and d["enabled"]]
    eligible=0
    for raw in bundle["securities"]:
        try:series=load_series(raw,cutoff,calendar,allow_observation=True,market="JP")
        except (DataError,KeyError,TypeError,ValueError) as exc:
            skipped.append({"symbol":raw.get("symbol"),"reason":str(exc)});continue
        if len(series.bars)<65:continue
        eligible+=1
        recent_series[series.symbol]=[{"session":b.session,"close":b.close} for b in series.bars[-15:]]
        for i in range(max(59,len(series.bars)-lookback_sessions),len(series.bars)):
            prefix=replace(series,bars=series.bars[:i+1])
            for d in definitions:
                pattern=HANDLERS[d["handler"]](prefix,cutoff,d,{})
                if not pattern["research_candidate"]:continue
                counts[d["id"]]+=1
                core={"technical":{"latest_close":prefix.bars[-1].close},"visible_series":{
                    "bars":[{"session":b.session,"high":b.high} for b in prefix.bars],"price_basis":series.price_basis}}
                plan=_reference_plan(core,pattern,policy)
                if not plan:continue
                for horizon in policy["short_horizon"]["holding_sessions"]:
                    outcome=replay_trade(plan,series.bars[i+1:i+1+horizon],horizon)
                    rows.append({"symbol":series.symbol,"signal_session":prefix.bars[-1].session,"pattern_id":d["id"],
                        "buy_type":pattern["buy_type"],"signal_input_digest":digest({"bars":[(b.session,b.open,b.high,b.low,b.close,b.volume) for b in prefix.bars],
                            "definition":d,"policy":policy}),"plan":plan,**outcome})
    groups=defaultdict(list)
    for row in rows:groups[(row["pattern_id"],row["holding_sessions"])].append(row)
    summary=[]
    for (pid,h),items in sorted(groups.items()):
        completed=[r for r in items if r["cost_adjusted_return"] is not None]
        summary.append({"pattern_id":pid,"holding_sessions":h,"signal_count":len(items),"completed_scenarios":len(completed),
            "status_counts":dict(Counter(r["status"] for r in items)),
            "target_hit_rate_stop_first":sum(r["status"]=="TARGET" for r in completed)/len(completed) if completed else None,
            "mean_price_return":fmean(r["gross_return"] for r in completed) if completed else None,
            "mean_cost_adjusted_return":fmean(r["cost_adjusted_return"] for r in completed) if completed else None,
            "meaning":"重疊訊號的描述性情境，不是獨立樣本或資金組合報酬。"})
    return {"version":"short-cycle-evaluation-v1","source_as_of":bundle["as_of"],"source_digest":digest(bundle),
        "policy":policy,"registry":registry,"eligible_stocks":eligible,"skipped":skipped,"signals_by_pattern":dict(counts),
        "summary":summary,"signals":rows,"recent_series":recent_series,"point_in_time_verified":False,"empirical_edge_validated":False,
        "limitations":["CURRENT_VINTAGE_HISTORY_AND_TODAY_FIXED_UNIVERSE_SURVIVORSHIP", "PARAMETERS_AUTHORED_AFTER_REFERENCE_CASE",
            "OVERLAPPING_SIGNALS_NOT_PORTFOLIO_OR_INDEPENDENT_OBSERVATIONS","NEXT_OPEN_PRICE_NOT_BROKER_FILL",
            "INTRADAY_TRIGGER_FILLS_UNVERIFIED_EXCLUDED","SAME_BAR_TARGET_AND_STOP_ASSUMED_STOP_FIRST",
            "STRATEGY_TARGET_EXCLUDES_COSTS_ACTUAL_COST_SCENARIOS_REPORTED_SEPARATELY"],"execution_enabled":False}


def render_evaluation(value):
    pct=lambda x:'—' if x is None else f'{x:.2%}'
    rows=''.join(f'<tr><td>{escape(r["pattern_id"])}</td><td>{r["holding_sessions"]}</td><td>{r["signal_count"]}</td>'
        f'<td>{r["completed_scenarios"]}</td><td>{pct(r["target_hit_rate_stop_first"])}</td><td>{pct(r["mean_price_return"])}</td>'
        f'<td>{pct(r["mean_cost_adjusted_return"])}</td></tr>' for r in value['summary'])
    examples=''.join(f'<tr><td>{escape(r["symbol"])}</td><td>{escape(r["signal_session"])}</td><td>{escape(r["buy_type"])}</td>'
        f'<td>{r["holding_sessions"]}</td><td>{escape(r["status"])}</td><td>{r["entry_reference"] or "—"}</td>'
        f'<td>{pct(r["gross_return"])}</td><td>{pct(r["cost_adjusted_return"])}</td></tr>' for r in value['signals']
        if r['symbol'] in {'6857.T','5016.T','6027.T'} and r['signal_session']>='2026-10-20')
    charts=[]
    symbol_rows=[]
    for symbol,bars in value.get('recent_series',{}).items():
        if symbol not in {'6857.T','5016.T','6027.T'} or len(bars)<2:continue
        subset=[r for r in value['signals'] if r['symbol']==symbol and r['holding_sessions']==3]
        completed=[r for r in subset if r['gross_return'] is not None]
        symbol_rows.append(f'<tr><td>{escape(symbol)}</td><td>{len(subset)}</td><td>{len(completed)}</td>'
            f'<td>{sum(r["status"]=="TARGET" for r in completed)}</td><td>{sum(r["ambiguous"] for r in completed)}</td></tr>')
        low,high=min(b['close'] for b in bars),max(b['close'] for b in bars)
        span=high-low or 1
        points=' '.join(f'{20+i*560/(len(bars)-1):.2f},{150-(b["close"]-low)*120/span:.2f}' for i,b in enumerate(bars))
        charts.append(f'<figure><figcaption>{escape(symbol)}：最近15日收盤，末筆{escape(bars[-1]["session"])}＝{bars[-1]["close"]:,.2f}；區間{low:,.2f}–{high:,.2f}</figcaption>'
            f'<svg viewBox="0 0 600 185" role="img" aria-label="{escape(symbol)}收盤走勢"><path d="M20 155H580" stroke="#ccc"/>'
            f'<polyline points="{points}" fill="none" stroke="#165a98" stroke-width="3"/>'
            f'<text x="20" y="180" font-size="12">{escape(bars[0]["session"])}</text><text x="500" y="180" font-size="12">{escape(bars[-1]["session"])}</text></svg></figure>')
    return ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>短線機會：訊號與事後路徑</title><style>body{font:15px/1.7 system-ui;max-width:1400px;margin:auto;padding:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px}aside{background:#fff4d7;padding:16px}.scroll{overflow:auto}figure{margin:24px 0}svg{display:block;width:100%;max-width:760px}</style>'
        '<h1>短線機會：3／5個交易日的訊號與事後路徑</h1>'
        '<aside>策略目標為股價+2%，選股不計券商費用；成本後結果另列。這是今日版本資料與目前固定名單的歷史前綴重算，'
        '訊號重疊、非獨立樣本，不能當投資組合績效、歷史當時預測準確率或樣本外優勢。'
        '次日開盤為假設成交；跳空超限不成交，盤中僅觸發不假設成交，同根日K同時碰停損與目標採停損先到。</aside>'
        f'<p>資料截止：{escape(value["source_as_of"])}；可用標的 {value["eligible_stocks"]} 檔，排除 {len(value["skipped"])} 檔。</p>'
        +''.join(charts)+
        '<h2>關注標的：3日情境比較</h2><p>包含不同型態及重疊日期。同根K同時觸及目標／停損列為歧義，保守算停損；達標數不可當成未來勝率。</p>'
        '<div class="scroll"><table><tr><th>標的</th><th>訊號數</th><th>已結束情境</th><th>先達2%目標</th><th>日K先後歧義</th></tr>'
        +''.join(symbol_rows)+'</table></div>'+
        '<h2>訊號情境彙整</h2><div class="scroll"><table><thead><tr><th>類型</th><th>交易日</th><th>訊號數</th><th>已結束情境</th><th>先達目標比例</th><th>平均股價報酬</th><th>平均成本後報酬</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
        '<h2>Advantest／JX金屬／6027 近期訊號</h2><div class="scroll"><table><thead><tr><th>代號</th><th>訊號日</th><th>買入類型</th><th>交易日</th><th>事後路徑</th><th>假設進場價</th><th>股價報酬</th><th>成本後報酬</th></tr></thead>'
        f'<tbody>{examples}</tbody></table></div></html>')
