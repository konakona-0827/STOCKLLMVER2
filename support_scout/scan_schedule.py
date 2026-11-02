"""Japan desk's explicit daily scan times, independent of browser/Codex."""
from datetime import timedelta
from .japan import JST

SCHEDULE_VERSION = 'jp-hourly-close-v1-20261102'
DAILY_SCAN_TIMES = ('09:00', '10:00', '11:00', '12:30', '13:30', '14:30', '15:15')
START_GRACE_SECONDS = 300


def daily_slots(now):
    local = now.astimezone(JST)
    return [local.replace(hour=int(t[:2]), minute=int(t[3:]), second=0, microsecond=0) for t in DAILY_SCAN_TIMES]


def due_slot(now):
    """Allow timer wakeup jitter up to five minutes; never backfill older runs."""
    local = now.astimezone(JST)
    return next((s for s in reversed(daily_slots(local))
                 if timedelta(0) <= local-s < timedelta(seconds=START_GRACE_SECONDS)), None)


def reservation_slot(now):
    """Manual half-hour guard retained, with a separate 15:15 closing slot."""
    local = now.astimezone(JST)
    minute = 15 if local.hour == 15 and local.minute >= 15 else local.minute//30*30
    return local.replace(minute=minute, second=0, microsecond=0)
