"""Small, thread-safe progress observations; no scheduling or trading decisions."""
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from threading import RLock
from time import monotonic

from .common import TAIPEI

_creation_lock = RLock()


class ActivityLog:
    def __init__(self):
        self.lock = RLock()
        self.active = {}
        self.recent = []
        self.sequence = 0

    @contextmanager
    def track(self, kind, label):
        with self.lock:
            self.sequence += 1
            key = self.sequence
            row = dict(kind=kind, label=label, started_at=datetime.now(TAIPEI).isoformat(), clock=monotonic())
            self.active[key] = row
        error = None
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            with self.lock:
                self.active.pop(key, None)
                self.recent.append(dict(kind=kind, label=label, finished_at=datetime.now(TAIPEI).isoformat(),
                                        duration_seconds=round(monotonic()-row['clock'], 2), error=error))
                self.recent = self.recent[-8:]

    def snapshot(self):
        with self.lock:
            return dict(active=[dict(kind=r['kind'], label=r['label'], started_at=r['started_at'],
                                     elapsed_seconds=max(0, int(monotonic()-r['clock']))) for r in self.active.values()],
                        recent=[dict(r) for r in self.recent])


def activity_for(owner):
    with _creation_lock:
        if getattr(owner, 'activity', None) is None:
            owner.activity = ActivityLog()
        return owner.activity


def tracked(kind, label):
    def decorate(function):
        @wraps(function)
        def call(self, *args, **kwargs):
            from execution.timing import span
            with activity_for(self).track(kind, label),span(kind.lower()):
                return function(self, *args, **kwargs)
        return call
    return decorate
