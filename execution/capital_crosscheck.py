from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Sequence
import re

from .models import ExecutionStatus


SEQ13_RE = re.compile(r"(?<!\d)(\d{13})(?!\d)")


class CrossCheckError(ValueError):
    pass


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ReplyRow:
    raw: str
    parts: tuple[str, ...]
    key_no: str | None
    market: str | None
    event_type: str | None
    order_error: str | None
    buy_sell: str | None
    symbol: str | None
    price: Decimal | None
    quantity: int | None
    seq13_candidates: tuple[str, ...]

    @property
    def is_tc(self) -> bool:
        return self.market == "TC"

    @property
    def is_normal_order(self) -> bool:
        return self.is_tc and self.event_type == "N" and self.order_error == "N"

    @property
    def is_fill(self) -> bool:
        return self.is_tc and self.event_type == "D"

    @property
    def is_cancel(self) -> bool:
        return self.is_tc and self.event_type == "C"

    def contains_seq(self, seq13: str) -> bool:
        return seq13 in self.seq13_candidates or seq13 in self.raw


@dataclass(frozen=True)
class CrossCheckResult:
    status: ExecutionStatus
    expected_seq13: str | None
    broker_seq13: str | None
    matched_live_rows: tuple[str, ...] = ()
    matched_replay_rows: tuple[str, ...] = ()
    accepted_rows: tuple[str, ...] = ()
    fill_rows: tuple[str, ...] = ()
    cancel_rows: tuple[str, ...] = ()
    replay_complete: bool = False
    report9_contains_seq: bool | None = None
    used_fallback: bool = False
    fallback_row: str | None = None
    message: str = ""

    @property
    def confirmed_fill(self) -> bool:
        return bool(self.fill_rows)

    @property
    def filled_quantity(self) -> int:
        total = 0
        for raw in self.fill_rows:
            qty = parse_reply_row(raw).quantity
            if qty is not None and qty > 0:
                total += qty
        return total


def parse_reply_row(raw: str) -> ReplyRow:
    """Parse fields documented for SKReplyLib.OnNewData.

    Zero-based indices used here:
      0 KeyNo
      1 MarketType
      2 Type
      3 OrderErr
      6 BuySell
      8 ComId
      11 Price
      20 Qty

    Unknown/later columns are intentionally left opaque.
    """
    text = str(raw)
    parts = tuple(text.split(","))

    def at(i: int) -> str | None:
        return parts[i].strip() if len(parts) > i else None

    return ReplyRow(
        raw=text,
        parts=parts,
        key_no=at(0),
        market=at(1),
        event_type=at(2),
        order_error=at(3),
        buy_sell=at(6),
        symbol=at(8),
        price=_decimal_or_none(at(11)),
        quantity=_int_or_none(at(20)),
        seq13_candidates=tuple(dict.fromkeys(SEQ13_RE.findall(text))),
    )


def validate_seq13(seq13: str) -> str:
    seq = str(seq13).strip()
    if not re.fullmatch(r"\d{13}", seq):
        raise CrossCheckError("SEQ13 must be exactly 13 digits")
    return seq


def _matching_rows(rows: Iterable[str], seq13: str) -> list[ReplyRow]:
    return [r for r in (parse_reply_row(x) for x in rows) if r.contains_seq(seq13)]


def evaluate_seq13(
    seq13: str,
    *,
    live_rows: Iterable[str] = (),
    replay_rows: Iterable[str] = (),
    replay_complete: bool = False,
    get_order_report9: str | None = None,
) -> CrossCheckResult:
    seq = validate_seq13(seq13)
    live = _matching_rows(live_rows, seq)
    replay = _matching_rows(replay_rows, seq)
    all_matches = live + replay

    accepted = [r for r in all_matches if r.is_normal_order]
    fills = [r for r in all_matches if r.is_fill]
    cancels = [r for r in all_matches if r.is_cancel]

    report_contains = None if get_order_report9 is None else seq in str(get_order_report9)

    if fills:
        status = ExecutionStatus.PARTIALLY_FILLED
        message = "Exact SEQ13 has one or more TC deal events."
    elif cancels:
        status = ExecutionStatus.CANCELLED
        message = "Exact SEQ13 found in TC cancel event."
    elif accepted:
        status = ExecutionStatus.ACKNOWLEDGED
        message = "Exact SEQ13 found in normal TC order event (N/N)."
    else:
        status = ExecutionStatus.UNCONFIRMED
        message = (
            "SEQ13 not proven by TC live/replay evidence; "
            "treat as UNCONFIRMED and do not auto-retry."
        )

    return CrossCheckResult(
        status=status,
        expected_seq13=seq,
        broker_seq13=seq if status != ExecutionStatus.UNCONFIRMED else None,
        matched_live_rows=tuple(r.raw for r in live),
        matched_replay_rows=tuple(r.raw for r in replay),
        accepted_rows=tuple(r.raw for r in accepted),
        fill_rows=tuple(r.raw for r in fills),
        cancel_rows=tuple(r.raw for r in cancels),
        replay_complete=replay_complete,
        report9_contains_seq=report_contains,
        message=message,
    )


def multiset_new_rows(before_rows: Sequence[str], after_rows: Sequence[str]) -> list[str]:
    remaining = Counter(str(x) for x in before_rows)
    out: list[str] = []
    for raw in after_rows:
        text = str(raw)
        if remaining[text] > 0:
            remaining[text] -= 1
        else:
            out.append(text)
    return out


def recover_unique_new_tc_order(
    *,
    before_rows: Sequence[str],
    after_rows: Sequence[str],
    matcher: Callable[[ReplyRow], bool],
    replay_complete: bool,
) -> CrossCheckResult:
    if not replay_complete:
        return CrossCheckResult(
            status=ExecutionStatus.UNCONFIRMED,
            expected_seq13=None,
            broker_seq13=None,
            replay_complete=False,
            used_fallback=True,
            message="Fallback blocked: replay/backfill is not complete.",
        )

    candidates = [
        parse_reply_row(raw)
        for raw in multiset_new_rows(before_rows, after_rows)
    ]
    candidates = [r for r in candidates if r.is_normal_order and matcher(r)]

    if len(candidates) != 1:
        return CrossCheckResult(
            status=ExecutionStatus.UNCONFIRMED,
            expected_seq13=None,
            broker_seq13=None,
            replay_complete=True,
            used_fallback=True,
            message=f"Fallback needs exactly one new matching normal TC row; found {len(candidates)}.",
        )

    row = candidates[0]
    if len(row.seq13_candidates) != 1:
        return CrossCheckResult(
            status=ExecutionStatus.UNCONFIRMED,
            expected_seq13=None,
            broker_seq13=None,
            accepted_rows=(row.raw,),
            replay_complete=True,
            used_fallback=True,
            fallback_row=row.raw,
            message="Fallback row has no unique SEQ13.",
        )

    return CrossCheckResult(
        status=ExecutionStatus.ACKNOWLEDGED,
        expected_seq13=None,
        broker_seq13=row.seq13_candidates[0],
        accepted_rows=(row.raw,),
        replay_complete=True,
        used_fallback=True,
        fallback_row=row.raw,
        message="Recovered one unique SEQ13 from one new matching normal TC row.",
    )
