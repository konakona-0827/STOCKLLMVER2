"""Diagnostic output must not replace the AI executor's last result."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution.pipeline_hook import execute_after_advice_written


class HealthPipelineReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.normal=self.root/'data/execution/latest_live_execution.json'
        self.diagnostic=self.root/'data/execution/health_order_test/latest_execution.json'

    def test_normal_execution_keeps_the_original_default_report(self):
        report=dict(status='COMPLETE',results=[])
        with patch('execution.pipeline_hook.execute_advice_file',return_value=report) as execute:
            result=execute_after_advice_written(self.root/'advice.json',project_root=self.root)
        self.assertIs(result,report)
        self.assertEqual(json.loads(self.normal.read_text(encoding='utf-8')),report)
        self.assertFalse(self.diagnostic.exists())
        self.assertNotIn('health_order_test',execute.call_args.kwargs)

    def test_diagnostic_execution_preserves_the_original_ai_error_report_bytes(self):
        self.normal.parent.mkdir(parents=True)
        original=b'{"status":"ERROR","reason":"original FLOW failure"}\n'
        self.normal.write_bytes(original)
        report=dict(status='COMPLETE',results=[dict(status='ACKNOWLEDGED',ownership='MANUAL_DIAGNOSTIC')])
        with patch('execution.pipeline_hook.execute_advice_file',return_value=report) as execute:
            result=execute_after_advice_written(self.root/'diagnostic.json',project_root=self.root,health_order_test=True)
        self.assertIs(result,report);self.assertEqual(self.normal.read_bytes(),original)
        self.assertEqual(json.loads(self.diagnostic.read_text(encoding='utf-8')),report)
        self.assertIs(execute.call_args.kwargs['health_order_test'],True)

    def test_explicit_report_path_takes_precedence_in_both_modes(self):
        for health in (False,True):
            with self.subTest(health_order_test=health):
                target=self.root/'custom'/str(health)/'receipt.json'
                report=dict(status='CUSTOM_DESTINATION',health=health)
                with patch('execution.pipeline_hook.execute_advice_file',return_value=report):
                    execute_after_advice_written(self.root/'advice.json',project_root=self.root,
                        latest_report_path=target,health_order_test=health)
                self.assertEqual(json.loads(target.read_text(encoding='utf-8')),report)
        self.assertFalse(self.normal.exists());self.assertFalse(self.diagnostic.exists())


if __name__=='__main__':unittest.main()
