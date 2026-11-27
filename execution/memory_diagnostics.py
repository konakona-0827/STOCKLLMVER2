"""Small, non-financial stage samples; no replies, credentials or model prose."""
import gc
import json
import os
from pathlib import Path
import sys
import threading
import time

_lock=threading.Lock()
_last={}


def checkpoint(root,stage):
    try:
        from operations.flow_resources import sample_memory
        from .com_lifetime import counters
        row=sample_memory(os.getpid())
        if row is None:return
        with _lock:
            key=(str(root),stage);previous=_last.get(key)
            current=row['private_commit_bytes'];_last[key]=current
            row.update(at=time.time(),pid=os.getpid(),thread_id=threading.get_ident(),stage=stage,
                private_delta_since_same_stage=None if previous is None else current-previous,
                python_allocated_blocks=sys.getallocatedblocks(),gc_counts=gc.get_count(),com=counters())
            folder=Path(root)/'data/health/flow_recovery';folder.mkdir(parents=True,exist_ok=True)
            with (folder/'memory_stages.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(row)+'\n')
            from opportunity_flow.common import save
            save(folder/'memory_diagnostics.json',row)
    except Exception:
        pass  # Diagnostics must never make a financial operation retry.
