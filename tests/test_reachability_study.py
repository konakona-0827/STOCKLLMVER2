"""The unseen study must not turn future daily extrema into entry evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from operations.validate_target_reachability import candidate,calendar_context,path_outcome,baseline_engine
from test_opportunity_search import candidate as live_candidate


class ReachabilityStudyTests(unittest.TestCase):
    def test_features_ignore_decision_day_extrema_volume_and_later_outcomes(self):
        bars=live_candidate()['daily_bars']
        dates=[b['date'] for b in bars]+['2026-11-03','2026-11-04']
        with calendar_context(dates):
            before=candidate('TEST','test',1,'2026-11-03',125,bars,dates)
            changed=deepcopy(bars)+[dict(date='2026-11-03',open=125,high=9999,low=1,close=500,
                                       volume=999999,complete=True,source='TWSE_DAILY')]
            after=candidate('TEST','test',1,'2026-11-03',125,changed,dates)
        self.assertEqual(before,after)
        self.assertEqual(before['quote_status'],'HISTORICAL_DAILY_PROXY')
        self.assertFalse(before['execution_permitted'])
        self.assertEqual(before['high'],before['low'])
        self.assertEqual(before['volume'],0)

    def test_price_paths_preserve_open_gaps_ambiguous_order_missing_sessions_and_horizon(self):
        dates=['2026-01-05','2026-01-06','2026-01-07']
        setup=dict(target=110,invalidation=95,holding_days=2)
        bars=[dict(date=dates[1],open=100,high=112,low=94),dict(date=dates[2],open=100,high=101,low=99)]
        self.assertEqual(path_outcome(dates[0],setup,bars,dates)['status'],'AMBIGUOUS')
        bars[0]['open']=94
        self.assertEqual(path_outcome(dates[0],setup,bars,dates)['status'],'FAILURE')
        bars[0].update(open=111)
        self.assertEqual(path_outcome(dates[0],setup,bars,dates)['status'],'SUCCESS')
        self.assertEqual(path_outcome(dates[0],setup,bars[1:],dates)['status'],'UNAVAILABLE')
        bars[0].update(open=100,high=101,low=99)
        self.assertEqual(path_outcome(dates[0],setup,bars,dates)['reason'],'TARGET_NOT_REACHED_WITHIN_HORIZON')

    def test_baseline_adapter_accepts_only_explicit_nonexecuting_proxy(self):
        module,metadata=baseline_engine()
        self.assertIn('HISTORICAL_DAILY_PROXY',metadata['research_guard'])
        self.assertNotEqual(metadata['original_guard'],metadata['research_guard'])
        c=live_candidate()
        from execution.auto_advice_executor import LiveExecutionConfig
        result=module.refresh(c,dict(available_cash_cents=1000000,capital_limit_twd=10000),
                              LiveExecutionConfig(minimum_commission_twd=1))
        self.assertEqual(result['opportunity_status']['blockers'],['LIVE_NON_TRIAL_QUOTE_REQUIRED'])

    def test_explicit_collection_failure_is_missing_data_not_a_zero_price(self):
        from operations.replay_archived_plans import load_daily,parse_twse
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'twse_6944_202501.failure.json').write_text(json.dumps(dict(error='NOT_LISTED')),encoding='utf-8')
            self.assertEqual(load_daily(directory,'6944'),[])
        record=dict(payload=dict(stat='OK',fields=['日期','成交股數','成交金額','開盤價','最高價','最低價','收盤價'],
            data=[['114/08/13','0','0','--','--','--','--']]))
        self.assertEqual(parse_twse(record),[])
        record['payload']['data'][0][1:3]=['401','16,039']
        self.assertEqual(parse_twse(record),[])


if __name__=='__main__':unittest.main()
