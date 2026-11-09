"""Fixed wall-clock slots; manual scans never move the auto deadline."""
import math
import threading
from datetime import date, datetime, time, timedelta

from config import TAIPEI
from market_calendar import twse_closed_reason

AUTO_INTERVAL_SECONDS = 3600
AUTO_PHASE_OFFSET_SECONDS = 300
MANUAL_SCAN_COOLDOWN_SECONDS = 60
TWSE_ODDLOT_OPEN_CHANGE = date(2026, 12, 7)


def twse_oddlot_first_match(day):
    return time(9, 10) if day < TWSE_ODDLOT_OPEN_CHANGE else time(9)


def twse_auto_scan_slot(wall):
    """Defer the opening slot until one minute after the first odd-lot match."""
    local = datetime.fromtimestamp(wall, TAIPEI)
    first_match = twse_oddlot_first_match(local.date())
    if time(9) <= local.time() < first_match:
        return (datetime.combine(local.date(), first_match, TAIPEI)
                + timedelta(minutes=1)).timestamp()
    return wall


def is_twse_oddlot_market_window(wall):
    """Intraday odd-lot order window in Taipei time; quotes still prove a live day."""
    local = datetime.fromtimestamp(wall, TAIPEI)
    minutes = local.hour * 60 + local.minute
    return (twse_closed_reason(local.date()) is None
            and 9 * 60 <= minutes < 13 * 60 + 30)


def is_health_check_window(wall):
    """Hourly broker health checks run on weekdays from 08:00 through 14:00."""
    local = datetime.fromtimestamp(wall, TAIPEI)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 8 * 60 <= minutes <= 14 * 60


def _next_weekday_at_eight(local):
    day = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local.hour >= 8:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.timestamp()


def health_check_due_or_next(wall):
    """Return now when checks are active, otherwise the next 08:00 slot."""
    local = datetime.fromtimestamp(wall, TAIPEI)
    if local.weekday() < 5 and local.hour < 8:
        return local.replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
    if is_health_check_window(wall):
        return wall
    return _next_weekday_at_eight(local)


def next_health_check_after(wall):
    """Return the next hourly slot, or next weekday 08:00 after 14:00."""
    local = datetime.fromtimestamp(wall, TAIPEI)
    candidate = (local.replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=1))
    if (local.weekday() < 5 and 8 <= candidate.hour <= 14
            and candidate.date() == local.date()):
        return candidate.timestamp()
    return _next_weekday_at_eight(local)


class Scheduler:
    def __init__(self, interval=AUTO_INTERVAL_SECONDS, allowed_slot=None,
                 phase_offset_seconds=0, adjust_slot=None):
        if interval <= 0:
            raise ValueError('interval must be positive')
        if not 0 <= phase_offset_seconds < interval:
            raise ValueError('phase_offset_seconds must be within the interval')
        self.interval = interval
        self.phase_offset_seconds = phase_offset_seconds
        self.allowed_slot = allowed_slot or (lambda _wall: True)
        self.adjust_slot = adjust_slot or (lambda wall: wall)
        self.scan_lock = threading.Lock()
        self.next_auto = None
        self.last_manual = None
        self.cooldown_until = 0
        self.skipped = 0

    def start_auto(self, wall):
        if self.next_auto is None:
            self.next_auto = self._next_after(wall)

    def _next_after(self, wall):
        slot = (math.floor((wall - self.phase_offset_seconds) / self.interval)
                * self.interval + self.phase_offset_seconds)
        while True:
            adjusted = self.adjust_slot(slot)
            if adjusted > wall and self.allowed_slot(adjusted):
                return adjusted
            slot += self.interval

    def stop_auto(self):
        self.next_auto = None

    def cooldown(self, monotonic):
        return max(0, math.ceil(self.cooldown_until - monotonic))

    def manual(self, wall, monotonic):
        if self.cooldown(monotonic) or not self.scan_lock.acquire(blocking=False):
            return False
        self.last_manual = wall
        self.cooldown_until = monotonic + MANUAL_SCAN_COOLDOWN_SECONDS
        return True

    def tick(self, wall):
        if self.next_auto is None or wall < self.next_auto:
            return False
        self.next_auto = self._next_after(wall)
        # If the process resumed after market close, the missed slot must not
        # start a late analysis or live execution run.
        if not self.allowed_slot(wall):
            return False
        if not self.scan_lock.acquire(blocking=False):
            self.skipped += 1
            return False
        return True

    def finish(self):
        self.scan_lock.release()
