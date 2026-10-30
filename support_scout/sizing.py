"""Deterministic manual buying plans; no order submission or inferred fills."""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
import math

from .contracts import DataError, digest, strict_json_loads

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "local_data" / "sizing_settings.json"
EXAMPLE = ROOT / "config" / "sizing.example.json"


def _number(value, *, zero=False):
    return type(value) in (int, float) and math.isfinite(value) and (value >= 0 if zero else value > 0)


def load_settings(value=None):
    if isinstance(value, dict):
        settings = deepcopy(value)
    else:
        path = Path(value or DEFAULT_SETTINGS)
        settings = strict_json_loads((path if path.exists() else EXAMPLE).read_text(encoding="utf-8-sig"))
    fields = {"version", "capital_jpy", "per_trade_risk_fraction", "max_position_fraction", "max_new_risk_fraction",
        "broker_mode", "minimum_amount_jpy", "amount_increment_jpy", "board_lot_shares",
        "entry_spread_fraction", "exit_spread_fraction", "gap_buffer_fraction"}
    if not isinstance(settings, dict) or set(settings) != fields or settings["version"] != "jp-manual-sizing-v1":
        raise DataError("INVALID_SIZING_CONFIGURATION")
    if settings["capital_jpy"] is not None and (not _number(settings["capital_jpy"]) or settings["capital_jpy"] > 1e12):
        raise DataError("INVALID_SIZING_CAPITAL")
    for key in ("per_trade_risk_fraction", "max_position_fraction", "max_new_risk_fraction"):
        v = settings[key]
        if key == "per_trade_risk_fraction" and v is None:
            continue
        if not _number(v) or v > 1:
            raise DataError("INVALID_SIZING_FRACTION:" + key)
    for key in ("entry_spread_fraction", "exit_spread_fraction", "gap_buffer_fraction"):
        if not _number(settings[key], zero=True) or settings[key] >= 1:
            raise DataError("INVALID_SIZING_COST")
    for key in ("minimum_amount_jpy", "amount_increment_jpy", "board_lot_shares"):
        if type(settings[key]) is not int or not 1 <= settings[key] <= 1_000_000:
            raise DataError("INVALID_SIZING_UNIT")
    if settings["broker_mode"] not in {"AMOUNT_JPY", "BOARD_LOT"}:
        raise DataError("INVALID_SIZING_BROKER_MODE")
    return settings


def _d(value):
    return Decimal(str(value))


def _cash(report, capital):
    portfolio = report.get("portfolio_snapshot")
    if portfolio is None:
        return capital, {}, None
    try:
        from .portfolio import verify_snapshot
        portfolio = verify_snapshot(portfolio, report["as_of"])
        cash = capital
        for fill in portfolio["confirmed_fills"]:
            amount = _d(fill["quantity"]) * _d(fill["price"])
            cash += amount - _d(fill["fees"]) if fill["side"] == "SELL" else -amount - _d(fill["fees"])
        return max(Decimal(0), cash), portfolio["positions"], None
    except (DataError, KeyError, TypeError, ValueError):
        return Decimal(0), {}, "PORTFOLIO_INTEGRITY_REVIEW_REQUIRED"


