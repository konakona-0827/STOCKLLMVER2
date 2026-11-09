from pathlib import Path
import tempfile
import unittest
from operations.audit_api_usage import audit,estimate_standard_sol
from operations.expanded_opportunity_replay import save


class UsageAuditTests(unittest.TestCase):
    def test_reused_responses_are_billed_once_and_token_subsets_not_added_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            row=dict(response_id='resp_real',input_tokens=100,output_tokens=20,cached_input_tokens=60,
                cache_write_tokens=30,reasoning_tokens=10,role='trading_and_support')
            for folder in ('20261109/original','20261109/copied'):
                save(root/folder/'api_usage_requests.json',[row]);save(root/folder/'agent_response.json',dict(api=dict(model='gpt-6-sol')))
            result=audit(root)
            self.assertEqual(result['totals']['successful_responses'],1)
            self.assertEqual(result['totals']['total_tokens'],120)
            self.assertEqual(result['duplicate_copies_excluded'],1)
            self.assertEqual(result['estimated_usd'],'0.000307')

    def test_failed_usage_remains_unknown_and_not_reported_as_actual_zero_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);save(root/'20261109/g/api_usage_requests.accounting.json',dict(status='REQUEST_FAILED_OR_CANCELLED',complete=False))
            r=audit(root);self.assertEqual(r['failed_or_cancelled_files'],1)
            self.assertIsNone(r['actual_billed_usd'])

    def test_invalid_cache_partition_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'INPUT_CACHE_PARTITION_INVALID'):
            estimate_standard_sol([dict(input_tokens=100,cached_input_tokens=80,cache_write_tokens=30,output_tokens=10)])

    def test_real_recorded_date_overrides_frozen_report_folder_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            row=dict(response_id='resp_new',input_tokens=10,output_tokens=2,
                model='gpt-6-sol',observed_at_utc='2026-11-09T16:00:00+00:00')
            save(root/'20261109/g/api_usage_requests.json',[row])
            self.assertEqual(audit(root,'2026-11-09')['unique_response_ids'],0)
            self.assertEqual(audit(root,'2026-11-10')['unique_response_ids'],1)
