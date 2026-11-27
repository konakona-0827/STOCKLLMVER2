"""Real Windows allocators and broker-owner shutdown, without account access."""
import ctypes
import gc
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from execution.capital_reply_session import CapitalReplySession,CapitalReplySessionError
from execution.broker_runtime import BrokerRuntime
from execution.com_lifetime import stock_record,counters,pump_events,_api
from state_lock import StateLease


@unittest.skipUnless(os.name=='nt','Windows Automation allocator')
class AutomationLifetimeTests(unittest.TestCase):
    def setUp(self):
        import comtypes
        class Stock(ctypes.Structure):
            _fields_=[('bstrStockNo',comtypes.BSTR),('bstrStockName',comtypes.BSTR),('nClose',ctypes.c_long)]
        self.Stock=Stock;self.automation=_api()[3]
        self.automation.SysAllocString.argtypes=[ctypes.c_wchar_p]
        self.automation.SysAllocString.restype=ctypes.c_void_p
        self.records=[]

    def output(self,record):
        self.records.append(record)
        for name,text in (('bstrStockNo','1234'),('bstrStockName','測試公司')):
            pointer=self.automation.SysAllocString(text)
            self.assertTrue(pointer)
            ctypes.c_void_p.from_address(ctypes.addressof(record)+getattr(self.Stock,name).offset).value=pointer
        record.nClose=1234

    def test_alias_record_is_copied_and_each_native_string_freed_once(self):
        before=counters()
        def method(record):
            self.output(record)
            return self.Stock.from_buffer(record),0
        result=stock_record(self.Stock,method)
        gc.collect()
        self.assertEqual(result.bstrStockNo,'1234');self.assertEqual(result.bstrStockName,'測試公司')
        self.assertEqual(result.nClose,1234)
        self.assertEqual(counters()['bstr_freed']-before['bstr_freed'],2)
        self.assertEqual(counters()['bstr_bytes_freed']-before['bstr_bytes_freed'],16)
        self.assertIsNone(self.records[0].bstrStockNo)

    def test_separate_returned_record_and_failed_calls_also_release_output(self):
        before=counters()['bstr_freed']
        def separate(record):
            self.output(record);other=self.Stock();self.output(other)
            return other,0
        self.assertEqual(stock_record(self.Stock,separate).bstrStockNo,'1234')
        self.assertEqual(counters()['bstr_freed']-before,4)
        for fail in ('rc','exception'):
            def failed(record):
                self.output(record)
                if fail=='exception':raise RuntimeError('native call failed')
                return record,123
            with self.subTest(fail=fail),self.assertRaises((ValueError,RuntimeError)):
                stock_record(self.Stock,failed)
        self.assertEqual(counters()['bstr_freed']-before,8)

    def test_native_wait_avoids_the_per_call_console_callback_registration(self):
        import comtypes.client
        import comtypes.client._events as events
        with patch.object(events,'_SetConsoleCtrlHandler',side_effect=AssertionError('unsafe callback registration')):
            for _ in range(1000):pump_events(comtypes.client,0)

    def test_native_message_window_callbacks_are_dispatched_on_owner_thread(self):
        from ctypes import wintypes as w
        user=ctypes.WinDLL('user32',use_last_error=True)
        callback_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
        class WindowClass(ctypes.Structure):
            _fields_=[('style',w.UINT),('procedure',callback_type),('class_extra',ctypes.c_int),
                ('window_extra',ctypes.c_int),('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),
                ('brush',w.HBRUSH),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
        user.DefWindowProcW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM];user.DefWindowProcW.restype=ctypes.c_ssize_t
        received=[]
        @callback_type
        def procedure(hwnd,message,wp,lp):
            if message==0x8001:received.append(threading.get_ident());return 0
            return user.DefWindowProcW(hwnd,message,wp,lp)
        name='FlowPumpTest'+str(id(self));wc=WindowClass();wc.procedure=procedure;wc.name=name
        user.RegisterClassW.argtypes=[ctypes.POINTER(WindowClass)];user.RegisterClassW.restype=w.WORD
        user.CreateWindowExW.argtypes=[w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,
            ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,ctypes.c_void_p]
        user.CreateWindowExW.restype=w.HWND
        user.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM];user.PostMessageW.restype=w.BOOL
        user.DestroyWindow.argtypes=[w.HWND];user.UnregisterClassW.argtypes=[w.LPCWSTR,w.HINSTANCE]
        self.assertTrue(user.RegisterClassW(ctypes.byref(wc)))
        hwnd=user.CreateWindowExW(0,name,'',0,0,0,0,0,w.HWND(-3),None,None,None)
        try:
            self.assertTrue(hwnd);self.assertTrue(user.PostMessageW(hwnd,0x8001,0,0))
            import comtypes.client
            pump_events(comtypes.client,.01)
            self.assertEqual(received,[threading.get_ident()])
        finally:
            if hwnd:user.DestroyWindow(hwnd)
            user.UnregisterClassW(name,None)


