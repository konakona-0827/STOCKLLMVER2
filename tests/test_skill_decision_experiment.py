from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from operations.expanded_opportunity_replay import run,save,read,archive_incomplete
from trading_agent import api_quota_exhausted


class SkillDecisionExperimentTests(unittest.TestCase):
    def test_quota_stop_prevents_credentials_or_model_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);save(root/'api_quota_stop.json',dict(blocked=True))
            actual_read=read
            def allow_policy(path):
                return dict(allow_model_requests=True) if Path(path).name=='replay_api.json' else actual_read(path)
            with patch('operations.expanded_opportunity_replay.load_api_env') as credentials, \
                    patch('operations.expanded_opportunity_replay.read',side_effect=allow_policy):
                self.assertTrue(run(root/'missing-benchmark.json',root/'out',root/'missing.env')['blocked'])
            credentials.assert_not_called()

    def test_user_pause_stops_before_credentials_and_cannot_be_released_by_topup(self):
        from operations.skill_decision_experiment import resume_api_quota
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);marker=dict(blocked=True,kind='USER_PAUSED_API');save(root/'api_quota_stop.json',marker)
            with patch('operations.expanded_opportunity_replay.load_api_env') as credentials:
                status=run(root/'missing.json',root/'out',root/'missing.env')
            self.assertEqual(status.get('reason'),'USER_PAUSED_API');credentials.assert_not_called()
            with self.assertRaisesRegex(ValueError,'USER_API_PAUSE_CANNOT_BE_RELEASED'):
                resume_api_quota(root)
            self.assertEqual(read(root/'api_quota_stop.json'),marker)

    def test_explicit_retry_archives_all_old_attempt_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dest=root/'group';save(dest/'result.json',dict(error='old'))
            (dest/'old_sdk_trace.json').write_text('old',encoding='utf-8')
            archived=archive_incomplete(dest,root)
            self.assertFalse(dest.exists());self.assertTrue(archived.is_relative_to(root.resolve()))
            self.assertEqual(read(archived/'result.json'),dict(error='old'))
            self.assertEqual((archived/'old_sdk_trace.json').read_text(),'old')
            with self.assertRaisesRegex(ValueError,'UNSAFE_ATTEMPT_ARCHIVE'):
                archive_incomplete(root.parent/'outside',root)

    def test_parallel_quota_stop_writes_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'stop.json'
            with ThreadPoolExecutor(max_workers=8) as workers:
                list(workers.map(lambda i:save(path,dict(blocked=True,worker=i)),range(30)))
            self.assertTrue(read(path)['blocked'])
            self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_provider_quota_is_distinct_from_temporary_rate_limit(self):
        self.assertTrue(api_quota_exhausted('code=credit_balance_exhausted'))
        self.assertTrue(api_quota_exhausted('type=insufficient_quota'))
        self.assertFalse(api_quota_exhausted('429 rate_limit_exceeded'))
        exc=RuntimeError('provider error');exc.body=dict(error=dict(code='credit_balance_exhausted'))
        self.assertTrue(api_quota_exhausted(exc))

    def test_quota_failure_returns_wait_with_explicit_failure_kind(self):
        from trading_agent import TradingTools,analyze
        from execution.auto_advice_executor import LiveExecutionConfig
        from config import now
        with tempfile.TemporaryDirectory() as tmp:
            c=dict(symbol='X',quote_status='LIVE',quote_timestamp=now().isoformat(),last_price=100,
                ask=100,bid=99.9,position=dict(ai_managed_qty=0),paper_position=dict(qty=0))
            account=dict(available_cash_cents=1000000,capital_limit_twd=10000,positions=[])
            ctx=TradingTools([c],LiveExecutionConfig(),'quota',Path(tmp)/'tools.json',
                lambda *args:c,lambda:account,lambda:[],entry_skill_enabled=False)
            async def unavailable(context,*args):
                context.get_portfolio();context.get_trade_history()
                raise RuntimeError('insufficient_quota credit_balance_exhausted')
            decision,response=analyze(ctx,'test',runner=unavailable)
            self.assertEqual(response['validation'],'ERROR')
            self.assertEqual(response['failure_kind'],'API_QUOTA_EXHAUSTED')
            self.assertEqual(decision['decisions'][0]['decision'],'WAIT')

    def test_infrastructure_failure_cannot_be_presented_as_skill_accuracy(self):
        from operations.skill_decision_experiment import VARIANTS,compare
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);setup=dict(setup_id='p',entry=100,target=110,invalidation=95,max_qty=10,
                strategy_id='intraday_recovery',holding_days=2)
            event=dict(event_id='day|X',symbol='X',source_run='g',split='validation')
            save(root/'benchmark.json',dict(benchmark_sha256='fixed',events=[event],groups=[dict(group_id='g')]))
            save(root/'labels.json',dict(benchmark_sha256='fixed',labels={'day|X':dict(reference='SUCCESS',routes={'p':dict(status='SUCCESS')})}))
            save(root/'protocol.json',dict(version='test'))
            for variant in VARIANTS:
                save(root/variant/'g/result.json',dict(benchmark_sha256='fixed',group=dict(group_id='g',events=['day|X']),
                    selected_symbols=['X'],all_candidates=[dict(symbol='X',trade_setups=[setup])],
                    decision=dict(decisions=[dict(symbol='X',decision='WAIT')]),
                    response=dict(validation='ERROR',api=dict(error='credit_balance_exhausted'))))
                save(root/variant/'g/agent_model_input.json',dict(same='input'))
            with patch('builtins.print'):compare(root)
            result=read(root/'comparison.json')
            self.assertTrue(result['records_complete']);self.assertFalse(result['decision_comparison_valid'])
            for variant in VARIANTS:
                quality=result['variants'][variant]['decision_quality']
                self.assertIsNone(quality['hit_rate']);self.assertIsNone(quality['same_day_miss_rate'])
                self.assertEqual(quality['operational_misses'],1)
