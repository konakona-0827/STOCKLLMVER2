from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable
import time

from .capital_reply_session import CapitalReplySession, CapitalReplySessionError


class AccountCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuyingPower:
    verified: bool
    one_account: bool | None
    balance_twd: Decimal | None
    withdrawable_twd: Decimal | None
    available_to_buy_twd: Decimal | None
    raw: str
    reason: str = ""


@dataclass(frozen=True)
class InventoryRow:
    symbol: str
    inventory_type: str
    yesterday_qty: int
    today_order_buy: int
    today_order_sell: int
    today_buy_fill: int
    today_sell_fill: int
    sellable_qty: int
    realtime_qty: int
    raw: str


@dataclass(frozen=True)
class SettlementDue:
    trade_date: str
    settlement_date: str
    net_twd: Decimal


def _roc_date(value: str) -> str:
    digits = value.strip()
    if len(digits) != 7 or not digits.isdigit():
        raise AccountCheckError("Invalid broker settlement date")
    return date(int(digits[:3]) + 1911, int(digits[3:5]),
                int(digits[5:7])).isoformat()


def parse_get_t3_due_amt(raw: str) -> list[SettlementDue]:
    """Parse the broker's T/T-1/T-2 TWD net settlement amounts."""
    text = str(raw or "").strip().rstrip(".")
    if not text or text == "-1":
        raise AccountCheckError("GetT3DueAmt returned no settlement data")
    twd = []
    for section in text.split(";"):
        if not section.strip():
            continue
        fields = [part.strip() for part in section.split(",")]
        if len(fields) != 10:
            raise AccountCheckError("Unexpected GetT3DueAmt format")
        if fields[9] not in ("臺幣", "台幣", "TWD", "NTD"):
            continue
        for start in (0, 3, 6):
            trade, settle, amount = fields[start:start + 3]
            if not trade and not settle and not amount:
                continue
            try:
                value = Decimal(amount)
                if not value.is_finite():
                    raise InvalidOperation
                twd.append(SettlementDue(_roc_date(trade),
                                         _roc_date(settle), value))
            except (InvalidOperation, ValueError) as exc:
                raise AccountCheckError("Invalid GetT3DueAmt amount/date") from exc
    if not twd:
        raise AccountCheckError("GetT3DueAmt has no TWD settlement rows")
    return twd


def query_settlement_dues(session: CapitalReplySession) -> list[SettlementDue]:
    if not session.connected or session._skO is None or not session.account:
        raise AccountCheckError("Capital session is not connected")
    if not hasattr(session._skO, "GetT3DueAmt"):
        raise AccountCheckError("SKOrderLib.GetT3DueAmt is unavailable")
    try:
        raw = session._skO.GetT3DueAmt(session.user, session.account)
    except Exception as exc:
        raise AccountCheckError(
            f"GetT3DueAmt exception: {type(exc).__name__}: {exc}") from exc
    return parse_get_t3_due_amt(str(raw or ""))


def _dec(value: str) -> Decimal:
    return Decimal(value.strip())


def _int(value: str) -> int:
    return int(Decimal(value.strip()))


def parse_get_balance(raw: str) -> BuyingPower:
    text = str(raw or "").strip()
    if not text or text == "-1":
        return BuyingPower(False, None, None, None, None, text, "GetBalance failed")

    if text.endswith("."):
        text = text[:-1]

    p = [x.strip() for x in text.split(",")]
    if len(p) < 4:
        return BuyingPower(False, None, None, None, None, raw, "Unexpected GetBalance format")

    flag = p[0].lower()
    one = True if flag == "true" else False if flag == "false" else None
    try:
        balance = _dec(p[1])
        withdrawable = _dec(p[2])
        available = _dec(p[3])
    except (InvalidOperation, ValueError):
        return BuyingPower(False, one, None, None, None, raw, "Invalid numeric fields")
    if not all(value.is_finite() for value in (balance, withdrawable, available)) or available < 0:
        return BuyingPower(False, one, None, None, None, raw,
                           "Non-finite or negative available buying power")

    # GetBalance is specifically the 一戶通 balance/available-amount query.
    if one is not True:
        return BuyingPower(
            False, one, balance, withdrawable, available, raw,
            "Account is not verified as 一戶通; BUY blocked conservatively."
        )

    return BuyingPower(True, one, balance, withdrawable, available, raw)


def query_buying_power(session: CapitalReplySession) -> BuyingPower:
    if not session.connected or session._skO is None:
        raise AccountCheckError("Capital session is not connected.")
    if not hasattr(session._skO, "GetBalance"):
        raise AccountCheckError("SKOrderLib.GetBalance is unavailable.")

    try:
        raw = session._skO.GetBalance(session.user)
    except Exception as exc:
        raise AccountCheckError(f"GetBalance exception: {type(exc).__name__}: {exc}") from exc

    result = parse_get_balance(str(raw or ""))
    if not result.verified:
        raise AccountCheckError(result.reason or "Buying power is not verified.")
    return result


def parse_real_balance_row(raw: str) -> InventoryRow | None:
    text = str(raw or "").strip()
    if not text or text.startswith("##"):
        return None
    p = [x.strip() for x in text.split(",")]
    if len(p) < 15:
        return None

    try:
        return InventoryRow(
            symbol=p[0].upper(),
            inventory_type=p[1].upper(),
            yesterday_qty=_int(p[6]),
            today_order_buy=_int(p[7]),
            today_order_sell=_int(p[8]),
            today_buy_fill=_int(p[9]),
            today_sell_fill=_int(p[10]),
            sellable_qty=_int(p[11]),
            realtime_qty=_int(p[14]),
            raw=text,
        )
    except (InvalidOperation, ValueError):
        return None


def query_inventory(
    session: CapitalReplySession,
    *,
    max_attempts: int = 2,
    retry_delay_seconds: float = 0.8,
) -> list[InventoryRow]:
    """Read current inventory.

    rc=1019 means a broker query is still being processed / called too soon.
    It is safe to retry this READ-ONLY query briefly. This is not an order retry.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            raws = session.query_real_balance_rows()
            rows = [parse_real_balance_row(x) for x in raws]
            malformed = [raw for raw, row in zip(raws, rows)
                         if str(raw).strip() and not str(raw).startswith("##")
                         and row is None]
            if malformed:
                raise AccountCheckError(
                    f"GetRealBalanceReport has {len(malformed)} unparsed rows; "
                    "inventory is not trustworthy"
                )
            return [x for x in rows if x is not None]
        except CapitalReplySessionError as exc:
            last_exc = exc
            message = str(exc)
            retryable_1019 = "rc=1019" in message
            if not retryable_1019 or attempt >= max_attempts:
                raise AccountCheckError(message) from exc

            # Let COM callbacks / broker query state settle, then retry once.
            try:
                session.pump(retry_delay_seconds)
            except Exception:
                time.sleep(retry_delay_seconds)

    raise AccountCheckError(str(last_exc or "Inventory query failed"))


def cash_sellable_qty(rows: Iterable[InventoryRow], symbol: str) -> int:
    wanted = symbol.strip().upper()
    # T = 集保/cash inventory. AI executor does not auto-sell financing/short inventory.
    return sum(
        max(0, row.sellable_qty)
        for row in rows
        if row.symbol == wanted and row.inventory_type == "T"
    )


def realtime_cash_qty(rows: Iterable[InventoryRow], symbol: str) -> int:
    wanted = symbol.strip().upper()
    return sum(
        max(0, row.realtime_qty)
        for row in rows
        if row.symbol == wanted and row.inventory_type == "T"
    )
