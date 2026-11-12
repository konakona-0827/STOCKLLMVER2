"""Full mocked broker/agent handoffs, venue routing and actual SELL dispatch guards."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from test_profit_exit_position_manager import AT,RULES,holding,management
from execution.account_guard import InventoryRow
from execution.account_snapshot import ExecutionAccountSnapshot
from execution.auto_advice_executor import execute_advice_file
from execution.execution_store import ExecutionStore
from execution.models import ExecutionStatus
from execution.capital_executor import SendResult
from position_manager import review_position
from tpex_history import parse_month
from market_venue import oddlot_market,record_tpex,recovery_watchlist
from profit_exit import refresh


class Clock(datetime):
    @classmethod
    def now(cls,tz=None):
        return AT.astimezone(tz) if tz else AT.astimezone().replace(tzinfo=None)


class VenueAndSellTests(unittest.TestCase):
    def test_tpex_completed_history_keeps_the_upstream_target_evidence_checks(self):
        from target_reachability import profile
        from test_opportunity_search import candidate
        c=candidate();bars=c['daily_bars']
        listed=profile(bars,as_of='2026-11-03',holding_days=10,trend=True)
        for bar in bars:bar['source']='TPEX_DAILY'
        tpex=profile(bars,as_of='2026-11-03',holding_days=10,trend=True)
        self.assertEqual(tpex['status'],'AVAILABLE')
        self.assertEqual(tpex['available_windows'],listed['available_windows'])

    def test_tpex_month_converts_lots_and_thousands_of_twd(self):
        payload=dict(stat='ok',code='1815',tables=[dict(title='個股日成交資訊',
            fields=['日 期','成交張數','成交仟元','開盤','最高','最低','收盤'],
            data=[['115/11/10','1,234','88,710','134.5','140.5','126','128']])])
        result=parse_month(payload,'1815',date(2026,11,1),AT.isoformat(),'official-test-url')
        self.assertEqual(result[0]['volume'],1234000)
        self.assertEqual(result[0]['turnover_twd'],88710000)
        self.assertEqual(result[0]['source'],'TPEX_DAILY')
        with self.assertRaises(ValueError):parse_month(payload,'2330',date(2026,11,1),AT.isoformat(),'url')
        with self.assertRaises(ValueError):parse_month(payload,'1815',date(2026,10,1),AT.isoformat(),'url')

    def test_verified_tpex_routes_to_six_and_does_not_change_listed_stocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            record_tpex(root,[dict(symbol='1815',venue='TPEx')],'2026-11-10')
            saved=json.loads((root/'data/analysis/market_discovery/symbol_venues.json').read_text('utf-8'))
            with patch('market_venue.registry',return_value=saved):
                self.assertEqual(oddlot_market('1815'),6)
                self.assertEqual(oddlot_market('2330'),5)

    def test_tpex_candidates_are_research_additions_with_explicit_live_checks(self):
        result=dict(symbols=['2330'],watchlist=[],weekly_recovery=dict(candidates=[dict(
            symbol='1815',reclaimed_high=129,required_next_checks=['LIVE_TPEX_ODD_LOT'])]))
        recovery_watchlist(result,'2026-11-11')
        self.assertEqual(result['symbols'],['2330','1815'])
        self.assertEqual(result['watchlist'][0]['venue'],'TPEx')
        self.assertIn('LIVE_TPEX_ODD_LOT',result['watchlist'][0]['required_next_checks'])
        recovery_watchlist(result,'2026-11-11')
        self.assertEqual(len(result['watchlist']),1)

    def test_sell_and_resting_sell_reach_existing_executor_with_zero_buy_budget(self):
        from execution.planned_exit import contract
        for planned in (False,True):
            with self.subTest(planned=planned),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);store=ExecutionStore(root/'isolated.sqlite3')
                store.bootstrap_ai_qty('1815',30)
                cfg=root/'execution.json';cfg.write_text(json.dumps(dict(enabled=True,planned_limits_enabled=True,
                    minimum_commission_twd=1,observe_seconds=0)),encoding='utf-8')
                c=holding();refresh(c,RULES,at=AT)
                d=dict(decision='SELL',suggested_price=140,suggested_qty=30)
                rec=dict(symbol='1815',decision='SELL',confidence=.8,suggested_qty=30,
                    action_ratio=1,ai_managed_qty=30,quote_status='LIVE',quote_timestamp=AT.isoformat(),
                    suggested_price=140 if planned else 137.5,asset_type='STOCK',
                    pricing_policy='PLANNED_LIMIT' if planned else 'AT_QUOTE',
                    existing_cost_basis=c['ai_cost_basis'],existing_buy_plans=c['buy_plans'],
                    exit_plan=contract(c,d,AT) if planned else None)
                payload=dict(interface_version='1.0',run_id='sell-zero-budget',run_status='READY',
                    llm_validation='VALID',pretrade_review_status='VALID',real_order_sent=False,
                    trading_mode='agent',market=dict(quotes=[dict(symbol='1815',quote_status='LIVE',
                        price=137.5,bid=137.5,ask=138,exchange_time=AT.isoformat())]),recommendations=[rec],
                    execution_budget=dict(available_cash_twd=0,budget_status='BLOCKED',strategy_cap_twd=10000))
                advice=root/'advice.json';advice.write_text(json.dumps(payload),encoding='utf-8')
                inventory=InventoryRow('1815','T',45,0,0,0,0,45,45,'')
                snapshot=ExecutionAccountSnapshot(AT.isoformat(),inventory=(inventory,))
                session=SimpleNamespace(connected=True,ensure_ready=Mock(return_value=SimpleNamespace(
                    ready=True,reply_state=1,reply_connected=True,action='REUSE')))
                def send(**kwargs):
                    self.assertEqual(kwargs['price'],Decimal(140 if planned else '137.5'))
                    kwargs['on_submitted'](SendResult(0,'accepted','1234567890123','mock'))
                    return SimpleNamespace(status=ExecutionStatus.ACKNOWLEDGED,quantity=30,price=kwargs['price'],
                        send_return_code=0,seq13='1234567890123',filled_quantity=0,broker_order_sent=True,
                        send_attempted=True,crosscheck_performed=True,used_fallback=False,changes_position=False,
                        crosscheck=SimpleNamespace(status=ExecutionStatus.ACKNOWLEDGED,accepted_rows=['accepted'],
                            fill_rows=[],cancel_rows=[],used_fallback=False))
                with patch('execution.auto_advice_executor.datetime',Clock), \
                        patch('execution.auto_advice_executor.CapitalOddLotExecutor.send_limit_order',side_effect=send) as sender:
                    report=execute_advice_file(advice,project_root=root,config_path=cfg,session=session,
                        account_snapshot=snapshot,execution_db_path=store.path)
                self.assertEqual(report['results'][0]['status'],'ACKNOWLEDGED',report)
                sender.assert_called_once()
                self.assertEqual(store.get_ai_qty('1815'),30)  # ACK is not a fill.
                self.assertEqual(store.pending_reconciliation_symbols(),{'1815'})


class PositionReviewPipelineTests(unittest.TestCase):
    def exercise(self,action='HOLD',*,changed=False,stop_after_inference=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        service=SimpleNamespace(directory=root/'analysis',db_path=root/'analysis/history.sqlite3',
            stop=SimpleNamespace(is_set=lambda:False),_execute_after_advice_written=Mock(return_value=dict(status='NO_TRADE_ACTION')))
        c=holding();c['max_buy_qty']=10
        quote={k:c[k] for k in ('symbol','quote_timestamp','quote_status','is_trial','last_price','bid','ask','open','high','low')}
        runtime=SimpleNamespace(fetch_quotes=Mock(return_value=({},{})),
                                cancel_exit=Mock(return_value=dict(status='CANCEL_REQUESTED_WAIT_FOR_BROKER')))
        store=SimpleNamespace(daily_reserved_buy_twd=lambda:Decimal(0),pending_reconciliation_symbols=lambda:set())
        result_plan=management(action,30 if action in ('SELL','TAKE_PROFIT_ORDER') else 0)
        stop=Mock(return_value=False)
        def provider(request):
            if request['text']['format']['name']=='position_management':
                if stop_after_inference:stop.return_value=True
                return json.dumps(result_plan),dict(runtime='FAKE_NO_MODEL')
            return json.dumps(dict(reviews=[dict(symbol='1815',approved=True,reason='有效退出')])) , {}
        with ExitStack() as stack:
            replacements={
                'position_manager.now':Mock(return_value=AT),
                'profit_exit.now':Mock(return_value=AT),
                'advice_interface.now':Mock(return_value=AT),
                'execution.auto_advice_executor.LiveExecutionConfig.load':Mock(return_value=RULES),
                'execution.health_monitor.sync_broker_state':Mock(return_value=(dict(ai_position_settings={}),{})),
                'analysis_service.live_ai_positions':Mock(side_effect=[[dict(symbol='1815',qty=30)],
                                                                      [dict(symbol='1815',qty=20 if changed else 30)]]),
                'execution.execution_store.ExecutionStore':Mock(return_value=store),
                'paper_portfolio.account':Mock(return_value=dict(capital_limit_cents=1000000)),
                'analysis_service.live_buy_budget':Mock(return_value=dict(available=Decimal(5000),status='READY')),
                'execution.broker_runtime.get_broker_runtime':Mock(return_value=runtime),
                'capital_multi_quote.multi_snapshot':Mock(return_value=dict(quotes=[quote])),
                'universe.load_universe':Mock(return_value=dict(etf_symbols=[])),
                'universe.load_rules':Mock(return_value={}),
                'position_store.load':Mock(return_value=[]),
                'analysis_service.build_union':Mock(return_value=[c]),
                'daily_history.enrich_candidates':Mock(),
                'analysis_service.apply_live_position_context':Mock(),
                'analysis_service.apply_buy_limits':Mock(),
                'opportunity_engine.refresh':Mock(side_effect=lambda c,a,r:c),
                'strategy_catalog.live_selection':Mock(return_value=dict(name='strategy_opportunity_v2',
                    version='test',sha256='test',prompt='test strategy')),
                'position_analyzer.build_request':Mock(side_effect=lambda c,a,p:dict(model='fake',instructions='test',
                    input=json.dumps(dict(analysis_set=c,paper_simulation_rules=dict(minimum_commission_twd=1))))),
            }
            for target,replacement in replacements.items():stack.enter_context(patch(target,replacement))
            if action=='CANCEL_EXIT_AND_REVIEW':
                store.pending_reconciliation_symbols=lambda:{'1815'}
                stack.enter_context(patch('execution.exit_cancel.pending_exit',return_value=dict(
                    decision_id='D',cancel_requested=False)))
            if changed or stop_after_inference:
                with self.assertRaisesRegex(ValueError,'POSITION_CHANGED|POSITION_MANAGER_STOPPED'):
                    review_position(service,'1815',{},[],stop,provider=provider)
                service._execute_after_advice_written.assert_not_called()
                return
            if action=='RESEARCH_ADD':
                decision=dict(market_view='仍待研究',decisions=[dict(symbol='1815',decision='HOLD')])
                analyze=stack.enter_context(patch('agent_decision._analyze_batch',return_value=(
                    decision,dict(validation='VALID',pretrade_review=dict(status='NOT_NEEDED')))))
                stack.enter_context(patch('advice_interface.write_advice_interface'))
            result=review_position(service,'1815',{},[],stop,provider=provider)
            self.assertTrue(replacements['daily_history.enrich_candidates'].call_args.kwargs['verify_sessions'])
            self.assertGreater(replacements['daily_history.enrich_candidates'].call_args.kwargs['max_bars'],22)
            self.assertEqual(result['management_plan']['action'],action)
            self.assertEqual(runtime.fetch_quotes.call_count,2)
            self.assertEqual(len(result['analysis_set']),1)
            self.assertEqual(result['analysis_set'][0]['symbol'],'1815')
            if action=='RESEARCH_ADD':analyze.assert_called_once()
            if action in ('SELL','TAKE_PROFIT_ORDER'):
                service._execute_after_advice_written.assert_called_once()
            else:
                service._execute_after_advice_written.assert_not_called()
            if action=='CANCEL_EXIT_AND_REVIEW':
                runtime.cancel_exit.assert_called_once()
                self.assertEqual(result['management_plan']['next_review_seconds'],60)
            return result

    def test_hold_and_sell_each_refresh_only_the_managed_symbol(self):
        for action in ('HOLD','SELL','TAKE_PROFIT_ORDER'):
            with self.subTest(action=action):self.exercise(action)

    def test_ownership_change_during_inference_prevents_order_dispatch(self):
        self.exercise(changed=True)

    def test_user_stop_during_inference_prevents_order_dispatch(self):
        self.exercise(stop_after_inference=True)

    def test_add_request_uses_existing_agent_research_pipeline(self):
        self.exercise('RESEARCH_ADD')

    def test_pending_exit_goes_to_cancel_queue_without_a_second_sell(self):
        self.exercise('CANCEL_EXIT_AND_REVIEW')


if __name__=='__main__':unittest.main()
