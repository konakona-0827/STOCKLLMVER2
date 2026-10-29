"""User-confirmed manual fills, replayable holdings, and human review actions.

These records are user declarations, not broker reconciliation or orders. Hashes
detect inconsistencies; they do not authenticate a file deliberately rewritten
with fabricated fills. Only confirmed, recorded, cutoff-visible fills count.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import math
from pathlib import Path
import re

from .contracts import DataError, TOKYO, digest, strict_json_loads, timestamp
from .japan import normalize_symbol
from .storage import atomic_json, pointer_lock

FILL_FIELDS = {"id", "symbol", "side", "quantity", "price", "fees", "filled_at", "confirmed"}
RISK_FIELDS = {"channel", "lower", "price_basis", "defined_at", "anchor_at"}
SOURCE = "USER_CONFIRMED_MANUAL_FILLS_NOT_BROKER_SYNC"
MAX_FILLS = 10000


def _money(value, *, zero=False):
    return type(value) in (int, float) and math.isfinite(value) and (value >= 0 if zero else value > 0) and value <= 1e12


def _risk(value):
    if not isinstance(value, dict) or set(value) != RISK_FIELDS:
        raise DataError("MANUAL_RISK_REFERENCE_FIELDS")
    if (not isinstance(value["channel"], str) or value["channel"] not in {"ATR", "H-001", "MANUAL"}
            or not _money(value["lower"]) or not isinstance(value["price_basis"], str)
            or value["price_basis"] not in {"raw_unverified", "raw_no_actions_in_window", "adjusted_ohlc"}):
        raise DataError("MANUAL_RISK_REFERENCE_INVALID")
    timestamp(value["defined_at"])
    anchor = value["anchor_at"]
    if anchor is None and value["channel"] != "MANUAL":
        raise DataError("MANUAL_RISK_ANCHOR_REQUIRED")
    if anchor is not None:
        try:
            if date.fromisoformat(anchor) > timestamp(value["defined_at"]).astimezone(TOKYO).date():
                raise ValueError()
        except (TypeError, ValueError):
            raise DataError("MANUAL_RISK_ANCHOR_INVALID") from None
    return deepcopy(value)


def _validate(entry):
    if not isinstance(entry, dict) or set(entry) not in (FILL_FIELDS, FILL_FIELDS | {"risk_reference"}):
        raise DataError("MANUAL_FILL_FIELDS")
    entry = deepcopy(entry)
    if not isinstance(entry["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", entry["id"]):
        raise DataError("MANUAL_FILL_ID")
    entry["symbol"] = normalize_symbol(entry["symbol"])
    if not isinstance(entry["side"], str) or entry["side"] not in {"BUY", "SELL"} or type(entry["confirmed"]) is not bool:
        raise DataError("MANUAL_FILL_SIDE_OR_CONFIRMATION")
    if type(entry["quantity"]) is not int or not 1 <= entry["quantity"] <= 10_000_000:
        raise DataError("MANUAL_FILL_QUANTITY")
    if not _money(entry["price"]) or not _money(entry["fees"], zero=True):
        raise DataError("MANUAL_FILL_PRICE_OR_FEES")
    timestamp(entry["filled_at"])
    if "risk_reference" in entry:
        if entry["side"] != "BUY":
            raise DataError("MANUAL_RISK_ON_BUY_ONLY")
        entry["risk_reference"] = _risk(entry["risk_reference"])
    return entry


def _history(value, *, contiguous=True):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "fills"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["fills"], list) or len(value["fills"]) > MAX_FILLS):
        raise DataError("INVALID_MANUAL_PORTFOLIO")
    ids, previous_sequence, previous_recorded = set(), 0, None
    for row in value["fills"]:
        if not isinstance(row, dict) or not {"recorded_at", "sequence"} <= set(row):
            raise DataError("MANUAL_FILL_RECORD_FIELDS")
        entry = _validate({k: v for k, v in row.items() if k not in {"recorded_at", "sequence"}})
        if entry["symbol"] != row["symbol"]:
            raise DataError("MANUAL_FILL_CANONICAL_SYMBOL_REQUIRED")
        recorded = timestamp(row["recorded_at"])
        if timestamp(row["filled_at"]) > recorded:
            raise DataError("MANUAL_FILL_CANNOT_BE_IN_FUTURE")
        if entry.get("risk_reference") and timestamp(entry["risk_reference"]["defined_at"]) > recorded:
            raise DataError("MANUAL_RISK_NOT_YET_KNOWN")
        sequence = row["sequence"]
        if (row["id"] in ids or type(sequence) is not int or sequence <= previous_sequence
                or (contiguous and sequence != previous_sequence + 1)
                or (previous_recorded is not None and recorded < previous_recorded)):
            raise DataError("DUPLICATE_OR_REORDERED_MANUAL_FILL")
        ids.add(row["id"])
        previous_sequence, previous_recorded = sequence, recorded
    return value


def load_fills(path):
    path = Path(path)
    value = strict_json_loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"schema_version": 1, "fills": []}
    return _history(value)


def _positions(rows):
    positions = {}
    for row in sorted(rows, key=lambda r: (timestamp(r["filled_at"]), r["sequence"])):
        p = positions.setdefault(row["symbol"], {"quantity": 0, "cost": Decimal(0), "opened_at": None,
            "risk_reference": None, "risk_reference_status": "NOT_PROVIDED"})
        qty, price, fees = row["quantity"], Decimal(str(row["price"])), Decimal(str(row["fees"]))
        if row["side"] == "BUY":
            reference = row.get("risk_reference")
            if not p["quantity"]:
                p.update(opened_at=row["filled_at"], risk_reference=deepcopy(reference),
                    risk_reference_status="USER_DEFINED" if reference else "NOT_PROVIDED")
            elif reference and reference != p["risk_reference"]:
                # Never silently redraw the original holding's risk level.
                p.update(risk_reference=None, risk_reference_status="CONFLICT_REVIEW")
            p["quantity"] += qty
            p["cost"] += qty * price + fees
        else:
            if qty > p["quantity"]:
                raise DataError("MANUAL_SELL_EXCEEDS_CONFIRMED_HOLDING")
            average = p["cost"] / p["quantity"]
            p["quantity"] -= qty
            p["cost"] -= qty * average
        p["last_fill_at"] = row["filled_at"]
    return {symbol: {"symbol": symbol, "quantity": p["quantity"], "currency": "JPY",
        "average_cost": float(p["cost"] / p["quantity"]), "total_cost": float(p["cost"]),
        "opened_at": p["opened_at"], "last_fill_at": p["last_fill_at"], "source": SOURCE,
        "risk_reference": deepcopy(p["risk_reference"]), "risk_reference_status": p["risk_reference_status"]}
        for symbol, p in positions.items() if p["quantity"]}


def snapshot(value, as_of):
    value = _history(value)
    cutoff = timestamp(as_of)
    rows = deepcopy([r for r in value["fills"] if r["confirmed"] is True
        and timestamp(r["filled_at"]) <= cutoff and timestamp(r["recorded_at"]) <= cutoff])
    body = {"schema_version": 1, "as_of": as_of, "positions": _positions(rows),
        "confirmed_fill_count": len(rows), "confirmed_fills": rows, "visible_fills_digest": digest(rows),
        "unconfirmed_fills_ignored": sum(not r["confirmed"] for r in value["fills"] if timestamp(r["recorded_at"]) <= cutoff),
        "execution_enabled": False}
    return {**body, "digest": digest(body)}


def verify_snapshot(value, as_of=None):
    """Check schema, digest, cutoff and recompute positions from confirmed fills.

    This verifies a declared manual ledger, not real-world broker execution.
    An old snapshot must be rebuilt for a different decision cutoff.
    """
    fields = {"schema_version", "as_of", "positions", "confirmed_fill_count", "confirmed_fills",
        "visible_fills_digest", "unconfirmed_fills_ignored", "execution_enabled", "digest"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DataError("INVALID_MANUAL_PORTFOLIO_SNAPSHOT")
    cutoff = timestamp(value["as_of"])
    if as_of is not None and cutoff != timestamp(as_of):
        raise DataError("MANUAL_PORTFOLIO_CUTOFF_MISMATCH")
    if (value["execution_enabled"] is not False or type(value["confirmed_fill_count"]) is not int
            or type(value["unconfirmed_fills_ignored"]) is not int
            or not 0 <= value["unconfirmed_fills_ignored"] <= MAX_FILLS):
        raise DataError("INVALID_MANUAL_PORTFOLIO_SNAPSHOT")
    rows = value["confirmed_fills"]
    _history({"schema_version": 1, "fills": rows}, contiguous=False)
    if any(r["confirmed"] is not True or timestamp(r["filled_at"]) > cutoff or timestamp(r["recorded_at"]) > cutoff for r in rows):
        raise DataError("MANUAL_PORTFOLIO_HAS_NONVISIBLE_FILL")
    try:
        valid = (value["confirmed_fill_count"] == len(rows) and value["visible_fills_digest"] == digest(rows)
            and digest(value["positions"]) == digest(_positions(rows))
            and value["digest"] == digest({k: v for k, v in value.items() if k != "digest"}))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise DataError("MANUAL_PORTFOLIO_SNAPSHOT_INTEGRITY_FAILED")
    return deepcopy(value)


def read_snapshot(path, as_of):
    return snapshot(load_fills(path), as_of)


def record_fill(path, entry, *, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    entry = _validate(entry)
    if timestamp(entry["filled_at"]) > timestamp(now):
        raise DataError("MANUAL_FILL_CANNOT_BE_IN_FUTURE")
    if entry.get("risk_reference") and timestamp(entry["risk_reference"]["defined_at"]) > timestamp(now):
        raise DataError("MANUAL_RISK_NOT_YET_KNOWN")
    path = Path(path)
    with pointer_lock(path.with_suffix(".lock")):
        value = load_fills(path)
        for existing in value["fills"]:
            if existing["id"] == entry["id"]:
                if {k: v for k, v in existing.items() if k not in {"recorded_at", "sequence"}} != entry:
                    raise DataError("MANUAL_FILL_IDEMPOTENCY_CONFLICT")
                return deepcopy(existing)
        if len(value["fills"]) >= MAX_FILLS:
            raise DataError("MANUAL_FILL_HISTORY_LIMIT")
        row = {**entry, "recorded_at": now, "sequence": len(value["fills"]) + 1}
        value["fills"].append(row)
        snapshot(value, now)  # Reject overselling/backdated inconsistent history before saving.
        atomic_json(path, value)
        return deepcopy(row)


def _holding_packet(report, item, position, portfolio):
    from .agents import validate_reviews
    from .decision import _packet_for
    packet = _packet_for(report, item)
    if packet is None or packet["core"].get("portfolio_position") != position or packet["core"].get("portfolio_snapshot_digest") != portfolio["digest"]:
        return None, {}, ["HOLDING_PACKET_INTEGRITY_FAILED"]
    raw = item.get("agent_reviews", {})
    if not isinstance(raw, dict):
        return packet, {}, ["HOLDING_REVIEW_INVALID"]
    accepted, errors = validate_reviews(packet, list(raw.values()))
    return packet, accepted, list(item.get("agent_errors", [])) + errors


def _risk_status(item, position, as_of):
    reference = position.get("risk_reference")
    if not reference or position.get("risk_reference_status") != "USER_DEFINED":
        return None, "POSITION_RISK_BASIS_UNSPECIFIED"
    if reference["price_basis"] != item.get("price_basis") or timestamp(reference["defined_at"]) > timestamp(as_of):
        return None, "POSITION_RISK_BASIS_MISMATCH"
    if reference["channel"] == "ATR":
        technical = item.get("technical", {})
        candidates = [technical.get("support") or {}, *technical.get("episodes", [])]
        anchor_key = "anchor_at"
    elif reference["channel"] == "H-001":
        pattern = next((p for p in item.get("patterns", []) if p.get("pattern_id") == "H-001"), {})
        candidates = [pattern.get("support") or {}, *pattern.get("episodes", [])]
        anchor_key = "anchor_session"
    else:
        return reference, None
    if not any(c.get(anchor_key) == reference["anchor_at"] and _money(c.get("lower"))
            and math.isclose(c["lower"], reference["lower"], rel_tol=1e-10, abs_tol=1e-10) for c in candidates):
        return None, "POSITION_RISK_BASIS_MISMATCH"
    return reference, None


def attach_manual_actions(report):
    """Produce manual review suggestions; never infer fills or submit orders."""
    portfolio = verify_snapshot(report["portfolio_snapshot"], report["as_of"])
    positions = portfolio["positions"]
    actions, seen = [], set()
    labels = {"ENTRY_REVIEW": "進場評估", "EXIT_REVIEW": "退出評估", "REDUCE_REVIEW": "減碼評估",
        "HOLD": "續抱評估", "REVIEW": "持倉資料／判斷待確認", "WAIT": "等待", "AVOID": "暫不進場"}
    for item in report["securities"]:
        symbol = item["symbol"]
        seen.add(symbol)
        position = positions.get(symbol)
        decision = item.get("trade_decision", {})
        views, reasons, action = {}, [], "REVIEW"
        if position:
            packet, reviews, errors = _holding_packet(report, item, position, portfolio)
            views = {k: deepcopy(v.get("position_view")) for k, v in reviews.items()}
            reference, risk_error = _risk_status(item, position, report["as_of"])
            ledger = report.get("ledger", {})
            ledger_rows = [r for r in ledger.get("rows", []) if r.get("symbol") == symbol]
            technical = item.get("technical", {})
            latest_bar = next((b for b in (packet or {}).get("core", {}).get("visible_series", {}).get("bars", [])
                if b.get("session") == technical.get("latest_session")), None)
            predates_fill = latest_bar is not None and timestamp(latest_bar["closed_at"]) < timestamp(position["last_fill_at"])
            data_bad = (item.get("data_status") not in {"VALID", "OBSERVATION"}
                or technical.get("latest_close") is None or technical.get("latest_session") is None
                or predates_fill
                or (report.get("collection", {}).get("expected_latest_session") is not None
                    and technical.get("latest_session") != report["collection"]["expected_latest_session"]))
            ledger_bad = (bool(ledger.get("errors")) or ledger.get("status") == "DEGRADED"
                or any(r.get("change") in {"STALE_IGNORED", "REVISION_REVIEW"} or r.get("revision_review") for r in ledger_rows))
            if data_bad:
                reasons = ["PRICE_PREDATES_LAST_CONFIRMED_FILL" if predates_fill else "PRICE_DATA_UNAVAILABLE_OR_STALE"]
            elif ledger_bad:
                reasons = ["HOLDING_LEDGER_REQUIRES_REVIEW"]
            elif packet is None or errors:
                reasons = errors or ["HOLDING_PACKET_INTEGRITY_FAILED"]
            elif risk_error:
                reasons = [risk_error]
            elif technical["latest_close"] < reference["lower"]:
                action, reasons = "EXIT_REVIEW", ["USER_RECORDED_RISK_LEVEL_BREACHED_AT_DAILY_CLOSE"]
            elif report.get("collection", {}).get("status") not in (None, "OK", "COMPLETE"):
                reasons = ["HOLDING_COLLECTION_REQUIRES_REVIEW"]
            elif set(reviews) != {"researcher", "reviewer"} or any(v is None for v in views.values()):
                reasons = ["HOLDING_REVIEW_INCOMPLETE"]
            else:
                proposed = [v["action"] for v in views.values()]
                reasons = [v["reason"] for v in views.values()]
                if "EXIT_REVIEW" in proposed:
                    action = "EXIT_REVIEW"
                elif "REDUCE_REVIEW" in proposed:
                    action = "REDUCE_REVIEW"
                elif (proposed == ["HOLD", "HOLD"] and not item.get("context_blockers")
                        and not any(r["missing_data"] or r["verdict"] == "REJECT" for r in reviews.values())):
                    action = "HOLD"
                else:
                    reasons.append("HOLDING_INFORMATION_OR_AGENT_CONSENSUS_INCOMPLETE")
                if len(set(proposed)) > 1:
                    reasons.append("POSITION_AGENT_DISAGREEMENT")
        else:
            action = "ENTRY_REVIEW" if decision.get("conclusion") == "CONSIDER" else "AVOID" if decision.get("conclusion") == "NO_TRADE" else "WAIT"
            reasons = decision.get("reason_codes", [])
            opportunity = item.get("opportunity_decision", {})
            if opportunity.get("action") == "PROBE_BUY_REVIEW":
                action, reasons = "ENTRY_REVIEW", opportunity["reason_codes"]
            elif opportunity.get("candidate"):
                action, reasons = ("AVOID" if opportunity.get("action") == "AVOID" else "WAIT"), opportunity["reason_codes"]
        value = {"symbol": symbol, "action": action, "label": labels[action], "reasons": reasons,
            "position": deepcopy(position), "agent_position_views": views,
            "entry_qualification": decision.get("conclusion"), "as_of": report["as_of"],
            "execution_enabled": False, "filled": False,
            "meaning": "人工評估建議，不是訂單；只有你確認成交後才更新持倉。"}
        item["manual_action"] = value
        actions.append(value)
    for symbol in sorted(set(positions) - seen):
        actions.append({"symbol": symbol, "action": "REVIEW", "label": labels["REVIEW"],
            "reasons": ["PORTFOLIO_SYMBOL_NOT_OBSERVED"], "position": deepcopy(positions[symbol]),
            "agent_position_views": {}, "entry_qualification": None, "as_of": report["as_of"],
            "execution_enabled": False, "filled": False,
            "meaning": "本輪沒有此持倉行情；持倉仍保留，不假設已出清。"})
    report["manual_actions"] = actions
    return report
