from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from execution.account_guard import BuyingPower, InventoryRow
from execution.account_snapshot import capture_account_snapshot


class AccountSnapshotTests(unittest.TestCase):
    @patch("execution.account_snapshot.query_inventory")
    @patch("execution.account_snapshot.query_buying_power")
    def test_capture_queries_each_requested_source_once(self, q_power, q_inv):
        q_power.return_value = BuyingPower(
            verified=True,
            one_account=True,
            balance_twd=Decimal("1000"),
            withdrawable_twd=Decimal("1000"),
            available_to_buy_twd=Decimal("900"),
            raw="x",
        )
        q_inv.return_value = []

        snap = capture_account_snapshot(
            object(),
            need_buying_power=True,
            need_inventory=True,
        )

        self.assertTrue(snap.has_buying_power)
        self.assertTrue(snap.has_inventory)
        q_power.assert_called_once()
        q_inv.assert_called_once()

    @patch("execution.account_snapshot.query_inventory")
    @patch("execution.account_snapshot.query_buying_power")
    def test_inventory_error_does_not_erase_buying_power(self, q_power, q_inv):
        q_power.return_value = BuyingPower(
            verified=True,
            one_account=True,
            balance_twd=Decimal("1000"),
            withdrawable_twd=Decimal("1000"),
            available_to_buy_twd=Decimal("900"),
            raw="x",
        )
        q_inv.side_effect = RuntimeError("rc=1019")

        snap = capture_account_snapshot(
            object(),
            need_buying_power=True,
            need_inventory=True,
        )

        self.assertTrue(snap.has_buying_power)
        self.assertFalse(snap.has_inventory)
        self.assertIn("1019", snap.inventory_error)


if __name__ == "__main__":
    unittest.main()
