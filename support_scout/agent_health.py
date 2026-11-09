"""Explain orchestration failures separately from market opinions and data gaps."""


def error_explanation(code):
    code = code or ""
    if code == "COMPACT_INPUT_BUDGET_EXCEEDED":
        return "本地輸入超過大小上限，該角色尚未送到API；不是模型看空"
    if code == "API_TIMEOUT":
        return "API等待逾時，未取得可用回覆；不是交易判斷，預設不自動重試"
    if code == "SHARED_PUBLIC_SOURCES_UNAVAILABLE_NO_SECOND_SEARCH":
        return "研究員未交付有效公開來源，反證員連帶略過，以免再查一次；不是第二次API故障"
    if code == "WIRE_LIST_LIMIT:risks":
        return "風險清單超出約定項數或字數上限，被本地檢查拒收"
    if code == "WIRE_LIST_LIMIT:missing_data":
        return "資料缺口清單超出約定項數或字數上限，被本地檢查拒收"
    if code.startswith("WIRE_"):
        return "回覆超出約定格式或長度，被本地檢查拒收"
    if "EVIDENCE" in code or "REVIEW_CONTRACT" in code or "OPPORTUNITY_VIEW" in code:
        return "證據引用或回覆契約不合規，不能交付下一階段"
    if code.startswith("API_HTTP_"):
        return "API服務回傳錯誤，未取得可用研究回覆"
    if "CONFIGURATION" in code or "API_KEY" in code:
        return "模型連線設定未齊全"
    return "此階段未完成，技術原因保留於當輪稽核紀錄"


def model_health(report):
    search = report.get("discovery", {}).get("search") or {}
    desk = report.get("desk") or {}
    events = desk.get("events", [])
    accepted = sum(e.get("status") == "ACCEPTED" for e in events)
    errors = [e for e in events if e.get("status") in {"ERROR", "SKIPPED"}]
    issues = [{"stage": e.get("role"), "symbol": e.get("symbol"), "code": e.get("error"),
        "explanation": error_explanation(e.get("error"))} for e in errors]
    if search.get("status") == "FAILED":
        issues.insert(0, {"stage": "search", "symbol": None, "code": search.get("error"),
            "explanation": error_explanation(search.get("error"))})
    status = report.get("observation", {}).get("agent_status")
    if desk.get("mode") == "offline_simulation":
        label = "離線流程示範，未呼叫模型"
    elif issues:
        label = "部分研究未完成" if accepted else "研究流程未完成"
    elif status == "CONFIGURATION_REQUIRED":
        label = "模型設定尚未齊全"
    elif events:
        label = "模型研究已完成" if not desk.get("deferred_symbols") else "已完成本輪名額，其他標的待研究"
    elif search.get("status") == "ABSTAIN":
        label = "尋標完成，本輪未交付深研"
    elif status == "NOT_REQUESTED":
        label = "本輪僅數值觀察，未要求模型研究"
    elif status == "NO_ELIGIBLE_PACKETS":
        label = "本輪沒有可研究封包"
    elif status in {"FAILED", "SEARCH_FAILED", "PARTIAL_FAILURE"}:
        label = "研究流程未完成"
    else:
        label = "尚無模型研究回覆"
    text = f'研究／反證有效回覆 {accepted}/{len(events)} 份'
    if issues:
        roles = {"search": "尋標員", "researcher": "研究員", "reviewer": "反證員"}
        text += "；" + "；".join(f'{i["symbol"] or ""}{roles.get(i["stage"], "研究角色")}：{i["explanation"]}' for i in issues)
    text += "。未完成只代表缺少有效研究，與WATCH／反證否決不同；缺雙方有效評估的標的不產生新買入提案。"
    return {"label": label, "text": text, "accepted_reviews": accepted, "scheduled_roles": len(events),
        "error_roles": sum(e.get("status") == "ERROR" for e in events),
        "skipped_roles": sum(e.get("status") == "SKIPPED" for e in events), "issues": issues}
