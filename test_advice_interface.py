import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from advice_interface import build_advice_interface, write_advice_interface


class AdviceInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.generated_at = datetime(2026, 10, 7, 13, 0, tzinfo=timezone(timedelta(hours=8)))
        self.result = {
            'manifest': {
                'run_id': 'run-1', 'status': 'COMPLETE', 'ai_validation': 'VALID',
                'llm_model': 'gpt-test', 'real_order_sent': False,
                'request_started_at': '2026-10-07T12:59:00+08:00',
                'request_completed_at': '2026-10-07T12:59:10+08:00',
                'llm_request_started_at': '2026-10-07T12:59:10+08:00',
                'llm_response_received_at': '2026-10-07T12:59:15+08:00',
                'finished_at': '2026-10-07T12:59:16+08:00',
            },
            'batch': {
                'symbols_requested': 1, 'symbols_received': 1,
                'request_timestamp': '2026-10-07T12:59:00+08:00',
                'request_completed_at': '2026-10-07T12:59:10+08:00',
                'quotes': [{
                    'symbol': '2330', 'source': 'CAPITAL_SKCOM', 'quote_status': 'LIVE',
                    'last_price': 1000.0, 'bid': 999.5, 'ask': 1000.0,
                    'volume_shares': 100, 'volume_unit': 'shares', 'is_trial': False,
                    'quote_timestamp': '2026-10-07T12:59:30+08:00',
                    'received_at': '2026-10-07T12:59:32+08:00', 'warnings': [],
                }],
            },
            'analysis_set': [{'symbol': '2330', 'analysis_source': 'TOP10',
                              'position': {'ai_managed_qty': 3}}],
            'decision': {
                'market_view': '測試分析',
                'decisions': [{'symbol': '2330', 'decision': 'HOLD', 'confidence': 0.8,
                               'reference_price': 1000.0, 'suggested_price': None,
                               'suggested_qty': 0, 'action_ratio': 0, 'reason': '測試理由',
                               'warnings': [], 'data_quality': 'LIVE'}],
            },
        }

    def test_builds_versioned_recommendations_and_current_quote_age(self):
        payload = build_advice_interface(self.result, self.generated_at)
        self.assertEqual(payload['interface_version'], '1.0')
        self.assertEqual(payload['run_status'], 'READY')
        self.assertEqual(payload['recommendations'][0]['decision'], 'HOLD')
        self.assertEqual(payload['recommendations'][0]['quote_age_seconds'], 30)
        self.assertEqual(payload['market']['quotes'][0]['age_seconds'], 30)
        self.assertFalse(payload['real_order_sent'])

    def test_error_run_still_exports_wait_fallback(self):
        self.result['manifest']['status'] = 'ERROR'
        self.result['manifest']['ai_validation'] = 'ERROR'
        self.result['decision']['decisions'][0]['decision'] = 'WAIT'
        payload = build_advice_interface(self.result, self.generated_at)
        self.assertEqual(payload['run_status'], 'ERROR')
        self.assertEqual(payload['recommendations'][0]['decision'], 'WAIT')

    def test_writes_complete_json_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'latest_advice.json'
            write_advice_interface(self.result, path, self.generated_at)
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(payload['run_id'], 'run-1')
            self.assertFalse(path.with_suffix('.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
