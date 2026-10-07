from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence
import re

from .models import ExecutionStatus


SEQ13_RE = re.compile(r"(?<!\d)(\d{13})(?!\d)")


class CrossCheckError(ValueError):
    pass


@dataclass(frozen=True)
class ReplyRow:
    """Parsed SKReplyLib OnNewData row.

    Only fields already verified by the existing probe are interpreted here:
      parts[1] == "TC"  -> intraday odd-lot
      parts[2]          -> event type (N/C/D)
      parts[3]          -> order error flag
    Other broker columns remain opaque until separately verified.
    """
    raw: str
    parts: tuple[str, ...]
    market: str | None
    event_type: str | None
    order_error: str | None
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
        return self.status == ExecutionStatus.FILLED and bool(self.fill_rows)

    @property
    def changes_position(self) -> bool:
        # Exact filled quantity/price will later come from the broker fill parser.
        # Cross-check alone never mutates the position.
        return False


def parse_reply_row(raw: str) -> ReplyRow:
    text = str(raw)
    parts = tuple(text.split(","))
    market = parts[1].strip() if len(parts) > 1 else None
    event_type = parts[2].strip() if len(parts) > 2 else None
    order_error = parts[3].strip() if len(parts) > 3 else None
    seqs = tuple(dict.fromkeys(SEQ13_RE.findall(text)))
    return ReplyRow(
        raw=text,
        parts=parts,
        market=market,
        event_type=event_type,
        order_error=order_error,
        seq13_candidates=seqs,
    )


def validate_seq13(seq13: str) -> str:
    seq = str(seq13).strip()
    if not re.fullmatch(r"\d{13}", seq):
        raise CrossCheckError("SEQ13 must be exactly 13 digits")
    return seq


def _matching_rows(rows: Iterable[str], seq13: str) -> list[ReplyRow]:
    out: list[ReplyRow] = []
    for raw in rows:
        row = parse_reply_row(raw)
        if row.contains_seq(seq13):
            out.append(row)
    return out


def evaluate_seq13(
    seq13: str,
    *,
    live_rows: Iterable[str] = (),
    replay_rows: Iterable[str] = (),
    replay_complete: bool = False,
    get_order_report9: str | None = None,
) -> CrossCheckResult:
    """Evaluate an exact broker sequence against observed SKReply evidence.

    TC precedence:
      D -> FILLED
      C -> CANCELLED (unless a D for the same seq is also present)
      N + OrderErr N -> ACKNOWLEDGED
      nothing provable -> UNCONFIRMED

    GetOrderReport(format=9) is recorded only as supplementary evidence here.
    It is not sufficient by itself to confirm a TC intraday odd-lot order.
    """
    seq = validate_seq13(seq13)

    live = _matching_rows(live_rows, seq)
    replay = _matching_rows(replay_rows, seq)
    all_matches = live + replay

    accepted = [r for r in all_matches if r.is_normal_order]
    fills = [r for r in all_matches if r.is_fill]
    cancels = [r for r in all_matches if r.is_cancel]

    report_contains = None
    if get_order_report9 is not None:
        report_contains = seq in str(get_order_report9)

    if fills:
        status = ExecutionStatus.FILLED
        message = "Exact SEQ13 found in TC fill event."
    elif cancels:
        status = ExecutionStatus.CANCELLED
        message = "Exact SEQ13 found in TC cancel event."
    elif accepted:
        status = ExecutionStatus.ACKNOWLEDGED
        message = "Exact SEQ13 found in normal TC order event (N/N)."
    else:
        status = ExecutionStatus.UNCONFIRMED
        if not replay_complete:
            message = (
                "SEQ13 not proven and replay is incomplete; "
                "treat as UNCONFIRMED and do not update positions."
            )
        elif report_contains:
            message = (
                "SEQ13 appears in GetOrderReport(9) but no exact TC SKReply evidence "
                "was found; report9 is supplementary only, so result is UNCONFIRMED."
            )
        else:
            message = (
                "SEQ13 not found in live/replay TC evidence after replay completion; "
                "treat as UNCONFIRMED."
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


def multiset_new_rows(
    before_rows: Sequence[str],
    after_rows: Sequence[str],
) -> list[str]:
    """Return rows newly present after the operation, preserving multiplicity/order."""
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
    """Conservative fallback when SendStockOddLotOrder returned no SEQ13.

    Recovery succeeds only when:
      1. replay/backfill is complete,
      2. exactly one NEW normal TC order row matches the caller's verified
         order fingerprint, and
      3. that row exposes exactly one unambiguous 13-digit sequence.

    The caller must provide `matcher` using fields whose broker-column mapping has
    already been verified.  This module intentionally does not guess unknown
    SKReply column positions.
    """
    if not replay_complete:
        return CrossCheckResult(
            status=ExecutionStatus.UNCONFIRMED,
            expected_seq13=None,
            broker_seq13=None,
            replay_complete=False,
            used_fallback=True,
            message="Fallback blocked: replay/backfill is not complete.",
        )

    candidates: list[ReplyRow] = []
    for raw in multiset_new_rows(before_rows, after_rows):
        row = parse_reply_row(raw)
        if row.is_normal_order and matcher(row):
            candidates.append(row)

    if len(candidates) != 1:
        return CrossCheckResult(
            status=ExecutionStatus.UNCONFIRMED,
            expected_seq13=None,
            broker_seq13=None,
            replay_complete=True,
            used_fallback=True,
            message=(
                f"Fallback requires exactly one new matching normal TC row; "
                f"found {len(candidates)}."
            ),
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
            message=(
                "One matching TC order row found, but SEQ13 is ambiguous or absent; "
                "do not infer broker sequence."
            ),
        )

    recovered = row.seq13_candidates[0]
    return CrossCheckResult(
        status=ExecutionStatus.ACKNOWLEDGED,
        expected_seq13=None,
        broker_seq13=recovered,
        accepted_rows=(row.raw,),
        replay_complete=True,
        used_fallback=True,
        fallback_row=row.raw,
        message="Recovered one unambiguous SEQ13 from one new matching normal TC row.",
    )
