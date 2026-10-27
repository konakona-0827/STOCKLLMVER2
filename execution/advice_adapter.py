from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .models import AdviceIntent, Side


SUPPORTED_INTERFACE_VERSIONS = {"1.0"}


class AdviceInterfaceError(ValueError):
    pass


def _decimal(value: Any, field: str, *, allow_none: bool = True) -> Decimal | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise AdviceInterfaceError(f"{field} is required")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AdviceInterfaceError(f"{field} must be numeric: {value!r}") from exc


def _int(value: Any, field: str, *, allow_none: bool = True) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise AdviceInterfaceError(f"{field} is required")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise AdviceInterfaceError(f"{field} must be an integer: {value!r}") from exc
    return result


def _confidence_to_pct(value: Any, symbol: str) -> Decimal:
    """Accept current dashboard conventions safely.

    - 0..1   -> convert to 0..100
    - >1..100 -> already percentage
    Anything else is rejected.
    """
    raw = _decimal(value, f"{symbol}.confidence", allow_none=False)
    assert raw is not None

    if Decimal("0") <= raw <= Decimal("1"):
        return (raw * Decimal("100")).quantize(Decimal("0.01"))
    if Decimal("1") < raw <= Decimal("100"):
        return raw.quantize(Decimal("0.01"))

    raise AdviceInterfaceError(
        f"{symbol}.confidence must be 0..1 or 0..100, got {value!r}"
    )


def _validate_root(payload: Mapping[str, Any]) -> tuple[str, str]:
    version = str(payload.get("interface_version", "")).strip()
    if version not in SUPPORTED_INTERFACE_VERSIONS:
        raise AdviceInterfaceError(
            f"Unsupported interface_version={version!r}; "
            f"supported={sorted(SUPPORTED_INTERFACE_VERSIONS)}"
        )

    run_id = str(payload.get("run_id", "")).strip()
    if not run_id:
        raise AdviceInterfaceError("run_id is required")

    # Hard gates: execution must never consume an incomplete/invalid LLM run.
    if payload.get("run_status") != "READY":
        raise AdviceInterfaceError(
            f"run_status must be READY, got {payload.get('run_status')!r}"
        )
    if payload.get("llm_validation") != "VALID":
        raise AdviceInterfaceError(
            f"llm_validation must be VALID, got {payload.get('llm_validation')!r}"
        )

    # This handoff is intended for an analysis pipeline that itself does not trade.
    # If it says an order was already sent, stop instead of risking duplicate execution.
    if payload.get("real_order_sent") is True:
        raise AdviceInterfaceError(
            "real_order_sent=True: execution blocked to prevent duplicate-order risk"
        )

    return version, run_id