def attach_execution_plans(report, settings=None):
    settings = load_settings(settings if settings is not None else report.get("sizing_policy"))
    report["sizing_policy"] = deepcopy(settings)
    ready = settings["capital_jpy"] is not None and settings["per_trade_risk_fraction"] is not None
    capital = _d(settings["capital_jpy"] or 0)
    cash, positions, portfolio_error = _cash(report, capital)
    initial_cash = cash
    per_risk = capital * _d(settings["per_trade_risk_fraction"] or 0)
    remaining_risk = capital * _d(settings["max_new_risk_fraction"])
    selected = [x["symbol"] for x in (report.get("discovery", {}).get("search") or {}).get("selection", [])]
    rows = sorted(report["securities"], key=lambda x: (selected.index(x["symbol"]) if x["symbol"] in selected else len(selected)))
    brief, allocated, total_risk = [], Decimal(0), Decimal(0)
    for item in rows:
        opportunity = item.get("opportunity_decision") or {}
        reference = opportunity.get("reference_plan") or {}
        holding = positions.get(item["symbol"])
        manual = item.get("manual_action") or {}
        action = manual.get("action", "REVIEW") if holding else opportunity.get("action", "WAIT")
        label = manual.get("label", "持倉待核對") if holding else opportunity.get("label", "等待有效機會評估")
        entry, stop, target = (reference.get(k) for k in ("entry_reference", "invalidation_reference", "target_reference"))
        amount, shares, loss, effective_risk, broker_rr = Decimal(0), Decimal(0), Decimal(0), None, None
        status = "NO_NEW_BUY_SIGNAL"
        checks = list(opportunity.get("manual_checks", []))
        checks.extend(["確認此標的可在你的PayPay帳戶交易；目前名單尚未核實。", "使用券商最新買賣報價重算；參考收盤不是可保證成交價。",
            "金額指定的換算股數只是估計，實際成交後才回報持倉。", "設定值是情境虧損上限，跳空可能使實際虧損超過上限。"])
        condition = "尚未符合進場條件，不買進。"
        if opportunity.get("volatility_watch"):
            level=opportunity.get("watch_trigger_reference")
            condition=(f"高波動觀察，先看是否站回{level:,.2f}並形成方向；尚無可執行進場條件。" if _number(level) else "高波動觀察，方向未確認，不買進。")
        if holding:
            status, condition = "HOLDING_REVIEW_NO_ADD", "已有確認持倉，不新增買進；依持倉評語人工評估續抱或退出。"
        elif action == "PROBE_BUY_REVIEW":
            condition = "先核對可交易性、即時價格與較近供給；仍守住H-002固定下緣且風報通過才評估金額試單。"
            if reference.get("buy_type"):
                condition=(f"{reference['buy_type_label']}：" + (f"尚待觸發，價格在{reference['entry_trigger_reference']:,.2f}至{reference['entry_ceiling_reference']:,.2f}間才評估；" if reference.get("trigger_required") else
                    f"下一時段價格不高於{reference['entry_ceiling_reference']:,.2f}才重新評估；") +
                    "按實際進場價重算+2%價格目標與戰術風險線；最多5個交易日，未達標也要時間退出評估。")
            if not ready:
                status = "BUDGET_NOT_CONFIGURED"
            elif portfolio_error:
                status = portfolio_error
            elif not all(_number(x) for x in (entry, stop, target, reference.get("risk_fraction"))) or not stop < entry < target:
                status = "INVALID_REFERENCE_PLAN"
            else:
                e, s, t = _d(entry), _d(stop), _d(target)
                friction = _d(settings["entry_spread_fraction"]) + _d(settings["exit_spread_fraction"]) + _d(settings["gap_buffer_fraction"])
                effective_risk = max(_d(reference["risk_fraction"]), (e - s) / e + friction)
                broker_rr = ((t - e) / e - friction) / effective_risk
                minimum_rr = _d(reference.get("minimum_reward_risk",report.get("opportunity_policy", {}).get("minimum_net_rr", 1.5)))
                qualification_rr=_d(reference.get("selection_reward_risk",broker_rr)) if reference.get("exclude_broker_costs_for_selection") else broker_rr
                if effective_risk > 1 or qualification_rr < minimum_rr:
                    status = "BROKER_COST_RISK_REVIEW_REQUIRED"
                else:
                    cap = min(cash, capital * _d(settings["max_position_fraction"]), per_risk / effective_risk, remaining_risk / effective_risk)
                    quoted_entry = e * (1 + _d(settings["entry_spread_fraction"]))
                    if settings["broker_mode"] == "AMOUNT_JPY":
                        step = _d(settings["amount_increment_jpy"])
                        amount = (max(cap, Decimal(0)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
                        shares = (amount / quoted_entry).quantize(Decimal("0.00000001"), rounding=ROUND_FLOOR)
                    else:
                        lot = _d(settings["board_lot_shares"])
                        shares = (max(cap, Decimal(0)) / (quoted_entry * lot)).to_integral_value(rounding=ROUND_FLOOR) * lot
                        amount = shares * quoted_entry
                    if amount < settings["minimum_amount_jpy"] or shares == 0:
                        amount, shares, status = Decimal(0), Decimal(0), "BELOW_MINIMUM_OR_LOT_UNAFFORDABLE"
                    else:
                        loss = amount * effective_risk
                        cash -= amount
                        remaining_risk -= loss
                        allocated += amount
                        total_risk += loss
                        status = "SIMULATION_ONLY" if opportunity.get("scope") == "SIMULATION_ONLY" or report.get("synthetic") is not False else "CONDITIONAL_ESTIMATE"
        plan = {"symbol": item["symbol"], "name": item.get("name", item["symbol"]), "action": action, "label": label,
            "entry_condition": condition, "entry_reference": entry, "invalidation_reference": stop, "target_reference": target,
            "suggested_amount_jpy": float(amount), "suggested_shares": float(shares), "sizing_status": status,
            "risk_budget_jpy": float(per_risk), "estimated_loss_jpy": float(loss),
            "effective_risk_fraction": float(effective_risk) if effective_risk is not None else None,
            "broker_adjusted_net_rr": float(broker_rr) if broker_rr is not None else None,
            "broker_mode": settings["broker_mode"], "held_quantity": holding.get("quantity", 0) if holding else 0,
            "manual_checks": list(dict.fromkeys(checks)), "as_of": report["as_of"], "execution_enabled": False,
            "quantity_meaning": "AMOUNT_JPY的股數僅供換算；不是券商實際數量或訂單。"}
        if "short_horizon" in report.get("opportunity_policy",{}):
            plan.update(buy_type=opportunity.get("buy_type"),buy_type_label=opportunity.get("buy_type_label"),
                holding_sessions=reference.get("holding_sessions"),time_exit_sessions=reference.get("time_exit_sessions"),
                target_basis=reference.get("target_basis"),target_return_fraction=reference.get("target_return_fraction"),
                estimated_net_return_fraction=reference.get("estimated_net_return_fraction"),
                trigger_required=reference.get("trigger_required",False),entry_trigger_reference=reference.get("entry_trigger_reference"),
                entry_ceiling_reference=reference.get("entry_ceiling_reference"),research_status=opportunity.get("research_status"))
        item["execution_plan"] = plan
        brief.append(deepcopy(plan))
    report["decision_brief"] = {"version": "jp-decision-brief-v1", "as_of": report["as_of"], "rows": brief,
        "capital_jpy": settings["capital_jpy"], "per_trade_risk_fraction": settings["per_trade_risk_fraction"],
        "per_trade_risk_budget_jpy": float(per_risk), "available_cash_estimate_jpy": float(initial_cash),
        "proposed_total_jpy": float(allocated), "proposed_loss_scenario_jpy": float(total_risk),
        "remaining_cash_estimate_jpy": float(cash), "sizing_policy_digest": digest(settings),
        "cash_basis": "USER_CAPITAL_AND_CONFIRMED_FILL_CASH_FLOWS_NOT_BROKER_BALANCE", "execution_enabled": False}
    return report
