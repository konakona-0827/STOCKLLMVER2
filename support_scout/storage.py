"""Atomic report publication: a complete run directory, then a latest pointer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager

from .contracts import DataError, strict_json_loads, timestamp


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish_run(target: Path, files: dict[str, str]) -> None:
    """Refuse overwrite so a stale run cannot partly replace an existing report."""
    if target.exists():
        raise DataError("RUN_DIRECTORY_ALREADY_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=target.parent))
    try:
        for name, content in files.items():
            (stage / name).write_text(content, encoding="utf-8", newline="\n")
        os.replace(stage, target)
    finally:
        # Only remove temporary files created by this function, never user folders.
        if stage.exists():
            for child in stage.iterdir():
                child.unlink()
            stage.rmdir()


@contextmanager
def pointer_lock(path: Path):
    """OS file lock auto-releases on crash; bounded wait, shared by local writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise DataError("LATEST_POINTER_LOCK_TIMEOUT") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def publish_latest(path: Path, value: dict) -> bool:
    with pointer_lock(path.with_suffix(".lock")):
        if path.is_file():
            previous = strict_json_loads(path.read_text(encoding="utf-8"))
            if timestamp(previous["as_of"]) > timestamp(value["as_of"]):
                return False
        atomic_json(path, value)
        return True
