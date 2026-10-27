from __future__ import annotations

import unittest

from execution.capital_crosscheck import parse_reply_row, evaluate_seq13
from execution.models import ExecutionStatus


SEQ = "1234567890123"


def make_row(event_type="D", qty="1"):
    # 21st field (zero-based 20) is Qty per documented OnNewData format.
    p = [""] * 49
    p[0] = SEQ
    p[1] = "TC"
    p[2] = event_type
    p[3] = "N"
    p[6] = "S"
    p[8] = "2891"
    p[11] = "40.0"
    p[20] = qty
    p[47] = SEQ
    return ",".join(p)


class CrossCheckStep8Tests(unittest.TestCase):
    def test_fill_quantity_parsed(self):
        row = parse_reply_row(make_row("D", "1"))
        self.assertEqual("2891", row.symbol)
        self.assertEqual("S", row.buy_sell)
        self.assertEqual(1, row.quantity)

    def test_crosscheck_sums_fill_qty(self):
        r = evaluate_seq13(
            SEQ,
            replay_rows=[make_row("D", "1"), make_row("D", "2")],
            replay_complete=True,
        )
        self.assertEqual(ExecutionStatus.PARTIALLY_FILLED, r.status)
        self.assertEqual(3, r.filled_quantity)


if __name__ == "__main__":
    unittest.main()
