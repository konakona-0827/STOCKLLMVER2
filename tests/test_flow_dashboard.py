"""The full legacy UI is reused, with isolated state and no broker/model access."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import gc
from pathlib import Path
import tempfile
from types import SimpleNamespace
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock,patch

from opportunity_flow.common import TAIPEI,save,read,policy
from opportunity_flow.dashboard_service import FlowController,FlowViewService,project_research
from opportunity_flow.fees import correct_legacy_capital_fee,fee_status,SOURCE


def scene():
    at=datetime.now(TAIPEI).isoformat()
    route=dict(route_id='fixture-route',entry=100,stop=dict(price=98),target=dict(price=108),eligible=True,
        economics=dict(quantity=10),blockers=[])
    packet=dict(evidence=[{},{},dict(facts=[route])])
    return dict(summary=dict(as_of=at,symbols_observed=3,states=dict(RESEARCH_READY=1,ECONOMIC_BLOCKED=1,DATA_BLOCKED=1)),
        candidates=[dict(symbol='1111',state='RESEARCH_READY',packet=deepcopy(packet),rank_score=.15,reasons=[],selected_route_id='fixture-route',
            quote=dict(last_price=100,bid=99.9,ask=100,quote_timestamp=at),
            review=dict(packet=deepcopy(packet),researcher=dict(thesis='支撐確認',wait_conditions=[]),challenger=dict(thesis='風險已列明',wait_conditions=[]))),
            dict(symbol='2222',state='ECONOMIC_BLOCKED',packet=deepcopy(packet),rank_score=.08,reasons=['NET_PROFIT_BELOW_FLOOR']),
            dict(symbol='3333',state='DATA_BLOCKED',packet=None,reasons=['RECENT_HISTORY_GAP_OR_INVALID_BAR'])])


class FakeController:
    def __init__(self):
        self.stop_event=threading.Event();self.status='STOPPED';self.error='';self.running=False;self.coordinator=None
        self.start=Mock(return_value=True);self.stop=Mock();self.request_update=Mock(return_value=True)
        self.configure_position=Mock(return_value=dict(symbol='1111',ai_qty=10,user_qty=20,enabled=True))


class FakeView:
    def __init__(self,root):
        self.account_root=root;self.directory=root/'data/analysis';self.directory.mkdir(parents=True)
        self.db_path=self.directory/'market_history.sqlite3';self.controller=FakeController();self.position_manager_active=False
        self.flow=scene();self.execution=dict(reports=[],blocked=[]);self.usage=dict(calls=4,usage=dict(input_tokens=1000),unknown_calls=0)
        self.names={'1111':'測試公司甲','2222':'測試公司乙','3333':'缺漏公司'};self.read_errors=[]
        self.run=Mock(side_effect=AssertionError('Legacy scan must never execute'))
        self.health=dict(status='OK',checked_at=self.flow['summary']['as_of'],errors=[],broker_inventory_verified=True,inventory_checked=True,
            ai_positions={'1111':10},budget_status='READY',current_buy_budget_twd=8000,broker_one_account_balance_twd=30000,
            broker_balance_twd=30000,strategy_cap_twd=10000,execution_attempts=[],broker_order_reconciliation=[],
            inventory_comparison=[dict(symbol='1111',broker_cash_qty=30,broker_sellable_qty=30,ai_qty=10,configured_user_qty=20,status='MATCH')])
    @property
    def stop(self):return self.controller.stop_event
    def refresh(self,sync_broker=False):return project_research(self.flow,self.names),[],deepcopy(self.health)


class DashboardReuseTests(unittest.TestCase):
    def setUp(self):
        from opportunity_flow.dashboard import FlowDashboard
        self.real_refresh=FlowDashboard.refresh
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.service=FakeView(self.base)
        save(self.base/'config/execution_live.json',dict(enabled=True,min_buy_fee_reserve_twd=20,minimum_commission_twd=1,max_daily_buy_twd=30000))
        import paper_portfolio
        paper_portfolio.set_capital_limit(self.service.db_path,10000)
        self.root=tk.Tk();self.root.withdraw()
        self.patches=[patch('dashboard.ROOT',self.base),patch('dashboard.load_env'),
            patch('daily_participation.policy',return_value={'enabled':False}),
            patch('execution.health_monitor.sync_broker_state',side_effect=AssertionError('Real broker access forbidden')),
            patch.object(FlowDashboard,'refresh')]
        for p in self.patches:p.start()
        self.args=SimpleNamespace(state=str(self.base/'flow'),dataset=str(self.base/'dataset'),model=None,state_root=None,read_only=False)
        self.app=FlowDashboard(self.root,self.args,start=False,service=self.service)
        self.app.result,self.app.positions,self.app.last_health=self.service.refresh()
        self.app.paper_account_configured=True;self.app.render()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for token in self.root.tk.call('after','info'):self.root.after_cancel(token)
        self.root.destroy()
        self.app=self.root=self.service=None
        for p in reversed(self.patches):p.stop()
        gc.collect();self.temp.cleanup()

    def test_full_legacy_information_tabs_and_financial_controls_are_retained(self):
        tabs=[self.app.notebook.tab(t,'text') for t in self.app.notebook.tabs()]
        for name in ('持倉概況','券商持倉對帳','正式委託與成交紀錄','交割明細','系統資訊','FLOW 全母體與研究卡'):
            self.assertIn(name,tabs)
        self.assertEqual(len(self.app.position_tree.get_children()),1)
        row=self.app.position_tree.item(self.app.position_tree.get_children()[0],'values')
        self.assertEqual(row[2:5],('30','30','10'))
        self.assertIn('30,000',self.app.finance_text.get())
        self.assertTrue(self.app.paper_capital_button.winfo_exists());self.assertTrue(self.app.daily_limit_button.winfo_exists())

    def test_manual_start_stop_use_flow_controller_and_never_run_legacy_scans(self):
        self.app.run_button.invoke();self.service.controller.request_update.assert_called_once()
        self.app.action_buttons['開始自動掃描'].invoke();self.service.controller.start.assert_called_once()
        self.app.action_buttons['停止自動掃描'].invoke();self.service.controller.stop.assert_called_once()
        self.service.run.assert_not_called()
        self.assertEqual(self.app.mode_selector.winfo_manager(),'')

    def test_default_dashboard_starts_flow_without_a_button_or_trade_confirmation(self):
        from opportunity_flow.dashboard import FlowDashboard
        window=tk.Tk();window.withdraw()
        try:
            FlowDashboard(window,self.args,service=self.service)
            self.service.controller.start.assert_not_called()
            window.update()  # Run the default after(0) startup callback.
            self.service.controller.start.assert_called_once_with()
            self.service.run.assert_not_called()
        finally:
            for token in window.tk.call('after','info'):window.after_cancel(token)
            window.destroy()

    def test_local_poll_is_quiet_and_never_runs_a_broker_health_check(self):
        self.service.check_health=Mock(side_effect=AssertionError('Local poll must not contact broker'))
        self.service.refresh=Mock(wraps=self.service.refresh)
        with patch.object(self.app.refresh_progress,'start') as animate:
            self.real_refresh(self.app)
            event=self.app.flow_events.get(timeout=2);self.app.flow_events.put(event)
            self.app._drain_flow_events();self.app.update_work_status()
        self.service.refresh.assert_called_once_with(sync_broker=False)
        self.service.check_health.assert_not_called();animate.assert_not_called()
        self.assertEqual(self.app.refresh_button.cget('text'),'健康檢查')
        self.assertNotIn('本機資料已更新',self.app.refresh_text.get())
        self.assertIn('畫面檢查',self.app.refresh_text.get())

    def test_unchanged_revision_keeps_selection_and_skips_the_full_redraw(self):
        self.app.result['_flow_view_revision']='same-snapshot';self.app.render()
        selected=self.app.position_tree.get_children()[0];self.app.position_tree.selection_set(selected)
        with patch('dashboard.Dashboard.render') as redraw:self.app.render()
        redraw.assert_not_called();self.assertEqual(self.app.position_tree.selection(),(selected,))

    def test_execution_timing_view_shows_stages_and_survives_corrupt_timing(self):
        at=datetime.now(TAIPEI).isoformat()
        self.service.execution=dict(execution_timing=dict(started_at=at,elapsed_ms=14,metrics={},events=[
            dict(sequence=1,stage='sdk_send',phase='start',at=at),
            dict(sequence=2,stage='sdk_send',phase='end',at=at,span_start=1,duration_ms=12)]))
        self.app.render_timing();items=self.app.timing_tree.get_children()
        self.assertEqual(len(items),1);self.assertEqual(self.app.timing_tree.item(items[0],'values')[0],'SDK 送單呼叫')
        for broken in ('corrupt',dict(metrics=[],events=[]),dict(events=[dict(sequence=[],stage='sdk_send',phase='end',at=at)]),
                       dict(events=[dict(sequence=1,stage='sdk_send',phase='end',at=at,duration_ms='bad')])):
            with self.subTest(record=broken):
                self.service.execution=dict(execution_timing=broken)
                self.app.render_timing();self.assertIn('無法核實',self.app.timing_summary.get())

    def test_health_button_has_independent_progress_and_never_starts_trading(self):
        entered,release=threading.Event(),threading.Event()
        def inspect(progress):
            progress('正在查詢券商');entered.set();release.wait(2)
            return dict(status='PASS',checked_at=datetime.now(TAIPEI).isoformat(),
                checks=[dict(key='ledger',label='正式交易帳本',status='PASS',detail='完整性通過')])
        self.service.check_health=Mock(side_effect=inspect)
        try:
            self.app.refresh_button.invoke();self.assertTrue(entered.wait(1))
            self.app._drain_flow_events();self.app.update_work_status()
            self.assertTrue(self.app.health_pending);self.assertEqual(str(self.app.refresh_progress.cget('mode')),'indeterminate')
            self.assertIn('正在查詢券商',self.app.refresh_text.get());self.service.controller.start.assert_not_called()
            self.assertFalse(self.app.request_health_check())  # No concurrent duplicate query.
        finally:release.set()
        event=self.app.flow_events.get(timeout=2);self.app.flow_events.put(event)
        self.app._drain_flow_events();self.app.update_work_status()
        self.assertFalse(self.app.health_pending);self.assertEqual(self.app.health_report['status'],'PASS')
        self.assertEqual(self.app.refresh_button.cget('text'),'健康檢查')
        self.assertIn('通過',self.app.health_summary.get())

    def test_close_drains_a_completed_view_read_instead_of_waiting_forever(self):
        self.app.refresh_pending=True;self.app.flow_events.put(('local',self.service.refresh()))
        self.app.close()
        with patch.object(self.root,'destroy') as destroy,patch.object(self.root,'after'):
            self.app.tick()
        destroy.assert_called_once_with();self.assertFalse(self.app.refresh_pending)

    def test_view_notification_is_retained_until_an_inflight_read_finishes(self):
        self.service.controller.view_changed=threading.Event();self.service.controller.view_changed.set()
        self.app._last_local_poll=__import__('time').monotonic();self.app.refresh_pending=True
        with patch.object(self.root,'after'),patch.object(self.app,'refresh',return_value=True) as refresh:
            self.app.tick();refresh.assert_not_called();self.assertTrue(self.app._view_refresh_requested)
            self.app.refresh_pending=False;self.app.tick();refresh.assert_called_once_with()
        self.assertFalse(self.app._view_refresh_requested)

    def test_work_indicator_uses_real_activity_and_keeps_failed_work_visible(self):
        self.service.controller.observation=lambda:dict(quotes_received_at='2026-10-16T09:18:25+08:00',
            activity=dict(active=[dict(kind='BROKER_ACCOUNT',label='券商對帳',elapsed_seconds=7)],recent=[]))
        self.app.update_work_status()
        self.assertIn('券商對帳（7 秒）',self.app.refresh_text.get())
        self.assertIn('2026-10-16 09:18:25',self.app.refresh_text.get())
        self.assertEqual(str(self.app.refresh_progress.cget('mode')),'indeterminate')
        self.service.controller.observation=lambda:dict(activity=dict(active=[],recent=[dict(kind='BROKER_ACCOUNT',label='券商對帳',error='TimeoutError')]))
        self.app.update_work_status()
        self.assertIn('TimeoutError',self.app.refresh_text.get())
        self.assertEqual(str(self.app.refresh_label.cget('foreground')),'#B00020')

    def test_controller_starts_live_research_and_original_executor_by_default(self):
        coordinator=SimpleNamespace(refresh_account=Mock(),close=Mock())
        controller=FlowController(self.base,self.args)
        with patch('opportunity_flow.live.LiveCoordinator',return_value=coordinator) as live,patch(
            'opportunity_flow.__main__.main',return_value=0,
        ) as run:
            controller._run()
        self.assertFalse(live.call_args.kwargs['read_only'])
        coordinator.refresh_account.assert_called_once_with()
        argv=run.call_args.args[0]
        self.assertEqual(argv[0],'watch')
        for flag in ('--live','--llm','--public-quotes','--refresh-daily'):self.assertIn(flag,argv)
        self.assertNotIn('--read-only',argv)
        self.assertIs(run.call_args.kwargs['coordinator'],coordinator)
        coordinator.close.assert_called_once_with()

    def test_headless_live_entry_enables_automatic_research_and_execution(self):
        from opportunity_flow import __main__ as entry
        main=entry.main
        for readonly in (False,True):
            with self.subTest(readonly=readonly),patch.object(entry,'main',return_value=0) as watch:
                argv=['live','--headless','--dataset',self.args.dataset,'--state',self.args.state]
                if readonly:argv.append('--read-only')
                self.assertEqual(main(argv),0)
                forwarded=watch.call_args.args[0]
                self.assertEqual(forwarded[0],'watch')
                for flag in ('--live','--llm','--public-quotes','--refresh-daily'):self.assertIn(flag,forwarded)
                self.assertEqual('--read-only' in forwarded,readonly)

    def test_pipeline_filters_include_failed_data_and_research_is_not_displayed_as_buy(self):
        self.assertEqual(len(self.app.flow_tree.get_children()),2)
        self.app.flow_filter.set('全部');self.app.render_flow_rows()
        self.assertEqual(len(self.app.flow_tree.get_children()),3)
        self.app.flow_search.set('缺漏')
        self.assertEqual(self.app.flow_tree.get_children(),('3333',))
        first=self.app.top_tree.item(self.app.top_tree.get_children()[0],'values')
        self.assertEqual(first[6],'雙方研究通過');self.assertEqual(first[7],'--')
        self.assertEqual(self.app.result['decision']['decisions'],[])

    def test_holdings_editor_uses_existing_total_quantity_and_new_handoff_backend(self):
        self.app.configure_ai_holding('1111',15,Decimal('99'))
        self.service.controller.configure_position.assert_called_once_with('1111',15,Decimal('99'))
        self.app.position_settings()
        windows=[w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel)]
        self.assertEqual(len(windows),1)
        labels=[w.cget('text') for w in windows[0].winfo_children() if isinstance(w,(tk.Label,)) or w.winfo_class()=='TLabel']
        self.assertIn('AI 管理股數',labels);self.assertIn('轉入股數的每股成本',labels)
        self.assertTrue(any('使用者交接' in text for text in labels))

    def test_read_only_mode_cannot_change_ownership_or_account_limits(self):
        self.app.args.read_only=True
        with patch('opportunity_flow.dashboard.messagebox.showinfo') as info,\
             patch('dashboard.Dashboard.save_paper_capital') as capital,patch('dashboard.Dashboard.save_daily_buy_limit') as daily:
            self.app.position_settings();self.app.save_paper_capital();self.app.save_daily_buy_limit()
        info.assert_called_once();capital.assert_not_called();daily.assert_not_called()
        self.service.controller.configure_position.assert_not_called()

    def test_close_waits_for_inflight_ownership_configuration(self):
        self.app.scheduler.scan_lock.acquire();self.app.close()
        with patch.object(self.root,'destroy') as destroy,patch.object(self.root,'after'):
            self.app.tick();destroy.assert_not_called()
            self.app.scheduler.scan_lock.release();self.app.tick();destroy.assert_called_once()

    def test_flow_order_reason_survives_missing_worktree_handoff_files(self):
        run='tw-flow-'+'a'*28
        self.app.last_health['execution_attempts']=[dict(run_id=run,symbol='1111',detail=dict(analysis_reason='原假設失效，退出'))]
        label,reason=self.app.run_strategy_info(run,'1111')
        self.assertEqual(label,'TW Opportunity Flow');self.assertEqual(reason,'原假設失效，退出')

    def test_flow_clock_does_not_start_original_hourly_scheduler_or_position_supervisor(self):
        with patch.object(self.app.scheduler,'tick',return_value=False) as old_scheduler,patch.object(self.app,'launch_health') as old_health:
            self.app.tick()
        self.assertIsNone(self.app.scheduler.next_auto)
        self.assertFalse(self.app.position_manager.active);old_health.assert_not_called()
        self.assertIn('FLOW 已停止',self.app.status_text.get())
        self.assertIn('約30秒',self.app.system_status_text.get())


class FeeCorrectionTests(unittest.TestCase):
    def test_ui_entry_selects_explicit_account_before_importing_legacy_config(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);save(root/'config/execution_live.json',dict(enabled=False,minimum_commission_twd=1))
            for name in ('.env','data/execution/live_execution.sqlite3','data/analysis/market_history.sqlite3'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
            script="""import sys,types,os
