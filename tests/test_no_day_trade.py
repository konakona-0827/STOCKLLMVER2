from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from execution.account_guard import parse_real_balance_row
from execution.account_snapshot import ExecutionAccountSnapshot
from execution.auto_advice_executor import execute_advice_file
from execution.execution_store import ExecutionStore


class NoDayTradeTests(unittest.TestCase):
    def test_auto_sell_does_not_use_todays_ai_buy_against_older_user_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExecutionStore(root / "data" / "execution" / "live_execution.sqlite3")
            store.bootstrap_ai_qty("2891", 5)
            config_path = root / "execution_live.json"
            config_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
            now = datetime.now(timezone.utc).isoformat()
            advice_path = root / "advice.json"
            advice_path.write_text(json.dumps({
                "interface_version": "1.0", "generated_at": now,
                "run_id": "RUN-NO-DAY-TRADE", "run_status": "READY",
                "llm_validation": "VALID", "real_order_sent": False,
                "market": {"quotes": [{
                    "symbol": "2891", "quote_status": "LIVE", "price": 40,
                    "bid": 39.95, "ask": 40, "exchange_time": now,
                    "age_seconds": 0,
                }]},
                "recommendations": [{
                    "symbol": "2891", "decision": "SELL", "confidence": 80,
                    "suggested_qty": 1, "action_ratio": 0,
                    "ai_managed_qty": 5, "quote_status": "LIVE",
                    "quote_timestamp": now, "quote_age_seconds": 0,
                }],
            }), encoding="utf-8")
            inventory = parse_real_balance_row(
                "2891,T,0,0,0,0,5,5,0,5,0,10,0,0,10,X,0,LOGIN,ACC"
            )
            snapshot = ExecutionAccountSnapshot(
                captured_at=now, inventory=(inventory,),
            )
            session = SimpleNamespace(
                connected=True,
                ensure_ready=Mock(return_value=SimpleNamespace(
                    ready=True, reply_state=1, reply_connected=True,
                    action="REUSE",
                )),
            )
            with patch(
                "execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order"
            ) as send:
                report = execute_advice_file(
                    advice_path, project_root=root, config_path=config_path,
                    session=session, account_snapshot=snapshot,
                )
            send.assert_not_called()
            item = report["results"][0]
            self.assertEqual("SKIPPED_CHECK_FAILED", item["status"])
            self.assertEqual(5, item["effective_broker_sellable_qty"])
            self.assertEqual(0, item["prior_day_ai_sellable_qty"])


if __name__ == "__main__":
    unittest.main()
