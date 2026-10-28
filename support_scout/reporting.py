"""Self-contained HTML report; all source and model text is escaped."""
from __future__ import annotations

from html import escape

from .contracts import timestamp
from datetime import timedelta, timezone

STATES = {"RULE_CONFIRMED": "規則確認（暫定）", "RETEST_HELD": "回測守住，等待確認",
    "BOUNCE_SEEN": "已反彈，等待回測", "BASE_FORMING": "底部形成中", "INVALIDATED": "支撐已失效",
    "NO_SETUP": "尚無型態", "EXPIRED": "型態過期", "INSUFFICIENT_DATA": "歷史不足"}
REASONS = {"OBSERVATION_ONLY": "僅供觀察", "CORPORATE_ACTIONS_UNVERIFIED": "公司行動未核實",
    "TRADABILITY_UNVERIFIED": "停牌／鎖價資訊未核實", "POINT_IN_TIME_NOT_VERIFIED": "非歷史當時可得快照",
    "DOSSIER_UNKNOWN_OR_STALE": "公司與公告查核待補", "MARKET_STRESS_UNKNOWN": "市場壓力尚未分類",
    "SUPPORT_NOT_RULE_CONFIRMED": "支撐未完成確認", "EXTENDED_FROM_SUPPORT": "目前距支撐過遠",
    "AWAITING_INDEPENDENT_AGENT_REVIEWS": "等待雙方研究", "HARD_INVALIDATION": "固定失效條件成立",
    "AGENT_WATCH_ONLY": "代理建議繼續觀察", "AGENT_REJECTED": "代理提出反證",
    "LATEST_INTRADAY_UNDERCUT": "最新低點穿過支撐下緣", "LATEST_CLOSE_NOT_ABOVE_ZONE": "最新收盤未站回上緣",
    "LOCKED_PRICE_IN_SUPPORT_EVIDENCE": "證據含鎖價／平價疑慮", "DATA_COLLECTION_DEGRADED": "資料取得不完整"}
REASONS.update({"COMMON_STOCK_CLASSIFICATION_UNVERIFIED": "普通股分類待獨立核對",
    "COMPANY_EVENTS_UNVERIFIED": "公司公告與決算事件未核實",
    "CURRENT_RETRIEVAL_NOT_HISTORICAL_PIT": "本次下載不代表歷史當時可得資料",
    "TURNOVER_ESTIMATED_CLOSE_TIMES_VOLUME": "成交值為收盤價乘股數估算",
    "YAHOO_UNOFFICIAL_SOURCE": "Yahoo 非交易所官方行情源",
    "CORPORATE_ACTION_IN_WINDOW_REQUIRES_REVIEW": "期間含拆股／公司行動，價格需覆核",
    "NO_AVAILABLE_BARS": "沒有可分析的完整日線"})


