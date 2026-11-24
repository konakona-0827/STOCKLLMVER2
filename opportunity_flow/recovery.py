"""Account-scoped recovery intent and liveness; never authorizes a trade."""
from datetime import datetime, timezone
from decimal import Decimal,InvalidOperation
import os
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import subprocess
import threading
import time

from .common import read, save


@lru_cache(maxsize=1)
def code_revision():
    try:
        value = subprocess.check_output(['git','rev-parse','HEAD'],cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ),stderr=subprocess.DEVNULL,timeout=3,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        revision=value.decode().strip()
        return revision if len(revision)==40 and all(c in '0123456789abcdef' for c in revision) else None
    except (OSError,subprocess.SubprocessError):
        return None


@lru_cache(maxsize=32)
def runtime_code_matches(expected,actual,code_root=None):
    if expected and expected==actual:return True
    if any(not isinstance(value,str) or len(value)!=40 or any(c not in '0123456789abcdef' for c in value)
           for value in (expected,actual)):
        return False
    try:
        result=subprocess.run(['git','diff','--quiet',expected,actual,'--','.',
            ':(exclude)reports/**',':(exclude)docs/**',':(exclude)README*'],
            cwd=code_root or Path(__file__).resolve().parents[1],stdout=subprocess.DEVNULL,
            env=dict(os.environ),stderr=subprocess.DEVNULL,timeout=3,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return result.returncode==0
    except (OSError,subprocess.SubprocessError):
        return False


def source_fingerprint(root):
    """Tracked executable/policy content; report and test commits are excluded."""
    root=Path(root).resolve()
    try:
        raw=subprocess.check_output(['git','ls-files','-z','--','*.py','*.ps1','*.cmd','*.json',
            'requirements*.txt','pyproject.toml','strategies/*.md','prompts/*.md','prompt*.md','skills/*.md'],cwd=root,
            env=dict(os.environ),stderr=subprocess.DEVNULL,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        files=[]
        for name in sorted(raw.decode('utf-8').split('\0')):
            if not name or name.startswith(('reports/','tests/','docs/')):continue
            path=(root/name).resolve()
            if not path.is_relative_to(root):return None
            files.append((name,hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()))
        return hashlib.sha256(json.dumps(files,separators=(',',':')).encode()).hexdigest() if files else None
    except (OSError,ValueError,UnicodeError,subprocess.SubprocessError):return None


@lru_cache(maxsize=1)
def runtime_fingerprint():
    return source_fingerprint(Path(__file__).resolve().parents[1])


def same_runtime_code(revision,left,right_revision,right):
    def valid(value,size):
        return isinstance(value,str) and len(value)==size and all(c in '0123456789abcdef' for c in value)
    if left is not None and right is not None:
        return valid(left,64) and valid(right,64) and left==right
    return runtime_code_matches(revision,right_revision)


STAGES = frozenset(('ACCOUNT_CHECK_STARTED','ACCOUNT_RECONCILED','ACCOUNT_CHECK_FAILED','WATCH_STARTED',
    'WAITING_RESEARCH_WINDOW','WAITING_DAILY_DATA','RESEARCH_CYCLE_COMPLETED','RESEARCH_REVIEW_STARTED',
    'RESEARCH_REVIEW_COMPLETED','RESEARCH_ERROR','PRETRADE_STARTED','PRETRADE_COMPLETED',
    'EXECUTOR_DISPATCHED','EXECUTOR_RETURNED','ORDER_SENT','FILL_CONFIRMED','POSITION_MANAGEMENT','EXECUTION_BLOCKED',
    'DISPATCH_UNKNOWN','BROKER_SESSION_RESTART_REQUESTED','SUPERVISOR_RECOVERY_REQUESTED',
    'PRETRADE_APPROVED','PRETRADE_VETOED','PRETRADE_REVIEW_ERROR'))

BLOCK_REASONS=frozenset(('USER_STOPPED_OR_OUTSIDE_TRADING_SESSION','PRETRADE_VETO_OR_ERROR',
    'BROKER_ACCOUNT_UNVERIFIED','BROKER_ACCOUNT_STALE','BROKER_INVENTORY_UNVERIFIED','CURRENT_ACCOUNT_ALLOWANCE_PAUSED',
    'LIVE_NON_TRIAL_QUOTE_REQUIRED','ODDLOT_QUOTE_MISSING','VERIFIED_ODDLOT_QUOTE_BASIS_REQUIRED',
    'QUOTE_STALE_OR_FUTURE','CROSSED_QUOTE','BROKER_MARKET_NOT_VERIFIED','WAITING_FOR_REVIEWED_ENTRY',
    'FRESH_CAPITAL_OR_ALLOCATION_INSUFFICIENT','FRESH_TRADE_ECONOMICS_FAILED','TWO_RESEARCH_APPROVALS_REQUIRED',
    'RESEARCH_REVIEW_STALE','PENDING_ORDER_OR_AI_DISABLED','SELL_PENDING_ORDER_REQUIRES_RECONCILIATION'))


def money(value):
    try:
        number=Decimal(str(value))
        return str(number) if number.is_finite() and number>=0 else None
    except (InvalidOperation,ValueError,TypeError):return None


def safe_details(detail):
    # Never export raw broker replies, model prose, account identity or paths.
    if not isinstance(detail,dict):return {}
    result={}
    for key in ('count','blocked_count','filled_quantity','eligible_count','ready_count'):
        value=detail.get(key)
        if type(value) is int and value>=0:result[key]=value
    if detail.get('side') in ('BUY','SELL'):result['side']=detail['side']
    if isinstance(detail.get('reason'),str) and detail['reason'] in BLOCK_REASONS:result['reason']=detail['reason']
    symbol=detail.get('symbol')
    if isinstance(symbol,str) and 4<=len(symbol)<=6 and symbol.isascii() and symbol.isdigit():result['symbol']=symbol
    return result


def observe(stage, **detail):
    try:
        probe=get_probe()
        if probe is not None:probe.event(stage,**detail)
    except Exception:
        # A diagnostic write must never turn a completed send into a retry.
        return


def observe_execution(report):
    observe('EXECUTOR_RETURNED')
    if isinstance(report,dict) and report.get('status')=='DISPATCH_UNKNOWN_DO_NOT_RETRY':
        observe('DISPATCH_UNKNOWN')
    if not isinstance(report,dict) or not isinstance(report.get('results'),list):return
    for result in report['results']:
        if not isinstance(result,dict):continue
        detail=dict(symbol=result.get('symbol'),side=result.get('side'),filled_quantity=result.get('filled_quantity'))
        if result.get('status')=='UNCONFIRMED':observe('DISPATCH_UNKNOWN',**detail)
        if result.get('broker_order_sent') is True:observe('ORDER_SENT',**detail)
        if type(result.get('filled_quantity')) is int and result['filled_quantity']>0:
            observe('FILL_CONFIRMED',**detail)


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
        self.row = dict(pid=self.pid, run_id=run_id, started_at=clock(),code_revision=code_revision(),
                        runtime_fingerprint=runtime_fingerprint(),stages={},events=[])
        self.last_saved = 0
        self.write_failures=0
        self.last_write_failure=None
        self.last_write_success=None
        self._local_restart=False
        self._supervision_checked=-float('inf')
        self._supervision_lock=threading.Lock()

    def request_restart(self,reason):
        # Scope every request to this launch. No manual intent is changed.
        self._local_restart=True
        try:
            save(folder(self.root)/'restart_request.json',dict(pid=self.pid,run_id=self.run_id,
                 reason=reason,requested_at=self.clock()))
        except OSError:
            pass  # The current process still observes its in-memory request.

    def save_heartbeat(self,now):
        self.last_saved=now
        self.row['diagnostics']=dict(heartbeat_write_failures=self.write_failures,
            last_write_failed_at=self.last_write_failure,last_write_succeeded_at=now)
        try:
            save(self.path,self.row)
            self.last_write_success=now
        except OSError:
            # Diagnostic contention must not stop Tk callbacks or the watch loop.
            # Keep the accumulated events in memory for the next scheduled write.
            self.write_failures+=1
            self.last_write_failure=now
            self.row['diagnostics']=dict(heartbeat_write_failures=self.write_failures,
                last_write_failed_at=now,last_write_succeeded_at=self.last_write_success)

    def ensure_supervision(self):
        dataset=os.environ.get('FLOW_RECOVERY_DATASET')
        state=os.environ.get('FLOW_RECOVERY_STATE')
        if not dataset or not state or self._local_restart:return
        if time.monotonic()-self._supervision_checked<10:return
        if not self._supervision_lock.acquire(blocking=False):return
        try:
            self._supervision_checked=time.monotonic()
            if not intent(self.root) or windows_shutting_down():return
            from state_lock import StateLease
            try:
                lease=StateLease(self.root,'flow-supervisor').acquire()
            except RuntimeError:return  # A live guardian still owns the OS lease.
            lease.close()
            from operations.flow_supervisor import launch_detached
            launch_detached(self.root,Path(dataset),Path(state),
                headless=os.environ.get('FLOW_RECOVERY_HEADLESS')=='1',
                model=os.environ.get('FLOW_RECOVERY_MODEL') or None)
            self.event('SUPERVISOR_RECOVERY_REQUESTED')
            self.request_restart('SUPERVISOR_LOST')
            # The new guardian waits for the existing dashboard/broker leases.
        except (OSError,ValueError,RuntimeError):
            # Keep the current owner; a later tick retries diagnostics/recovery.
            return
        finally:self._supervision_lock.release()

    def event(self, stage, **detail):
        if stage not in STAGES:return
        now=self.clock()
        with self.lock:
            stages=self.row['stages']
            previous=stages.get(stage,{})
            stages[stage]=dict(first_at=previous.get('first_at',now),last_at=now,
                               count=previous.get('count',0)+1,detail=safe_details(detail))
            self.row['events']=(self.row['events']+[dict(at=now,stage=stage,**safe_details(detail))])[-100:]
            self.row.update(at=now,desired_running=intent(self.root))
            self.save_heartbeat(now)

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
                runtime=getattr(coordinator,'runtime',None)
                self.row['read_only'] = getattr(runtime,'read_only',None) is not False
                self.row['executor_enabled'] = getattr(getattr(runtime,'config',None),'enabled',None) is True
                self.row['budget_ready'] = snapshot.get('budget_status') == 'READY'
                cfg=getattr(runtime,'config',None)
                self.row['budget']={key:money(value) for key,value in dict(
                    cash_reserve_twd=getattr(cfg,'min_available_to_buy_twd',None),
                    ai_cap_twd=snapshot.get('strategy_cap_twd'),
                    current_buy_budget_twd=snapshot.get('current_buy_budget_twd'),
                    daily_buy_cap_twd=getattr(cfg,'max_daily_buy_twd',None)).items()}
            if force or now - self.last_saved >= 5:
                self.row.update(at=now, desired_running=intent(self.root))
                self.save_heartbeat(now)
        self.ensure_supervision()

    def restart_requested(self):
        if self._local_restart:return True
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