from pathlib import Path
from opportunity_flow.control_panel import launch
selected=Path(sys.argv[1]).resolve()
ui=types.ModuleType('opportunity_flow.dashboard')
def fake_launch(args):
    from config import ROOT
    assert ROOT==selected
    assert Path(os.environ['TRADING_STATE_ROOT'])==selected
    return 42
ui.launch=fake_launch
sys.modules['opportunity_flow.dashboard']=ui
assert launch(types.SimpleNamespace(state_root=str(selected)))==42
"""
            result=subprocess.run([sys.executable,'-c',script,str(root)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_total_quantity_editor_records_handoff_and_zero_restores_manual_ownership(self):
        from opportunity_flow.legacy_runtime import LegacyRuntime
        from execution.execution_store import ExecutionStore
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);save(root/'config/execution_live.json',dict(enabled=True,minimum_commission_twd=1,min_buy_fee_reserve_twd=20))
            runtime=LegacyRuntime.__new__(LegacyRuntime);runtime.root=root;runtime.read_only=False
            runtime.stop_event=threading.Event();runtime.operation_lock=threading.RLock()
            runtime.store=ExecutionStore(root/'data/execution/live_execution.sqlite3')
            snapshot=dict(checked_at=datetime.now(TAIPEI).isoformat(),broker_inventory_verified=True,errors=[],ai_positions={},
                inventory_comparison=[dict(symbol='1111',broker_cash_qty=30)],ai_committed_positions=[])
            runtime.refresh=lambda:snapshot
            result=runtime.configure_position('1111',10,'100')
            self.assertEqual((result['ai_qty'],result['user_qty']),(10,20))
            self.assertFalse(runtime.handoff_context('1111',10)['original_buy_plan_known'])
            snapshot['ai_positions']={'1111':10}
            snapshot['ai_committed_positions']=[dict(symbol='1111',qty=10,committed_cost_twd='1020')]
            result=runtime.configure_position('1111',0)
            self.assertEqual((result['ai_qty'],result['user_qty']),(0,30));self.assertFalse(runtime.store.ai_trading_enabled('1111'))
            with runtime.store._connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM execution_attempts').fetchone()[0],0)

    def test_stopping_controller_never_opens_a_replacement_broker_for_ui_action(self):
        controller=FlowController(Path('isolated'),SimpleNamespace(state='isolated',read_only=False))
        controller.thread=SimpleNamespace(is_alive=lambda:True);controller.status='STOPPING';controller.stop_event.set()
        controller.coordinator=SimpleNamespace(closed=False)
        operation=Mock()
        with self.assertRaisesRegex(RuntimeError,'停止'):controller.runtime_action(operation)
        operation.assert_not_called()
        controller.stop();self.assertTrue(controller.wake.is_set())

    def test_real_view_adapter_labels_public_reference_and_never_returns_trade_intents(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);args=SimpleNamespace(state=str(root/'flow'),dataset=str(root/'dataset'),read_only=False)
            save(Path(args.state)/'latest.json',scene())
            save(Path(args.dataset)/'master.json',{'1111':dict(name='測試')})
            save(Path(args.state)/'public_quotes.json',dict(quotes=[dict(symbol='2222',last_price=88,quote_status='PUBLIC_REFERENCE',quote_timestamp=datetime.now(TAIPEI).isoformat())]))
            service=FlowViewService(root,args)
            with patch('execution.health_monitor.latest_refresh_snapshot',return_value={}):
                result,positions,health=service.refresh()
            quote=next(q for q in result['batch']['quotes'] if q['symbol']=='2222')
            self.assertEqual(quote['quote_status'],'PUBLIC_REFERENCE');self.assertEqual(result['decision']['decisions'],[])
            self.assertFalse(service.controller.running)

    def test_corrupt_view_state_keeps_last_good_view_and_reports_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);args=SimpleNamespace(state=str(root/'flow'),dataset=str(root/'dataset'),read_only=False)
            path=Path(args.state)/'latest.json';save(path,scene());service=FlowViewService(root,args)
            with patch('execution.health_monitor.latest_refresh_snapshot',return_value={}):
                service.refresh();save(path,{'summary':{},'candidates':None});result,_,_=service.refresh()
            self.assertEqual(len(result['flow']['candidates']),3)
            self.assertEqual(service.read_errors,['FLOW_READ_ERROR:ValueError'])
    def test_official_minimum_and_cash_reserve_are_separate_without_risk_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);before=dict(enabled=True,min_buy_fee_reserve_twd=20,buy_cash_buffer_rate=.01,
                planned_limits_enabled=False,max_daily_buy_twd=30000,min_available_to_buy_twd=10000,max_buy_qty=999)
            save(root/'config/execution_live.json',before)
            report=correct_legacy_capital_fee(root,root/'audit');after=read(root/'config/execution_live.json')
            self.assertTrue(report['changed']);self.assertEqual(after['minimum_commission_twd'],1)
            self.assertEqual({k:after[k] for k in before},before)
            self.assertEqual(after['commission_assumption_source'],SOURCE)
            self.assertFalse(after['commission_account_verified'])
            from execution.auto_advice_executor import LiveExecutionConfig
            cfg=LiveExecutionConfig.load(root/'config/execution_live.json')
            self.assertEqual(cfg.economic_minimum_commission_twd,Decimal(1));self.assertEqual(cfg.min_buy_fee_reserve_twd,Decimal(20))
            self.assertFalse(correct_legacy_capital_fee(root,root/'audit2')['changed'])

    def test_explicit_account_fee_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);save(root/'config/execution_live.json',dict(minimum_commission_twd=5,min_buy_fee_reserve_twd=20))
            self.assertFalse(correct_legacy_capital_fee(root,root/'audit')['changed'])
            self.assertEqual(fee_status(root)['minimum_commission_twd'],5)

    def test_active_broker_owner_blocks_shared_configuration_edit(self):
        from state_lock import StateLease
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config/execution_live.json';save(path,dict(min_buy_fee_reserve_twd=20));before=path.read_bytes()
            with StateLease(root,'broker-runtime'):
                with self.assertRaises(RuntimeError):correct_legacy_capital_fee(root,root/'audit')
            self.assertEqual(path.read_bytes(),before)

    def test_display_projection_never_creates_an_execution_decision(self):
        result=project_research(scene())
        self.assertTrue(result['display_only']);self.assertFalse(result['decision']['decisions'])
        self.assertEqual(result['batch']['quotes'][0]['quote_status'],'LIVE')


if __name__=='__main__':unittest.main()
