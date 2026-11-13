from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

from execution.capital_executor import (
    CapitalExecutionError,
    CapitalOddLotExecutor,
    extract_seq13,
    parse_send_result,
    prepare_limit_order,
    validate_real_order,
)
from execution.models import Side
from execution.models import ExecutionStatus
from execution.capital_crosscheck import CrossCheckResult


class CapitalExecutorOfflineTests(unittest.TestCase):
    def test_prepare_buy_and_sell_uses_oddlot_fields_without_a_session_or_sender(self):
        sk=SimpleNamespace(STOCKORDER=lambda:SimpleNamespace(sPrime=-1,nTradeType=-1,nSpecialTradeType=-1))
        for side,code in (('BUY',0),('SELL',1)):
            with self.subTest(side=side):
                order=prepare_limit_order(sk,'NON_SUBMITTABLE',symbol='0050',side=side,quantity=1,price='100.50')
                self.assertEqual(vars(order),dict(bstrFullAccount='NON_SUBMITTABLE',bstrStockNo='0050',
                    sPeriod=4,sFlag=0,sBuySell=code,bstrPrice='100.50',nQty=1,sPrime=0,nTradeType=0,nSpecialTradeType=2))

    def test_manual_order_report_distinguishes_rejection_unknown_ack_and_fill(self):
        from real_order_test import print_result
        for sent,status,filled,exit_code,label in (
            (False,ExecutionStatus.FAILED,0,1,'NO'),
            (None,ExecutionStatus.UNCONFIRMED,0,1,'UNKNOWN'),
            (True,ExecutionStatus.UNCONFIRMED,0,1,'YES'),
            (True,ExecutionStatus.ACKNOWLEDGED,0,0,'YES'),
            (True,ExecutionStatus.FILLED,1,0,'YES'),
        ):
            with self.subTest(status=status,sent=sent):
                result=SimpleNamespace(send_return_code=1068 if sent is False else 0 if sent else None,
                    seq13=None,status=status,filled_quantity=filled,send_attempted=True,
                    broker_order_sent=sent,crosscheck_performed=sent is not False,
                    crosscheck=CrossCheckResult(status,None,None))
                output=StringIO()
                with redirect_stdout(output):self.assertEqual(print_result(result),exit_code)
                self.assertIn('REAL_ORDER_SENT='+label,output.getvalue())
                self.assertIn('FILLED_QUANTITY='+str(filled),output.getvalue())
                self.assertIn('BROKER_ACCEPTANCE_VERIFIED='+('YES' if exit_code==0 else 'NO'),output.getvalue())

    def test_extract_seq13(self):
        self.assertEqual("1234567890123", extract_seq13("ok 1234567890123 done"))

    def test_parse_tuple_send_result(self):
        result = parse_send_result(("1234567890123", 0))
        self.assertEqual(0, result.return_code)
        self.assertEqual("1234567890123", result.seq13)

    def test_parse_scalar_send_result(self):
        result = parse_send_result(0)
        self.assertEqual(0, result.return_code)
        self.assertIsNone(result.seq13)

    def test_validate_buy(self):
        sym, side, qty, px = validate_real_order("0050", "BUY", 1, "100.5")
        self.assertEqual("0050", sym)
        self.assertEqual(Side.BUY, side)
        self.assertEqual(1, qty)
        self.assertEqual(Decimal("100.5"), px)

    def test_validate_sell(self):
        _, side, _, _ = validate_real_order("0050", "SELL", 5, "100")
        self.assertEqual(Side.SELL, side)

    def test_reject_invalid_qty(self):
        with self.assertRaises(CapitalExecutionError):
            validate_real_order("0050", "BUY", 1000, "100")

    def test_nonzero_send_return_is_rejected_before_submitted_callback(self):
        session = SimpleNamespace(
            _sk=SimpleNamespace(STOCKORDER=lambda: SimpleNamespace()),
            _skO=SimpleNamespace(
                SendStockOddLotOrder=lambda *_args: ("price limit rejected", 1068)
            ),
            account="ACCOUNT",
            user="USER",
            ensure_ready=Mock(),
            tc_rows_copy=Mock(return_value=[]),
            crosscheck_seq13=Mock(),
            pump=Mock(),
        )
        callback = Mock()

        result = CapitalOddLotExecutor(session).send_limit_order(
            symbol="0050",
            side=Side.BUY,
            quantity=1,
            price=100,
            on_submitted=callback,
        )

        self.assertEqual("FAILED", result.status.value)
        self.assertFalse(result.broker_order_sent)
        self.assertTrue(result.send_attempted)
        self.assertFalse(result.crosscheck_performed)
        self.assertEqual(0, result.filled_quantity)
        callback.assert_not_called()
        session.crosscheck_seq13.assert_not_called()

    def test_ack_does_not_stop_observation_before_confirmed_fill(self):
        seq = "1234567890123"
        fields = [""] * 21
        fields[0], fields[1], fields[2], fields[3] = seq, "TC", "D", "N"
        fields[6], fields[8], fields[20] = "B", "0050", "1"
        fill = ",".join(fields)
        ack = CrossCheckResult(ExecutionStatus.ACKNOWLEDGED, seq, seq)
        filled = CrossCheckResult(ExecutionStatus.PARTIALLY_FILLED, seq, seq,
                                  fill_rows=(fill,))
        session = SimpleNamespace(
            _sk=SimpleNamespace(STOCKORDER=lambda: SimpleNamespace()),
            _skO=SimpleNamespace(SendStockOddLotOrder=lambda *_args: (seq, 0)),
            account="ACCOUNT", user="USER", ensure_ready=Mock(),
            tc_rows_copy=Mock(return_value=[]), pump=Mock(),
            crosscheck_seq13=Mock(side_effect=[ack, filled]),
        )
        result = CapitalOddLotExecutor(session).send_limit_order(
            symbol="0050", side=Side.BUY, quantity=1, price=100,
            observe_seconds=0.1,
        )
        self.assertEqual(2, session.crosscheck_seq13.call_count)
        self.assertEqual(ExecutionStatus.FILLED, result.status)
        self.assertEqual(1, result.filled_quantity)


if __name__ == "__main__":
    unittest.main()
