from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from execution.account_guard import BuyingPower
from execution.account_snapshot import ExecutionAccountSnapshot
from execution.auto_advice_executor import execute_advice_file
from execution.execution_store import ExecutionStore


class LiveBuyingPowerRecheckTests(unittest.TestCase):
    def test_buy_is_blocked_when_fresh_broker_balance_is_too_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ExecutionStore(root / "data" / "execution" / "live_execution.sqlite3")
            config_path = root / "execution_live.json"
            config_path.write_text(
                json.dumps({
                    "enabled": True,
                    "max_quote_age_seconds": 120,
                    "min_confidence_pct": 0,
                    "max_order_twd": None,
                    "max_buy_qty": 999,
                    "buy_cash_buffer_rate": 0.01,
                    "observe_seconds": 0,
                }),
                encoding="utf-8",
            )
            now = datetime.now(timezone.utc).isoformat()
            advice_path = root / "advice.json"
            advice_path.write_text(json.dumps({
                "interface_version": "1.0",
                "generated_at": now,
                "run_id": "RUN-BUY-RECHECK",
                "run_status": "READY",
                "llm_validation": "VALID",
                "real_order_sent": False,
                "market": {"quotes": [{
                    "symbol": "0050",
                    "quote_status": "LIVE",
                    "price": 100,
                    "bid": 99.9,
                    "ask": 100,
                    "exchange_time": now,
                    "age_seconds": 0,
                }]},
                "recommendations": [{
                    "symbol": "0050",
                    "decision": "BUY",
                    "confidence": 80,
                    "suggested_qty": 1,
                    "action_ratio": 0,
                    "ai_managed_qty": 0,
                    "quote_status": "LIVE",
                    "quote_timestamp": now,
                    "quote_age_seconds": 0,
                }],
            }), encoding="utf-8")

            initial_power = BuyingPower(
                True, True, Decimal("1000"), Decimal("1000"),
                Decimal("1000"), "initial",
            )
            snapshot = ExecutionAccountSnapshot(
                captured_at=now,
                buying_power=initial_power,
            )
            session = SimpleNamespace(
                connected=True,
                ensure_ready=Mock(return_value=SimpleNamespace(
                    ready=True,
                    reply_state=1,
                    reply_connected=True,
                    action="REUSE",
                )),
            )
            fresh_power = BuyingPower(
                True, True, Decimal("100"), Decimal("100"),
                Decimal("100"), "fresh",
            )

            with patch(
                "execution.auto_advice_executor.query_buying_power",
                return_value=fresh_power,
            ) as query, patch(
                "execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order"
            ) as send:
                report = execute_advice_file(
                    advice_path,
                    project_root=root,
                    config_path=config_path,
                    session=session,
                    account_snapshot=snapshot,
                )

            query.assert_called_once_with(session)
            send.assert_not_called()
            item = report["results"][0]
            self.assertEqual("SKIPPED_CHECK_FAILED", item["status"])
            self.assertEqual("100", item["fresh_broker_buying_power_twd"])


if __name__ == "__main__":
    unittest.main()