def fmt(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def sparkline(bars, support):
    closes = [b["close"] for b in bars[-60:]]
    if len(closes) < 2:
        return ""
    prices = closes + ([support["lower"], support["upper"]] if support else [])
    lo, hi = min(prices), max(prices)
    span = hi - lo or 1
    y = lambda price: 68 - (price - lo) / span * 60
    points = " ".join(f"{i * 220 / (len(closes)-1):.2f},{y(c):.2f}" for i, c in enumerate(closes))
    zone = ""
    if support:
        zone = f'<rect x="0" y="{y(support["upper"]):.2f}" width="220" height="{max(1,y(support["lower"])-y(support["upper"])):.2f}" fill="#f0c46e" opacity=".25"/>'
    return f'<svg viewBox="0 0 220 76" role="img" aria-label="最近60根來源收盤價與暫定支撐區">{zone}<polyline fill="none" stroke="#306e9b" stroke-width="2" points="{points}"/></svg>'


def render_html(report: dict, bundle: dict) -> str:
    japan = report.get("market") == "JP"
    market_label = "日股" if japan else "台股"
    market_date_label = "東京市場（JPY）" if japan else "台北市場（TWD）"
    collection = report.get("collection", {})
    observation = report.get("observation", {})
    latest = collection.get("actual_latest_session") or "—"
    expected = collection.get("expected_latest_session") or "—"
    data = {s["symbol"]: s for s in bundle["securities"]}
    discovery = report.get("discovery", {})
    search = discovery.get("search") or {}
    selections = {s["symbol"]: s for s in search.get("selection", [])}
    coverage = discovery.get("coverage", {})
    ledger = report.get("ledger", {})
    ledger_rows = ledger.get("rows", [])
    rows, cards = [], []
    for item in report["securities"]:
        symbol = item["symbol"]
        anchor_id = escape(str(symbol), quote=True)
        t = item.get("technical", {})
        support = t.get("support") or {}
        reasons = [REASONS.get(r, r) for r in item["reasons"]]
        changes = [r for r in ledger_rows if r.get("symbol") == symbol]
        change = ", ".join(r.get("change", "") for r in changes) or "首次／無追蹤型態"
        reason_text = escape("；".join(reasons))
        decision = item.get("trade_decision", {})
        conclusion = decision.get("label", item["verdict"])
        selection = selections.get(symbol)
        origin = (f'SEARCH #{selection["agent_order"]}' if selection else "本輪未提交") if discovery else "手動名單"
        state = STATES.get(t.get("state"), item["data_status"])
        rows.append(f'<tr><td><a href="#{anchor_id}">{escape(str(symbol))}</a><br><small>{escape(item.get("name", ""))}</small></td>'
            f'<td>{escape(state)}</td><td>{fmt(t.get("latest_close"))}</td>'
            f'<td>{fmt(support.get("lower"))}–{fmt(support.get("upper"))}</td>'
            f'<td>{fmt(t.get("distance_above_zone_atr"))} ATR</td><td><b>{escape(conclusion)}</b><br><small>{escape(item["verdict"])}</small></td>'
            f'<td>{escape(origin)}</td><td>{escape(change)}</td><td class="notes">{reason_text}</td></tr>')
        reviews = []
        for role, review in item.get("agent_reviews", {}).items():
            reviews.append(f'<p><b>{escape(role)} · {escape(review["verdict"])}</b> {escape(review["summary"])}</p>')
        for event in report.get("desk", {}).get("events", []):
            if event.get("symbol") == symbol and event.get("status") == "ERROR":
                reviews.append(f'<p class="warn">{escape(event["role"])}：{escape(event.get("error", "ERROR"))}</p>')
        source = data.get(symbol, {})
        source_rows = source.get("source_documents", [])
        links = " ".join(f'<a href="{escape(s["url"], quote=True)}" target="_blank" rel="noreferrer">來源{i+1}</a>'
            for i, s in enumerate(source_rows) if s.get("url", "").startswith("https://"))
        plan = decision.get("reference_plan") or {}
        decision_html = ""
        if decision:
            decision_html = (f'<div class="decision"><h4>{escape(conclusion)}</h4><p>{escape(decision.get("summary", ""))}</p>'
                f'<p>收盤參考：{fmt(plan.get("entry_reference"))}；固定失效下緣：{fmt(plan.get("invalidation_reference"))}；'
                f'歷史阻力參考：{fmt(plan.get("target_reference"))}；成本與跳空情境後 RR：{fmt(plan.get("net_reward_risk"))}</p>'
                '<p><small>以上為快照參考價；下個交易時段須重新驗證。支撐下緣是收盤失效條件，並非保證成交的停損單。</small></p>'
                f'<p>後續條件：{escape("；".join(decision.get("next_steps", [])))}</p></div>')
        cards.append(f'<article id="{anchor_id}"><h3>{escape(str(symbol))} {escape(item.get("name", ""))} · {escape(state)}</h3>'
            f'{sparkline(source.get("bars", []), support)}<p>低點：{escape(str(support.get("anchor_at", "—")))}；'
            f'反彈：{escape(str(support.get("bounce_at", "—")))}；回測：{escape(str(support.get("retest_at", "—")))}；'
            f'確認：{escape(str(support.get("confirmed_at", "—")))}</p>'
            f'<p>價格口徑：{escape(item.get("price_basis", "未知"))}。固定失效下緣：{fmt(support.get("lower"))}；最新日線：{escape(str(t.get("latest_session", "—")))}</p>'
            f'{decision_html}<p class="notes">{reason_text}</p>{"".join(reviews) or "<p>本檔尚無模型評語。</p>"}'
            f'<p>{links}</p></article>')
    jst = timestamp(report["as_of"]).astimezone(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    queue = ", ".join(report["technical_queue"]) or "本輪沒有，繼續等待"
    status = observation.get("status", "UNKNOWN")
    model = observation.get("agent_status", "NOT_RUN")
    decisions = report.get("decision_summary", {}).get("counts", {})
    decision_metrics = (f'<div>決策結論<b>可考慮 {decisions.get("CONSIDER",0)} · 等待 {decisions.get("WAIT",0)} · 不交易 {decisions.get("NO_TRADE",0)}</b>技術型態與交易資格分別檢查</div>' if japan else "")
    badge = "SYNTHETIC DEMO" if report.get("synthetic") else "OBSERVATION ONLY" if report.get("observation_only") else "DECISION RESEARCH"
    banner = ("合成資料示範，不是真實行情。" if report.get("synthetic") else
        "Yahoo Finance 暫時資料源；公司行動、公司公告與完整交易狀態尚未核對。" if japan and report.get("observation_only") else
        "決策研究結論需於下一交易時段重新驗證。" if japan else
        "原始價格尚未核對公司行動、公司公告與完整交易狀態。")
    search_html = ""
    scope_label = f"{len(report['securities'])} 檔手動名單，非全市場"
    if discovery:
        scope_label = f"本輪 {coverage.get('scanned_count',len(report['securities']))} / 母池 {coverage.get('universe_count','—')} 檔，輪替掃描"
        universe = discovery.get("universe", {})
        outputs = {s["symbol"]: s for s in report["securities"]}
        handoffs = []
        for selected in search.get("selection", []):
            output = outputs[selected["symbol"]]
            reviews = output.get("agent_reviews", {})
            handoffs.append(f'<tr><td>{selected["agent_order"]}</td><td>{escape(selected["symbol"])}</td>'
                f'<td>{selected["pre_rank"]}</td><td>{escape(selected["rationale"])}</td>'
                f'<td>{escape(reviews.get("researcher",{}).get("verdict","未完成"))}</td>'
                f'<td>{escape(reviews.get("reviewer",{}).get("verdict","未完成"))}</td>'
                f'<td>{escape(output.get("trade_decision",{}).get("label", "待裁決"))}</td></tr>')
        status_text = search.get("status", "NOT_RUN")
        if discovery.get("mode") == "NUMERIC_ONLY":
            status_text = "只完成數值預掃，未執行 SEARCH agent"
        search_html = (f'<h2>SEARCH 尋標與決策交接</h2><article><p><b>{escape(status_text)}</b> · '
            f'模型 {escape(str(search.get("model") or "未呼叫"))} · '
            f'總 API 呼叫 {discovery.get("total_call_count",0)} / {discovery.get("max_total_calls",0)}</p>'
            f'<p>{escape(scope_label)}；JPX 名單基準日 {escape(str(universe.get("as_of","—")))}。'
            f'本輪候選 {len((search.get("search_packet") or {}).get("core",{}).get("candidates",[]))} 檔，'
            f'SEARCH 提交 {len(selections)} 檔。未完成搜尋時不會自動代選。</p>'
            f'<p class="warn">{escape(search.get("error", ""))}</p>'
            '<div class="scroll"><table><thead><tr><th>Agent順序</th><th>標的</th><th>數值預排</th><th>SEARCH理由</th>'
            '<th>研究員</th><th>反證員</th><th>決策</th></tr></thead>'
            f'<tbody>{"".join(handoffs) or "<tr><td colspan=7>本輪未提交候選。</td></tr>"}</tbody></table></div></article>')
    return f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Support Desk {market_label}觀察 {escape(latest)}</title><style>
body{{font:15px/1.65 system-ui,sans-serif;color:#203449;background:#f4f6f8;margin:0}}main{{max-width:1500px;margin:auto;padding:30px}}h1{{margin-bottom:6px}}.sub,small{{color:#607185}}.banner{{background:#fff4d7;border-left:4px solid #b58222;padding:16px;margin:22px 0}}.metrics{{display:flex;gap:12px;flex-wrap:wrap}}.metrics div,article{{background:white;padding:18px;border:1px solid #dde4eb;border-radius:8px}}.metrics b{{display:block;font-size:20px}}.scroll{{overflow:auto;background:white;margin:24px 0}}table{{border-collapse:collapse;min-width:1000px;width:100%}}th,td{{text-align:left;padding:12px;vertical-align:top;border-bottom:1px solid #e4e9ed}}th{{background:#eaf0f5}}.notes{{font-size:13px;max-width:360px;color:#536579}}article{{margin:14px 0}}article svg{{max-width:440px;width:100%;height:130px}}a{{color:#246b9b}}input{{padding:10px;width:260px;border:1px solid #c7d1db;border-radius:4px}}.warn{{color:#9d3c29}}footer{{margin:30px 0;color:#607185}}
</style><main><h1>Support Desk · {market_label}支撐決策</h1><div class="sub">快照截止 JST {escape(jst)} · 日線日期依{market_date_label} · 本地收盤觀察，非即時報價</div>
<div class="banner"><b>{badge}</b>　{escape(banner)} 型態與價位皆為暫定觀察；雙 Agent 同意也不會解除缺口。分數不是勝率。</div>
<div class="metrics"><div>資料最新日<b>{escape(latest)}</b>預期 {escape(expected)}</div><div>資料取得<b>{escape(collection.get('status','UNKNOWN'))}</b>{escape(scope_label)}</div><div>觀察執行<b>{escape(status)}</b>模型 {escape(model)}</div><div>技術候選<b>{len(report['technical_queue'])}</b>{escape(queue)}</div>{decision_metrics}</div>
{search_html}
<h2>觀察名單與失效條件</h2><input id="filter" placeholder="搜尋代號、名稱或狀態" aria-label="篩選觀察表">
<div class="scroll"><table><thead><tr><th>標的</th><th>型態</th><th>收盤</th><th>暫定支撐區</th><th>距離上緣</th><th>結論</th><th>候選來源</th><th>跨日變化</th><th>待確認</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>個股證據與兩方評語</h2>{''.join(cards)}<footer>Run {escape(observation.get('run_id','—'))} · 規則 {escape(report['rules']['version'])} · 快照 {escape(report['snapshot_id'])}<br>本報告保存於本機；沒有訂單或帳戶執行能力。</footer>
</main><script>document.getElementById('filter').addEventListener('input',function(){{const q=this.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}});</script></html>'''
