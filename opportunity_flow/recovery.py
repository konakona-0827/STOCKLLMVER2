"""Account-scoped recovery intent and liveness; never authorizes a trade."""
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time

from .common import read, save


def folder(root):
    return Path(root) / 'data/health/flow_recovery'


def intent(root):
    path = folder(root) / 'intent.json'
    if not path.exists():
        return True
    try:
        value = read(path)
        return isinstance(value,dict) and value.get('desired_running') is True
    except (OSError, ValueError, TypeError):
        return False  # An unreadable manual-stop record must not resume trading.


def set_intent(root, enabled, reason):
    save(folder(root) / 'intent.json', dict(desired_running=bool(enabled), reason=reason,
         changed_at=datetime.now(timezone.utc).isoformat()))


def windows_shutting_down():
    if os.name != 'nt':
        return False
    import ctypes
    return bool(ctypes.windll.user32.GetSystemMetrics(0x2000))  # SM_SHUTTINGDOWN


class RecoveryProbe:
    def __init__(self, root, run_id, *, clock=time.time, pid=None):
        self.root, self.run_id, self.clock = Path(root), run_id, clock
        self.pid = os.getpid() if pid is None else pid
        self.path = folder(root) / f'runtime-{self.pid}.json'
        self.lock = threading.Lock()
        self.row = dict(pid=self.pid, run_id=run_id, started_at=clock())
        self.last_saved = 0

    def update(self, *, gui=False, loop=False, coordinator=None, controller_status=None, force=False):
        now = self.clock()
        with self.lock:
            if gui:
                self.row['gui_at'] = now
            if loop:
                self.row['loop_at'] = now
            if controller_status is not None:
                self.row['controller_status'] = controller_status
            snapshot = getattr(coordinator, 'snapshot', None) or {}
            if coordinator is not None:
                try:
                    checked = datetime.fromisoformat(snapshot['checked_at']).timestamp()
                    fresh = 0 <= now - checked <= 120
                except (KeyError, ValueError, TypeError):
                    fresh = False
                self.row['account_verified'] = bool(fresh and snapshot.get('inventory_checked') is True
                    and not snapshot.get('errors') and not snapshot.get('reconciliation_errors')
                    and snapshot.get('status') != 'ERROR')
                self.row['account_checked_at'] = snapshot.get('checked_at')
                self.row['broker_read_blocked'] = bool(getattr(coordinator, 'broker_read_error', None))
            if force or now - self.last_saved >= 5:
                self.row.update(at=now, desired_running=intent(self.root))
                save(self.path, self.row)
                self.last_saved = now

    def restart_requested(self):
        path = folder(self.root) / 'restart_request.json'
        try:
            request = read(path)
            return isinstance(request,dict) and request.get('run_id') == self.run_id and request.get('pid') == self.pid
        except (OSError, ValueError, TypeError):
            return False

    def manual_stop(self, reason='USER_STOP'):
        set_intent(self.root, False, reason)

    def manual_start(self):
        set_intent(self.root, True, 'USER_START')


_probe = None
_probe_key = None
_probe_lock = threading.Lock()


def get_probe():
    global _probe, _probe_key
    key = (os.environ.get('FLOW_RECOVERY_ROOT'), os.environ.get('FLOW_RECOVERY_RUN_ID'))
    if not all(key):
        return None
    with _probe_lock:
        if _probe_key != key:
            _probe, _probe_key = RecoveryProbe(*key), key
        return _probe


def heartbeat_reason(row, pid, run_id, launched_at, now):
    if not isinstance(row, dict) or row.get('pid') != pid or row.get('run_id') != run_id:
        return 'STARTUP_HEARTBEAT_MISSING' if now - launched_at > 180 else None
    try:
        age = now - float(row['at'])
        if age < 0 or age > 180:
            return 'PROCESS_HEARTBEAT_STALE'
        if row.get('desired_running') is not True:
            return None
        loop = row.get('loop_at')
        if loop is None and now - launched_at > 180:
            return 'WATCH_LOOP_NOT_STARTED'
        if loop is not None and now - float(loop) > 600:
            return 'WATCH_LOOP_STALLED'
    except (KeyError, ValueError, TypeError):
        return 'INVALID_HEARTBEAT' if now - launched_at > 180 else None
    return None
