"""One COM-owning broker thread shared by live execution and health checks."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

from .capital_reply_session import build_live_readonly_session_from_project


class BrokerRuntime:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self._queue: Queue = Queue()
        self._thread = Thread(target=self._run, name="broker-runtime", daemon=True)
        self._thread.start()

    def _submit(self, kind: str, *args):
        future = Future()
        self._queue.put((kind, args, future))
        return future.result()

    def execute(self, advice_path: str | Path):
        return self._submit("execute", Path(advice_path))

    def health(self):
        return self._submit("health")

    def _run(self):
        session = None
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
                kind, args, future = self._queue.get()
                try:
                    if not com_initialized:
                        if kind == "execute":
                            raise RuntimeError(com_error)
                        from .health_monitor import run_health_check
                        result = run_health_check(self.project_root,
                                                  connection_error=com_error)
                    else:
                        connection_error = None
                        if session is None:
                            try:
                                session = build_live_readonly_session_from_project(
                                    self.project_root
                                )
                                session.connect()
                            except Exception as exc:
                                session = None
                                connection_error = (
                                    f"broker connection: {type(exc).__name__}: {exc}"
                                )
                        if kind == "execute":
                            if session is None:
                                raise RuntimeError(connection_error)
                            from .pipeline_hook import execute_after_advice_written
                            result = execute_after_advice_written(
                                args[0], project_root=self.project_root,
                                session=session,
                            )
                        elif kind == "health":
                            from .health_monitor import run_health_check
                            result = run_health_check(
                                self.project_root, session=session,
                                connection_error=connection_error,
                            )
                        else:
                            raise ValueError(f"unknown broker job: {kind}")
                    future.set_result(result)
                except BaseException as exc:
                    future.set_exception(exc)
                finally:
                    self._queue.task_done()
        finally:
            if com_initialized:
                comtypes.CoUninitialize()


_runtimes: dict[Path, BrokerRuntime] = {}
_lock = Lock()


def get_broker_runtime(project_root: str | Path) -> BrokerRuntime:
    root = Path(project_root).resolve()
    with _lock:
        if root not in _runtimes:
            _runtimes[root] = BrokerRuntime(root)
        return _runtimes[root]
