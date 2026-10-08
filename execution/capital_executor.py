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
    broker_order_sent: bool | None
    crosscheck_performed: bool
    automatic_retry_allowed: bool = False
    send_attempted: bool = False

    @property
    def changes_position(self) -> bool:
        return self.filled_quantity > 0 and self.status in (
            ExecutionStatus.PARTIALLY_FILLED, ExecutionStatus.FILLED,
        )


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

        # SKCOM's synchronous return code is authoritative: a non-zero code
        # means the broker rejected the request. Do not persist SUBMITTED or
        # cross-check a rejected request as though an order had been accepted.
        if sent.return_code not in (0, None):
            cross = CrossCheckResult(
                status=ExecutionStatus.FAILED,
                expected_seq13=sent.seq13,
                broker_seq13=None,
                message=f"SendStockOddLotOrder rejected the request: rc={sent.return_code}",
            )
            return RealOrderResult(
                symbol=sym,
                side=side,
                quantity=qty,
                price=px,
                send_return_code=sent.return_code,
                send_message=sent.message,
                seq13=sent.seq13,
                crosscheck=cross,
                status=ExecutionStatus.FAILED,
                filled_quantity=0,
                broker_order_sent=False,
                crosscheck_performed=False,
                automatic_retry_allowed=False,
                send_attempted=True,
            )

        # Persist SUBMITTED immediately, before waiting for cross-check.
        if sent.return_code == 0 and on_submitted is not None:
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
                # ACK only proves that the order exists. Keep observing for
                # an actual fill, including additional partial-fill events.
                if probe.filled_quantity >= qty or probe.status in (
                    ExecutionStatus.CANCELLED, ExecutionStatus.FAILED,
                ):
                    break

            if cross is None or cross.filled_quantity < qty:
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

        if sent.return_code is None:
            # The call happened, but its acceptance was not established.
            # Keep the intent claimed and require reconciliation; never retry.
            final = ExecutionStatus.UNCONFIRMED
            filled = 0
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
            broker_order_sent=None if sent.return_code is None else sent.return_code == 0,
            crosscheck_performed=True,
            automatic_retry_allowed=False,
            send_attempted=True,
        )
