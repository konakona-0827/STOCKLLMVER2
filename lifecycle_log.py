"""Append-only process lifecycle events for postmortem correlation."""
import faulthandler
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from config import TAIPEI, redact


class LifecycleLog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'dashboard_lifecycle.jsonl'
        self.run_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._fault_file = None

    def event(self, kind, **detail):
        instant = datetime.now(timezone.utc)
        row = {
            'time_utc': instant.isoformat(timespec='milliseconds'),
            'time_taipei': instant.astimezone(TAIPEI).isoformat(timespec='milliseconds'),
            'run_id': self.run_id,
            'pid': os.getpid(),
            'uptime_seconds': round(time.monotonic() - self.started, 3),
            'event': kind,
            'detail': detail,
        }
        line = redact(json.dumps(row, ensure_ascii=False, default=str)) + '\n'
        try:
            with self._lock:
                with self.path.open('a', encoding='utf-8') as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            print(f'Lifecycle log write failed: {redact(exc)}', file=sys.stderr)

    def start(self):
        self.event('START', parent_pid=os.getppid(), python=sys.version.split()[0],
                   platform=platform.platform(), executable=sys.executable)
        # Native crashes can bypass Python exception handlers. Their traceback is
        # written to a separate append-only file, while the missing EXIT remains
        # visible in the structured lifecycle log.
        try:
            self._fault_file = (self.directory / 'dashboard_faults.log').open('a', encoding='utf-8')
            self._fault_file.write(f'\nrun_id={self.run_id} pid={os.getpid()} start_utc={datetime.now(timezone.utc).isoformat()}\n')
            self._fault_file.flush()
            faulthandler.enable(file=self._fault_file, all_threads=True)
        except OSError as exc:
            print(f'Native fault log unavailable: {redact(exc)}', file=sys.stderr)

    def exception(self, kind, exc_type, exc_value, exc_tb):
        self.event(kind, error_type=exc_type.__name__,
                   traceback=''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    def finish(self, reason, **detail):
        try:
            self.event('EXIT', reason=reason, **detail)
        finally:
            if self._fault_file is not None:
                faulthandler.disable()
                self._fault_file.close()
                self._fault_file = None
