"""Explicit launcher upgrades wait for graceful financial-owner shutdown."""
import threading
import time
from uuid import uuid4

from opportunity_flow.common import read,save
from opportunity_flow.recovery import code_revision,folder,runtime_fingerprint,same_runtime_code,set_intent,windows_shutting_down
from state_lock import StateLease

PREFIX='EXPLICIT_LIVE_HANDOFF:'


def pending_token(root):
    try:
        value=read(folder(root)/'intent.json')
        reason=value.get('reason','')
        token=reason[len(PREFIX):] if isinstance(reason,str) and reason.startswith(PREFIX) else ''
        return token if value.get('desired_running') is False and len(token)==32 and all(c in '0123456789abcdef' for c in token) else None
    except (OSError,ValueError,AttributeError):return None


def known_runtime(root,owner):
    try:
        pid,run=owner['child_pid'],owner['run_id']
        row=read(folder(root)/f'runtime-{pid}.json')
        return row if row.get('pid')==pid and row.get('run_id')==run and 0<=time.time()-float(row['at'])<=180 else None
    except (OSError,ValueError,TypeError,KeyError,AttributeError):return None


def request_handoff(root,owner):
    row=known_runtime(root,owner)
    if row is None:return False
    try:
        save(folder(root)/'restart_request.json',dict(pid=row['pid'],run_id=row['run_id'],
            reason='EXPLICIT_CODE_UPGRADE',requested_at=time.time()))
        return True
    except OSError:return False


def prepare_handoff(root):
    existing=pending_token(root)
    if existing:return existing
    try:
        lease=StateLease(root,'flow-supervisor').acquire()
    except RuntimeError:pass
    else:
        lease.close();return None
    try:owner=read(folder(root)/'supervisor_status.json')
    except (OSError,ValueError):return None
    row=known_runtime(root,owner)
    if row is None or same_runtime_code(code_revision(),runtime_fingerprint(),
                                        row.get('code_revision'),row.get('runtime_fingerprint')):return None
    token=uuid4().hex
    set_intent(root,False,PREFIX+token)
    request_handoff(root,owner)
    return token


def wait_for_handoff(root,token,*,stop=None,pause=time.sleep):
    stop=stop or threading.Event()
    try:waiting=StateLease(root,'flow-handoff').acquire()
    except RuntimeError:return False
    try:
        while pending_token(root)==token and not stop.is_set() and not windows_shutting_down():
            try:
                with StateLease(root,'flow-supervisor'),StateLease(root,'dashboard'),StateLease(root,'broker-runtime'):
                    if pending_token(root)!=token:return False
                    set_intent(root,True,'EXPLICIT_HANDOFF_COMPLETED')
                    return True
            except RuntimeError:
                try:owner=read(folder(root)/'supervisor_status.json')
                except (OSError,ValueError):owner={}
                request_handoff(root,owner)
                pause(1)
        return False
    finally:waiting.close()
