"""Capture an owned model CLI without descendant-held stdout pipe waits."""
import asyncio
import os
from pathlib import Path
import subprocess
import signal
import tempfile
import time


async def capture(command,*,cwd,env,stdin=None,timeout=30,stop_event=None):
    # Files deliberately replace PIPEs: process.wait() can finish even if a
    # helper inherits stdout. Only this task's temporary files are cleaned.
    with tempfile.TemporaryDirectory(prefix='tw-model-io-',ignore_cleanup_errors=True) as folder:
        folder=Path(folder)
        (folder/'stdin').write_bytes(stdin or b'')
        with (folder/'stdin').open('rb') as incoming,(folder/'stdout').open('wb') as outgoing,(folder/'stderr').open('wb') as errors:
            options=dict(cwd=cwd,env=env,stdin=incoming,stdout=outgoing,stderr=errors)
            if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
            else:options['start_new_session']=True
            process=await asyncio.create_subprocess_exec(*command,**options)
            deadline=time.monotonic()+timeout
            try:
                while process.returncode is None:
                    if stop_event is not None and stop_event.is_set():raise RuntimeError('CODEX_REVIEW_STOPPED')
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('CODEX_MODEL_WALL_TIMEOUT')
                    try:await asyncio.wait_for(process.wait(),min(.2,remaining))
                    except asyncio.TimeoutError:pass
            except BaseException:
                if process.returncode is None:
                    if os.name=='nt':
                        killer=await asyncio.create_subprocess_exec('taskkill','/PID',str(process.pid),'/T','/F',
                            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                        try:await asyncio.wait_for(killer.wait(),10)
                        except asyncio.TimeoutError:killer.kill()
                    if process.returncode is None:
                        try:
                            if os.name=='nt':process.kill()
                            else:os.killpg(process.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                    try:await asyncio.wait_for(process.wait(),5)
                    except asyncio.TimeoutError:pass
                raise
        return process.returncode,(folder/'stdout').read_bytes(),(folder/'stderr').read_bytes()
