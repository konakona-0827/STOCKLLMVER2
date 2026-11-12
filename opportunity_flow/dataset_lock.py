"""An OS lock survives neither a killed collector nor an abandoned marker file."""
from contextlib import contextmanager
import os
from pathlib import Path

from .common import read


@contextmanager
def dataset_lease(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    handle=(folder/'.dataset.lock').open('a+b')
    try:
        handle.seek(0,2)
        if not handle.tell():handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:raise ValueError('DATASET_UPDATE_IN_PROGRESS') from exc
        marker=folder/'.collecting'
        if marker.exists():
            try:known=read(marker).get('lock_version')==1
            except (ValueError,AttributeError):known=False
            if not known:raise ValueError('DATASET_UPDATE_IN_PROGRESS:LEGACY_MARKER_REQUIRES_REVIEW')
        yield
    finally:handle.close()
