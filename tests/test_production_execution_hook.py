from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from analysis_service import AnalysisService


class ProductionExecutionHookTests(unittest.TestCase):
    def test_auto_run_calls_hook_once_after_advice_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AnalysisService(root / "analysis")
            run_dir = root / "analysis" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            advice_path = run_dir / "advice_interface.json"
            advice_path.write_text("{}", encoding="utf-8")
            runtime = SimpleNamespace(execute=Mock(return_value={"status": "COMPLETE"}))
            with patch("execution.broker_runtime.get_broker_runtime",
                       return_value=runtime) as get_runtime:
                report = service._execute_after_advice_written(
                    "AUTO", {"manifest": {
                        "status": "COMPLETE", "ai_validation": "VALID",
                    }}, run_dir, advice_path,
                )

            self.assertEqual({"status": "COMPLETE"}, report)
            get_runtime.assert_called_once()
            runtime.execute.assert_called_once_with(advice_path)
            self.assertTrue((run_dir / "execution_report.json").exists())

    def test_manual_run_does_not_call_live_execution_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AnalysisService(root / "analysis")
            run_dir = root / "analysis" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            advice_path = run_dir / "advice_interface.json"

            with patch("execution.broker_runtime.get_broker_runtime") as execute:
                report = service._execute_after_advice_written(
                    "MANUAL", {}, run_dir, advice_path,
                )

            self.assertIsNone(report)
            execute.assert_not_called()

    def test_auto_run_without_valid_llm_result_does_not_call_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AnalysisService(root / "analysis")
            run_dir = root / "analysis" / "runs" / "run-1"
            run_dir.mkdir(parents=True)

            with patch("execution.broker_runtime.get_broker_runtime") as execute:
                report = service._execute_after_advice_written(
                    "AUTO", {"manifest": {
                        "status": "COMPLETE", "ai_validation": "NOT_CALLED",
                    }}, run_dir, run_dir / "advice_interface.json",
                )

            self.assertIsNone(report)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
