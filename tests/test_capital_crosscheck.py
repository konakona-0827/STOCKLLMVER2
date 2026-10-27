from __future__ import annotations

import unittest

from execution.capital_crosscheck import (
    CrossCheckError,
    evaluate_seq13,
    multiset_new_rows,
    parse_reply_row,
    recover_unique_new_tc_order,
)
from execution.models import ExecutionStatus


SEQ = "1234567890123"


def row(event_type="N", err="N", seq=SEQ, suffix="X"):
    # Only indexes 1,2,3 are interpreted by the cross-check module.
    return f"opaque,TC,{event_type},{err},foo,{seq},{suffix}"


class CapitalCrossCheckTests(unittest.TestCase):
    def test_parse_known_tc_fields(self):
        parsed = parse_reply_row(row())
        self.assertTrue(parsed.is_tc)
        self.assertTrue(parsed.is_normal_order)
        self.assertEqual((SEQ,), parsed.seq13_candidates)

    def test_exact_seq_normal_order_acknowledged(self):
        result = evaluate_seq13(
            SEQ,
            live_rows=[row("N", "N")],
            replay_complete=False,
        )
        self.assertEqual(ExecutionStatus.ACKNOWLEDGED, result.status)
        self.assertEqual(SEQ, result.broker_seq13)

    def test_exact_seq_fill_wins(self):
        result = evaluate_seq13(
            SEQ,
            replay_rows=[row("N", "N"), row("D", "N")],
            replay_complete=True,
        )
        self.assertEqual(ExecutionStatus.PARTIALLY_FILLED, result.status)
        self.assertTrue(result.confirmed_fill)

    def test_exact_seq_cancel(self):
        result = evaluate_seq13(
            SEQ,
            replay_rows=[row("N", "N"), row("C", "N")],
            replay_complete=True,
        )
        self.assertEqual(ExecutionStatus.CANCELLED, result.status)

    def test_report9_alone_does_not_confirm_tc(self):
        result = evaluate_seq13(
            SEQ,
            replay_rows=[],
            replay_complete=True,
            get_order_report9=f"something {SEQ}",
        )
        self.assertEqual(ExecutionStatus.UNCONFIRMED, result.status)
        self.assertTrue(result.report9_contains_seq)

    def test_missing_seq_after_complete_is_unconfirmed(self):
        result = evaluate_seq13(
            SEQ,
            replay_rows=[row(seq="9999999999999")],
            replay_complete=True,
        )
        self.assertEqual(ExecutionStatus.UNCONFIRMED, result.status)
        self.assertIsNone(result.broker_seq13)

    def test_invalid_seq_rejected(self):
        with self.assertRaises(CrossCheckError):
            evaluate_seq13("123")

    def test_multiset_diff_preserves_duplicate_semantics(self):
        a = row(suffix="A")
        b = row(suffix="B")
        self.assertEqual([a, b], multiset_new_rows([a], [a, a, b]))

    def test_fallback_recovers_exactly_one_unambiguous_new_tc(self):
        old = row(seq="9999999999999", suffix="OLD")
        new = row(seq=SEQ, suffix="NEW")
        result = recover_unique_new_tc_order(
            before_rows=[old],
            after_rows=[old, new],
            matcher=lambda r: r.raw.endswith("NEW"),
            replay_complete=True,
        )
        self.assertEqual(ExecutionStatus.ACKNOWLEDGED, result.status)
        self.assertEqual(SEQ, result.broker_seq13)
        self.assertTrue(result.used_fallback)

    def test_fallback_rejects_ambiguous_seq_candidates(self):
        new = f"opaque,TC,N,N,foo,{SEQ},other,9999999999999"
        result = recover_unique_new_tc_order(
            before_rows=[],
            after_rows=[new],
            matcher=lambda r: True,
            replay_complete=True,
        )
        self.assertEqual(ExecutionStatus.UNCONFIRMED, result.status)
        self.assertIsNone(result.broker_seq13)

    def test_fallback_requires_replay_complete(self):
        result = recover_unique_new_tc_order(
            before_rows=[],
            after_rows=[row()],
            matcher=lambda r: True,
            replay_complete=False,
        )
        self.assertEqual(ExecutionStatus.UNCONFIRMED, result.status)


if __name__ == "__main__":
    unittest.main()