class BrokerTeardownTests(unittest.TestCase):
    def session(self,root):
        return CapitalReplySession(project_root=root,dll_path=Path(root)/'unused.dll')

    def test_unadvise_and_drop_interfaces_before_com_uninitialization(self):
        events=[];owner=[]
        class Connection:
            def disconnect(self):events.append(('unadvise',threading.get_ident()))
        class Interface:
            def __del__(self):events.append(('release',threading.get_ident()))
        def create(root):
            session=self.session(root)
            def connect():
                owner.append(threading.get_ident());session._owner_thread_id=owner[0]
                for name in ('_skQ','_skC','_skO','_skR'):setattr(session,name,Interface())
                for name in ('_reply_conn','_order_conn','_center_conn'):setattr(session,name,Connection())
            session.connect=connect
            return session
        com=SimpleNamespace(CoInitialize=Mock(),CoUninitialize=lambda:events.append(('uninitialize',threading.get_ident())))
        with tempfile.TemporaryDirectory() as root,patch.dict(sys.modules,{'comtypes':com}),\
             patch('execution.broker_runtime.build_live_readonly_session_from_project',side_effect=create),\
             patch('execution.health_monitor.run_health_check',return_value={'status':'OK'}):
            runtime=BrokerRuntime(root);runtime.health();self.assertEqual(runtime.shutdown()['status'],'STOPPED')
        self.assertEqual([x[0] for x in events],['unadvise']*3+['release']*4+['uninitialize'])
        self.assertTrue(all(tid==owner[0] for _,tid in events))

    def test_cleanup_failure_keeps_owner_and_lease_and_blocks_all_new_work(self):
        close=Mock(side_effect=[RuntimeError('Unadvise failed'),None])
        session=SimpleNamespace(connect=Mock(),close=close)
        com=SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())
        with tempfile.TemporaryDirectory() as root,patch.dict(sys.modules,{'comtypes':com}),\
             patch('execution.broker_runtime.build_live_readonly_session_from_project',return_value=session),\
             patch('execution.health_monitor.run_health_check',return_value={'status':'OK'}):
            runtime=BrokerRuntime(root);runtime.health()
            self.assertEqual(runtime.shutdown()['status'],'STOPPING_BROKER_CLEANUP_PENDING')
            com.CoUninitialize.assert_not_called()
            with self.assertRaises(RuntimeError):StateLease(root,'broker-runtime').acquire()
            with self.assertRaises(RuntimeError):runtime.execute('never-send.json')
            self.assertEqual(runtime.shutdown()['status'],'STOPPED')
            com.CoUninitialize.assert_called_once()

    def test_session_cleanup_refuses_another_thread_and_preserves_failed_receiver(self):
        with tempfile.TemporaryDirectory() as root:
            session=self.session(root);session._owner_thread_id=-1
            with self.assertRaisesRegex(CapitalReplySessionError,'COM thread'):session.close()
            session._owner_thread_id=threading.get_ident()
            connection=Mock();connection.disconnect.side_effect=[RuntimeError('busy'),None]
            session._reply_conn=connection;session._skR=object()
            with self.assertRaisesRegex(CapitalReplySessionError,'CLEANUP_PENDING'):session.close()
            self.assertIs(session._reply_conn,connection);self.assertIsNotNone(session._skR)
            session.close();self.assertIsNone(session._reply_conn);self.assertIsNone(session._skR)

    def test_quote_disconnect_failure_keeps_its_native_objects_until_retry(self):
        from capital import CapitalMarket
        market=CapitalMarket(Mock());connection=Mock()
        connection.disconnect.side_effect=[RuntimeError('busy'),None]
        market.connections=[connection];market.sinks=[object()];quote=object()
        market.objects={'SKQuoteLib':quote}
        with self.assertRaisesRegex(RuntimeError,'QUOTE_EVENT_CLEANUP_PENDING'):market.close()
        self.assertIs(market.objects['SKQuoteLib'],quote);self.assertEqual(market.connections,[connection])
        self.assertTrue(market.sinks)
        market.close();self.assertFalse(market.objects);self.assertFalse(market.connections)

    def test_idle_pump_has_no_reply_history_copy_and_honors_short_wait(self):
        with tempfile.TemporaryDirectory() as root:
            session=self.session(root);session._connected=True;session._comtypes=Mock();session.snapshot.copy=Mock()
            with patch('execution.capital_reply_session.time.monotonic',side_effect=[0,0,.001,.02]):
                session.pump_idle(.01)
            session.snapshot.copy.assert_not_called()
            session._comtypes.PumpEvents.assert_called_once_with(.009000000000000001)

    def test_failed_leave_monitor_cannot_unadvise_or_free_an_active_monitor(self):
        from capital import CapitalMarket
        market=CapitalMarket(Mock());market.entered=True;connection=Mock()
        market.connections=[connection];market.sinks=[object()]
        quote=Mock();quote.SKQuoteLib_LeaveMonitor.side_effect=[9999,0]
        market.objects={'SKQuoteLib':quote}
        with self.assertRaisesRegex(RuntimeError,'QUOTE_MONITOR_CLEANUP_PENDING'):market.close()
        self.assertTrue(market.entered);self.assertIs(market.objects['SKQuoteLib'],quote)
        connection.disconnect.assert_not_called()
        market.close();connection.disconnect.assert_called_once()
        self.assertFalse(market.entered);self.assertFalse(market.objects)

    def test_shutdown_blocks_financial_jobs_already_waiting_in_owner_queue(self):
        from concurrent.futures import Future
        entered=threading.Event();release=threading.Event();queued=Future()
        def health(*args,**kwargs):entered.set();release.wait(3);return {'status':'OK'}
        session=SimpleNamespace(connect=Mock(),close=Mock())
        com=SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())
        with tempfile.TemporaryDirectory() as root,patch.dict(sys.modules,{'comtypes':com}),\
             patch('execution.broker_runtime.build_live_readonly_session_from_project',return_value=session),\
             patch('execution.health_monitor.run_health_check',side_effect=health),\
             patch('execution.pipeline_hook.execute_after_advice_written') as send:
            runtime=BrokerRuntime(root)
            reader=threading.Thread(target=runtime.health);reader.start();self.assertTrue(entered.wait(2))
            runtime._queue.put(('execute',(Path('never-send.json'),),queued))
            runtime._closing.set();release.set();reader.join(3)
            try:
                with self.assertRaisesRegex(RuntimeError,'SHUTDOWN_PENDING'):queued.result(timeout=2)
                send.assert_not_called()
            finally:runtime.shutdown()


if __name__=='__main__':unittest.main()
