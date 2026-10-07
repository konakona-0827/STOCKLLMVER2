from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
import time
from typing import Any, Callable

from .capital_crosscheck import CrossCheckResult, recover_unique_new_tc_order
from .capital_reply_session import CapitalReplySession
from .models import ExecutionStatus, Side


SEQ13_RE = re.compile(r"(?<!\d)(\d{13})(?!\d)")


class CapitalExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SendResult:
    return_code: int | None
    message: str
    seq13: str | None
    raw_repr: str


@dataclass(frozen=True)
class RealOrderResult:
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    send_return_code: int | None
    send_message: str
    seq13: str | None
    crosscheck: CrossCheckResult
    status: ExecutionStatus
    filled_quantity: int
    broker_order_sent: bool
    crosscheck_performed: bool
    automatic_retry_allowed: bool = False


def extract_seq13(text: Any) -> str | None:
    m = SEQ13_RE.search(str(text or ""))
    return m.group(1) if m else None


def parse_send_result(result: Any) -> SendResult:
    message = ""
    rc = None
    if isinstance(result, tuple):
        if result:
            message = str(result[0] or "")
        if len(result) >= 2:
            try:
                rc = int(result[-1])
            except (TypeError, ValueError):
                rc = None
    else:
        try:
            rc = int(result)
        except (TypeError, ValueError):
            pass
    return SendResult(rc, message, extract_seq13(message), repr(result))


def validate_real_order(symbol, side, quantity, price):
    sym = str(symbol).strip().upper()
    if not sym or not re.fullmatch(r"[0-9A-Za-z._-]{1,16}", sym):
        raise CapitalExecutionError(f"Invalid symbol: {symbol!r}")

    try:
        side = side if isinstance(side, Side) else Side(str(side).strip().upper())
    except ValueError as exc:
        raise CapitalExecutionError("side must be BUY or SELL") from exc

    try:
        qty = int(quantity)
    except (TypeError, ValueError) as exc:
        raise CapitalExecutionError("quantity must be integer") from exc
    if not 1 <= qty <= 999:
        raise CapitalExecutionError("intraday odd-lot quantity must be 1..999")

    try:
        px = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapitalExecutionError("price must be numeric") from exc
    if px <= 0:
        raise CapitalExecutionError("price must be > 0")

    return sym, side, qty, px


class CapitalOddLotExecutor:
    def __init__(self, session: CapitalReplySession) -> None:
        self.session = session

    def send_limit_order(
        self,
        *,
        symbol,
        side,
        quantity,
        price,
        observe_seconds=20.0,
        on_submitted: Callable[[SendResult], None] | None = None,
    ) -> RealOrderResult:
        sym, side, qty, px = validate_real_order(symbol, side, quantity, price)

        # Cheap pre-order liveness check. Reuse a healthy login and, if needed,
        # reconnect only SKReply. Never perform a blind second SKCenterLib_Login.
        try:
            self.session.ensure_ready()
        except Exception as exc:
            raise CapitalExecutionError(
                f"Capital session health check failed: {type(exc).__name__}: {exc}"
            ) from exc

        before_tc = self.session.tc_rows_copy()

        order = self.session._sk.STOCKORDER()
        order.bstrFullAccount = self.session.account
        order.bstrStockNo = sym
        order.sPeriod = 4
        order.sFlag = 0
        order.sBuySell = 0 if side == Side.BUY else 1
        order.bstrPrice = format(px, "f")
        order.nQty = qty
        if hasattr(order, "sPrime"):
            order.sPrime = 0
        if hasattr(order, "nTradeType"):
            order.nTradeType = 0
        if hasattr(order, "nSpecialTradeType"):
            order.nSpecialTradeType = 2

        raw = self.session._skO.SendStockOddLotOrder(
            self.session.user,
            False,
            order,
        )
        sent = parse_send_result(raw)

        # Persist SUBMITTED immediately, before waiting for cross-check.
        if on_submitted is not None:
            on_submitted(sent)

        # For a known SEQ13, stop waiting as soon as the broker reply stream
        # proves ACK/CANCEL/DEAL. observe_seconds is a timeout, not a fixed sleep.
        cross = None
        if sent.seq13:
            deadline = time.time() + max(0.0, float(observe_seconds))
            while time.time() < deadline:
                self.session.pump(min(0.5, max(0.0, deadline - time.time())))
                probe = self.session.crosscheck_seq13(
                    sent.seq13,
                    include_report9=False,
                )
                if probe.status != ExecutionStatus.UNCONFIRMED:
                    cross = probe
                    break

            if cross is None:
                cross = self.session.crosscheck_seq13(
                    sent.seq13,
                    include_report9=True,
                )
        else:
            # Without SEQ13, collect for the timeout and use the conservative
            # before/after unique-new-order fallback.
            self.session.pump(observe_seconds)
            after_tc = self.session.tc_rows_copy()
            cross = recover_unique_new_tc_order(
                before_rows=before_tc,
                after_rows=after_tc,
                matcher=lambda row: (
                    row.symbol == sym
                    and row.buy_sell == ("B" if side == Side.BUY else "S")
                    and row.quantity == qty
                ),
                replay_complete=self.session.snapshot.replay_complete,
            )

        filled = min(qty, max(0, cross.filled_quantity))

        if sent.return_code not in (0, None):
            final = ExecutionStatus.FAILED
        elif filled >= qty:
            final = ExecutionStatus.FILLED
        elif filled > 0:
            final = ExecutionStatus.PARTIALLY_FILLED
        else:
            final = cross.status

        return RealOrderResult(
            symbol=sym,
            side=side,
            quantity=qty,
            price=px,
            send_return_code=sent.return_code,
            send_message=sent.message,
            seq13=sent.seq13 or cross.broker_seq13,
            crosscheck=cross,
            status=final,
            filled_quantity=filled,
            broker_order_sent=True,
            crosscheck_performed=True,
            automatic_retry_allowed=False,
        )
