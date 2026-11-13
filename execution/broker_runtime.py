"""One COM-owning broker thread shared by quotes, execution and health checks."""
from __future__ import annotations

from concurrent.futures import Future
from math import ceil
from pathlib import Path
from queue import Queue, Empty
from threading import Event, Lock, Thread
from time import monotonic,perf_counter_ns
from state_lock import StateLease

from .capital_reply_session import build_live_readonly_session_from_project
from .timing import current_trace,set_trace,reset_trace,mark


class BrokerRuntime:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self._lease = StateLease(self.project_root, 'broker-runtime').acquire()
        self._queue: Queue = Queue()
        self._unresponsive = Event()
        self._submission_lock = Lock()
        self._shutdown_future = None
        self._thread = Thread(target=self._run, name="broker-runtime", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._lease.close()
            raise

    @staticmethod
    def _read_wait_seconds(kind, args):
        if kind == 'health':
            return 60.0
        if kind == 'quotes':
            # SDK timeout applies separately to each group of 20. Include
            # initial login, Reply recovery and queue time in the outer limit.
            return 45.0 + max(1, ceil(len(args[0]) / 20)) * max(0.0, float(args[3]))
        if kind == 'shutdown':
            return 5.0
        return None  # Never turn an uncertain financial action into a retry.

    def _submit(self, kind: str, *args):
        wait_seconds = self._read_wait_seconds(kind, args)
        started = monotonic()
        with self._submission_lock:
            if kind != 'shutdown' and (self._unresponsive.is_set() or self._shutdown_future is not None):
                raise RuntimeError('BROKER_READ_TIMEOUT_OR_SHUTDOWN_RESTART_REQUIRED')
            if kind == 'shutdown' and self._shutdown_future is not None:
                future = self._shutdown_future
            else:
                future = Future()
                future._execution_trace=current_trace()
                future._broker_read_deadline = (
                    started + wait_seconds if kind in ('health', 'quotes') else None
                )
                if kind == 'shutdown':
                    self._shutdown_future = future
                future._timing_queued_ns=perf_counter_ns()
                mark('broker_queue_enqueued',job_kind=kind)
                self._queue.put((kind, args, future))
        try:
            result = future.result(timeout=wait_seconds)
            if wait_seconds is not None and monotonic() - started > wait_seconds:
                # Also reject a late successful read after sleep/resume or a
                # scheduler pause; it is not fresh execution evidence.
                raise TimeoutError('BROKER_READ_COMPLETED_AFTER_DEADLINE')
            return result
        except TimeoutError as exc:
            if wait_seconds is None:
                raise
            if kind == 'shutdown':
                return dict(status='STOPPING_BROKER_UNRESPONSIVE',lease_held=self._thread.is_alive(),automatic_retry=False)
            self._unresponsive.set()
            future.cancel()  # Only cancels jobs which have not started.
            raise TimeoutError(f'BROKER_READ_DEADLINE_EXCEEDED:{kind}:RESTART_REQUIRED') from exc

    def _check_read_deadline(self, future):
        deadline = getattr(future, '_broker_read_deadline', None)
        if deadline is not None and monotonic() > deadline:
            self._unresponsive.set()
            raise TimeoutError('BROKER_READ_COMPLETED_AFTER_DEADLINE')

    def execute(self, advice_path: str | Path):
        return self._submit("execute", Path(advice_path))

    def execute_if_active(self,advice_path,stopped):
        return self._submit('execute',Path(advice_path),stopped)

    def health(self):
        return self._submit("health")

    def fetch_quotes(self, symbols, audit, stopped, timeout=12):
        return self._submit("quotes", tuple(symbols), audit, stopped, timeout)

    def fetch_quotes_with_markets(self, symbols, markets, audit, stopped, timeout=12):
        quotes,errors={},{}
        for market in (5,6):
            group=tuple(s for s in dict.fromkeys(symbols) if markets.get(s)==market)
            if not group:continue
            try:
                found,failed=self._submit('quotes',group,audit,stopped,timeout,market)
                quotes.update(found);errors.update(failed)
            except Exception as exc:
                errors.update({s:f'{type(exc).__name__}: {exc}' for s in group})
        errors.update({s:'ODDLOT_MARKET_UNKNOWN' for s in symbols if markets.get(s) not in (5,6)})
        return quotes,errors

    def cancel_exit(self,decision_id,stopped=lambda:False):
        return self._submit('cancel_exit',decision_id,stopped)

    def shutdown(self):
        if not self._thread.is_alive():
            return {"status": "ALREADY_STOPPED"}
        result = self._submit("shutdown")
        if result['status'] == 'STOPPING_BROKER_UNRESPONSIVE':
            return result
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            return dict(status='STOPPING_BROKER_UNRESPONSIVE',lease_held=True,automatic_retry=False)
        return result

    def _run(self):
        session = None
        quote_market = None
        quote_symbols = None
        login_blocked = False
        login_error = None
        com_initialized = False
        com_error = None
        try:
            import comtypes
            comtypes.CoInitialize()
            com_initialized = True
        except Exception as exc:
            com_error = f"COM initialization failed: {type(exc).__name__}: {exc}"

        try:
            while True:
                try:
                    kind, args, future = self._queue.get(timeout=.2)
                except Empty:
                    # A blocking Queue.get leaves the STA's COM callbacks
                    # unprocessed between jobs, including Reply disconnects.
                    pump=getattr(session,'pump',None)
                    if callable(pump):
                        try:pump(.01)
                        except Exception as exc:
                            login_error=f'COM event pump: {type(exc).__name__}: {exc}'
                    continue
                stop_worker = False
                if not future.set_running_or_notify_cancel():
                    self._queue.task_done()
                    continue
                trace_token=set_trace(getattr(future,'_execution_trace',None))
                try:
                    queued_ns=getattr(future,'_timing_queued_ns',None)
                    mark('broker_worker_started',job_kind=kind,queue_wait_ms=round((perf_counter_ns()-queued_ns)/1_000_000,3) if queued_ns is not None else 0)
                    self._check_read_deadline(future)
                    if kind != 'shutdown' and self._unresponsive.is_set():
                        raise RuntimeError('BROKER_READ_TIMEOUT_RESTART_REQUIRED')
                    if kind == "shutdown":
                        if quote_market is not None:
                            quote_market.close()
                            quote_market = None
                            quote_symbols = None
                        result = {"status": "STOPPED"}
                        stop_worker = True
                    elif not com_initialized:
                        if kind in ("execute", "quotes"):
                            raise RuntimeError(com_error)
                        from .health_monitor import run_health_check
                        result = run_health_check(self.project_root,
                                                  connection_error=com_error)
                    else:
                        connection_error = login_error if login_blocked else None
                        if session is None and not login_blocked:
                            candidate = None
                            try:
                                candidate = build_live_readonly_session_from_project(
                                    self.project_root
                                )
                                candidate.connect()
                                session = candidate
                                login_error = None
                            except Exception as exc:
                                session = None
                                connection_error = (
                                    f"broker connection: {type(exc).__name__}: {exc}"
                                )
                                if candidate is not None and getattr(
                                    candidate, "center_login_attempted", False
                                ):
                                    # Login may have reached SKCOM before a later
                                    # setup failure. Another login could return 2003.
                                    login_blocked = True
                                    login_error = connection_error
                        if kind == "execute":
                            if session is None:
                                raise RuntimeError(connection_error)
                            from .pipeline_hook import execute_after_advice_written
                            result = execute_after_advice_written(
                                args[0], project_root=self.project_root,
                                session=session,
                                stopped=lambda: self._unresponsive.is_set() or (len(args)>1 and args[1]()),
                            )
                        elif kind == 'cancel_exit':
                            if session is None:
                                raise RuntimeError(connection_error)
                            from .exit_cancel import request_cancel
                            result=request_cancel(self.project_root,session,args[0],
                                lambda: self._unresponsive.is_set() or args[1]())
                        elif kind == "health":
                            from .health_monitor import run_health_check
                            result = run_health_check(
                                self.project_root, session=session,
                                connection_error=connection_error,
                            )
                        elif kind == "quotes":
                            if session is None:
                                raise RuntimeError(connection_error)
                            from capital_multi_quote import MultiCapitalMarket
                            requested_symbols = tuple(dict.fromkeys(args[0]))
                            if (quote_market is not None
                                    and quote_symbols != requested_symbols):
                                quote_market.audit = args[1]
                                quote_market.event("QUOTE_SYMBOL_SET_CHANGED", {
                                    "old_symbol_count": len(quote_symbols or ()),
                                    "new_symbol_count": len(requested_symbols),
                                })
                                # Retain the original SKQuoteLib event connection.
                                # Disconnecting GetEvents and attaching a new sink
                                # to the same COM object immediately before
                                # EnterMonitor preceded access violations on
                                # the first AUTO scan (2026-10-12 and 10-14).
                                # LeaveMonitor clears the subscription; the same
                                # monitor/sink can then subscribe to the new set.
                                quote_market.leave_monitor()
                                quote_market.stopped = args[2]
                                quote_symbols = requested_symbols
                            if quote_market is None:
                                quote_market = MultiCapitalMarket(
                                    args[1], stopped=args[2], login_session=session,
                                )
                                quote_symbols = requested_symbols
                            else:
                                quote_market.audit = args[1]
                                quote_market.stopped = args[2]
                                quote_market.event("QUOTE_MONITOR_REUSED", {
                                    "symbol_count": len(requested_symbols),
                                })
                            try:
                                quote_market.flow_market_override=args[4] if len(args)>4 else None
                                result = quote_market.fetch_batch(
                                    requested_symbols, timeout=args[3],
                                )
                                if not quote_market.entered:
                                    # LeaveMonitor also disconnects SKReply.
                                    # The full 20+1 probe confirmed that
                                    # reconnecting Reply restores state=1.
                                    reply_health = session.ensure_ready()
                                    quote_market.event("REPLY_READY_AFTER_QUOTES", {
                                        "ready": reply_health.ready,
                                        "action": reply_health.action,
                                        "reply_state": reply_health.reply_state,
                                    })
                            except BaseException:
                                quote_market.close()
                                quote_market = None
                                quote_symbols = None
                                raise
                        else:
                            raise ValueError(f"unknown broker job: {kind}")
                    # The owner checks too, before taking any queued action.
                    # This covers a suspended caller which has not yet woken
                    # to mark its own Future wait as timed out.
                    self._check_read_deadline(future)
                    mark('broker_worker_finished',job_kind=kind)
                    future.set_result(result)
                except BaseException as exc:
                    mark('broker_worker_failed',job_kind=kind,error=type(exc).__name__)
                    future.set_exception(exc)
                finally:
                    reset_trace(trace_token)
                    self._queue.task_done()
                if stop_worker:
                    break
        finally:
            try:
                if quote_market is not None:
                    quote_market.close()
                if com_initialized:
                    comtypes.CoUninitialize()
            finally:
                self._lease.close()


_runtimes: dict[Path, BrokerRuntime] = {}
_lock = Lock()


def get_broker_runtime(project_root: str | Path) -> BrokerRuntime:
    root = Path(project_root).resolve()
    with _lock:
        if root not in _runtimes:
            _runtimes[root] = BrokerRuntime(root)
        return _runtimes[root]


def shutdown_broker_runtime(project_root: str | Path):
    root = Path(project_root).resolve()
    with _lock:
        runtime = _runtimes.get(root)
    if runtime is None:
        return {"status": "NOT_STARTED"}
    result = runtime.shutdown()
    # An unresolved COM owner keeps its OS lease. Never create a second owner
    # or reconnect blindly just because the UI stopped waiting.
    if not runtime._thread.is_alive():
        with _lock:
            if _runtimes.get(root) is runtime:
                _runtimes.pop(root)
    return result
