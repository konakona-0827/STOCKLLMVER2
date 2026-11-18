"""Restart FLOW after process exit; a living broker owner is never force-replaced."""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from opportunity_flow.common import read, save
from opportunity_flow.recovery import folder, heartbeat_reason, intent, set_intent
from state_lock import StateLease


def record(root, event, **detail):
    path = folder(root) / 'supervisor.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(), event=event,
                         supervisor_pid=os.getpid(), **detail), ensure_ascii=False) + '\n')


def status(root, state, **detail):
    save(folder(root) / 'supervisor_status.json', dict(at=datetime.now(timezone.utc).isoformat(),
         supervisor_pid=os.getpid(), state=state, **detail))


def broker_owner_absent(root):
    """The existing account leases protect direct launchers and every worktree."""
    try:
        with StateLease(root, 'dashboard'), StateLease(root, 'broker-runtime'):
            return True
    except RuntimeError:
        return False


def child_command(root, dataset, state, *, headless=False, model=None):
    command = [sys.executable, '-X', 'faulthandler', '-m', 'opportunity_flow', 'live',
               '--state-root', str(root), '--dataset', str(dataset), '--state', str(state)]
    if headless:
        command.append('--headless')
    if model:
        command.extend(['--model', model])
    return command


def launch_detached(root,dataset,state,*,headless=False,model=None):
    executable=Path(sys.executable)
    if os.name=='nt' and executable.with_name('pythonw.exe').is_file():
        executable=executable.with_name('pythonw.exe')
    command=[str(executable),'-m','operations.flow_supervisor','--state-root',str(root),
             '--dataset',str(dataset),'--state',str(state)]
    if headless:command.append('--headless')
    if model:command.extend(['--model',model])
    target=folder(root);target.mkdir(parents=True,exist_ok=True)
    with (target/'supervisor_output.log').open('ab') as output:
        child=subprocess.Popen(command,cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return child.pid


def supervise(root, dataset, state, *, headless=False, model=None, stop=None, poll_seconds=5):
    """Only a dead child may be replaced; stalled owners receive a graceful stop request."""
    import threading
    stop = stop or threading.Event()
    root, dataset, state = Path(root), Path(dataset), Path(state)
    target = folder(root)
    target.mkdir(parents=True, exist_ok=True)
    try:
        lease = StateLease(root, 'flow-supervisor').acquire()
    except RuntimeError:
        record(root, 'SUPERVISOR_ALREADY_RUNNING')
        return 0
    try:
        history = read(target / 'restart_history.json')
        recent = deque(t for t in history if isinstance(t,(int,float)) and 0 <= time.time()-t <= 600)
    except (OSError,ValueError,TypeError):
        recent = deque()
    def failed_launch():
        recent.append(time.time())
        save(target / 'restart_history.json',list(recent))
    child = None
    try:
        record(root, 'SUPERVISOR_START', code_root=str(ROOT), headless=headless)
        while not stop.is_set():
            if not intent(root):
                status(root, 'MANUALLY_PAUSED')
                return 0
            if not broker_owner_absent(root):
                status(root, 'EXISTING_ACCOUNT_OWNER', automatic_duplicate_start=False)
                stop.wait(poll_seconds)
                continue
            now = time.time()
            while recent and now - recent[0] > 600:
                recent.popleft()
            if len(recent) >= 3:
                status(root, 'RESTART_BACKOFF', next_retry_at=recent[0] + 600)
                stop.wait(poll_seconds)
                continue
            run_id = uuid4().hex
            env = dict(os.environ, TRADING_STATE_ROOT=str(root), FLOW_RECOVERY_ROOT=str(root),
                       FLOW_RECOVERY_RUN_ID=run_id)
            with (target / 'child_output.log').open('ab') as output:
                launched = time.time()
                try:
                    child = subprocess.Popen(child_command(root, dataset, state, headless=headless, model=model),
                        cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except OSError as exc:
                    failed_launch()
                    record(root, 'CHILD_START_FAILED', error=type(exc).__name__)
                    stop.wait(poll_seconds)
                    continue
                record(root, 'FLOW_STARTED', child_pid=child.pid, run_id=run_id)
                requested = None
                while True:
                    try:
                        exit_code = child.wait(timeout=poll_seconds)
                        break
                    except subprocess.TimeoutExpired:
                        try:
                            row = read(target / f'runtime-{child.pid}.json')
                        except (OSError, ValueError):
                            row = {}
                        reason = heartbeat_reason(row, child.pid, run_id, launched, time.time())
                        if stop.is_set():
                            reason = 'SUPERVISOR_STOP'
                        if reason and requested is None:
                            requested = time.time()
                            save(target / 'restart_request.json', dict(pid=child.pid, run_id=run_id,
                                 reason=reason, requested_at=requested))
                            record(root, 'GRACEFUL_RECOVERY_REQUESTED', child_pid=child.pid, reason=reason)
                        stalled = requested is not None and time.time() - requested > 300
                        status(root, 'MANUALLY_PAUSED' if not intent(root) else
                               'STALLED_OWNER_WAITING' if stalled else 'RECOVERING' if requested else
                               'RUNNING' if row.get('account_verified') and row.get('controller_status') == 'RUNNING'
                               else 'AWAITING_FRESH_ACCOUNT',
                               child_pid=child.pid, run_id=run_id, heartbeat=row,
                               automatic_force_kill=False)
                        # Even after a stall, do not kill an uncertain financial
                        # action, release its lease, or launch a second COM owner.
                record(root, 'FLOW_EXITED', child_pid=child.pid, run_id=run_id, exit_code=exit_code)
                child = None
            if stop.is_set() or not intent(root):
                status(root, 'MANUALLY_PAUSED' if not intent(root) else 'STOPPED')
                return 0
            failed_launch()
            stop.wait(poll_seconds)
        return 0
    finally:
        lease.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--model')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    from opportunity_flow.live import resolve_account_root
    root = resolve_account_root(args.state_root)
    if args.install:
        if os.name != 'nt':
            parser.error('Windows recovery registration requires Windows')
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(ROOT / 'operations/InstallOpportunityFlowRecovery.ps1'),
                   '-StateRoot', str(root), '-Dataset', str(args.dataset.resolve()),
                   '-State', str(args.state.resolve()), '-PythonPath', sys.executable, '-RegisterOnly']
        if args.headless:
            command.append('-Headless')
        if args.model:
            command.extend(['-Model', args.model])
        subprocess.run(command, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if args.resume:
        set_intent(root, True, 'EXPLICIT_LIVE_LAUNCH')
    if args.install:
        if args.headless:
            # Preserve foreground Ctrl+C for the CLI while scheduled recovery
            # uses the same guarded lifecycle in the logged-on user's session.
            import signal
            import threading
            stop = threading.Event()
            def pause(signum, frame):
                from opportunity_flow.recovery import windows_shutting_down
                if not windows_shutting_down():
                    set_intent(root, False, 'USER_KEYBOARD_INTERRUPT')
                stop.set()
            previous = signal.signal(signal.SIGINT, pause)
            try:
                return supervise(root,args.dataset.resolve(),args.state.resolve(),
                                 headless=True,model=args.model,stop=stop)
            finally:
                signal.signal(signal.SIGINT, previous)
        # Closing OpportunityFlow.cmd's console must not kill supervision.
        pid=launch_detached(root,args.dataset.resolve(),args.state.resolve(),headless=args.headless,model=args.model)
        print(json.dumps(dict(status='RECOVERY_LAUNCHED',supervisor_pid=pid,
                              fresh_broker_reconciliation_required=True)))
        return 0
    return supervise(root, args.dataset.resolve(), args.state.resolve(), headless=args.headless, model=args.model)


if __name__ == '__main__':
    raise SystemExit(main())