def build_advice_intents(payload: Mapping[str, Any]) -> list[AdviceIntent]:
    """Normalize advice_interface v1.0 into execution-layer intents.

    BUY/SELL become AdviceIntent objects.
    HOLD/WAIT are intentionally ignored here because they require no broker order.

    IMPORTANT:
    - This function does not calculate final order quantity.
    - It does not call SKCOM.
    - It does not update positions.
    - risk_guard.py will be authoritative for sizing and freshness.
    """
    version, run_id = _validate_root(payload)

    recommendations = payload.get("recommendations")
    if not isinstance(recommendations, list):
        raise AdviceInterfaceError("recommendations must be a list")

    intents: list[AdviceIntent] = []
    seen_trade_symbols: set[str] = set()

    for row in recommendations:
        if not isinstance(row, Mapping):
            raise AdviceInterfaceError("each recommendation must be an object")

        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            raise AdviceInterfaceError("recommendation.symbol is required")

        decision = str(row.get("decision", "WAIT")).strip().upper()
        if decision in {"HOLD", "WAIT"}:
            continue
        if decision not in {"BUY", "SELL"}:
            raise AdviceInterfaceError(
                f"{symbol}: unsupported decision {decision!r}"
            )

        if symbol in seen_trade_symbols:
            raise AdviceInterfaceError(
                f"{symbol}: duplicate BUY/SELL recommendation in one advice interface"
            )
        seen_trade_symbols.add(symbol)

        quote_status = str(row.get("quote_status", "UNAVAILABLE")).strip().upper()
        if not quote_status:
            quote_status = "UNAVAILABLE"

        confidence_pct = _confidence_to_pct(row.get("confidence", 0), symbol)

        suggested_qty = _int(row.get("suggested_qty"), f"{symbol}.suggested_qty")
        if suggested_qty is not None and suggested_qty < 0:
            raise AdviceInterfaceError(f"{symbol}.suggested_qty must be >= 0")

        ai_managed_qty = _int(row.get("ai_managed_qty"), f"{symbol}.ai_managed_qty")
        if ai_managed_qty is not None and ai_managed_qty < 0:
            raise AdviceInterfaceError(f"{symbol}.ai_managed_qty must be >= 0")

        action_ratio = _decimal(
            row.get("action_ratio", 0),
            f"{symbol}.action_ratio",
            allow_none=False,
        )
        assert action_ratio is not None
        if not Decimal("0") <= action_ratio <= Decimal("1"):
            raise AdviceInterfaceError(
                f"{symbol}.action_ratio must be between 0 and 1"
            )

        warnings = row.get("warnings") or []
        if not isinstance(warnings, list):
            raise AdviceInterfaceError(f"{symbol}.warnings must be a list")

        # The current interface does not yet expose a native decision_id.
        # Use a deterministic derived ID until upstream adds one.
        native_decision_id = str(row.get("decision_id", "")).strip()
        decision_id = native_decision_id or f"{run_id}:{symbol}"

        evidence_ids = row.get("evidence_ids") or []
        if not isinstance(evidence_ids, list):
            raise AdviceInterfaceError(f"{symbol}.evidence_ids must be a list")

        intents.append(
            AdviceIntent(
                run_id=run_id,
                decision_id=decision_id,
                interface_version=version,
                symbol=symbol,
                side=Side(decision),
                confidence_pct=confidence_pct,
                reason=str(row.get("reason", "")).strip(),
                analysis_source=(
                    None
                    if row.get("analysis_source") is None
                    else str(row.get("analysis_source"))
                ),
                warnings=tuple(str(x) for x in warnings),
                quote_status=quote_status,
                quote_timestamp=row.get("quote_timestamp"),
                quote_age_seconds=_decimal(
                    row.get("quote_age_seconds"),
                    f"{symbol}.quote_age_seconds",
                ),
                reference_price=_decimal(
                    row.get("reference_price"),
                    f"{symbol}.reference_price",
                ),
                suggested_price=_decimal(
                    row.get("suggested_price"),
                    f"{symbol}.suggested_price",
                ),
                suggested_qty=suggested_qty,
                action_ratio=action_ratio,
                ai_managed_qty=ai_managed_qty,
                evidence_ids=tuple(str(x) for x in evidence_ids if str(x).strip()),
                amount_twd=_decimal(row.get("amount_twd"), f"{symbol}.amount_twd"),
            )
        )

    return intents


def load_advice_file(path: str | Path) -> tuple[dict[str, Any], list[AdviceIntent]]:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AdviceInterfaceError("advice interface root must be a JSON object")
    return payload, build_advice_intents(payload)


def _intent_to_printable(intent: AdviceIntent) -> dict[str, Any]:
    return {
        "run_id": intent.run_id,
        "decision_id": intent.decision_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "confidence_pct": str(intent.confidence_pct),
        "quote_status": intent.quote_status,
        "quote_timestamp": intent.quote_timestamp,
        "quote_age_seconds": (
            None if intent.quote_age_seconds is None else str(intent.quote_age_seconds)
        ),
        "suggested_price": (
            None if intent.suggested_price is None else str(intent.suggested_price)
        ),
        "suggested_qty": intent.suggested_qty,
        "action_ratio": str(intent.action_ratio),
        "ai_managed_qty": intent.ai_managed_qty,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate advice_interface JSON and print normalized trade intents."
    )
    parser.add_argument("advice_json")
    args = parser.parse_args()

    _, intents = load_advice_file(args.advice_json)
    print(
        json.dumps(
            [_intent_to_printable(x) for x in intents],
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"TRADE_INTENT_COUNT={len(intents)}")
    print("BROKER_ORDER_SENT=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
