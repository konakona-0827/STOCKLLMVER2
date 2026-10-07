"""Fixed wall-clock slots; manual scans never move the auto deadline."""
import math
import threading

AUTO_INTERVAL_SECONDS = 1800
MANUAL_SCAN_COOLDOWN_SECONDS = 60


class Scheduler:
    def __init__(self, interval=AUTO_INTERVAL_SECONDS):
        if interval <= 0:
            raise ValueError('interval must be positive')
        self.interval = interval
        self.scan_lock = threading.Lock()
        self.next_auto = None
        self.last_manual = None
        self.cooldown_until = 0
        self.skipped = 0

    def start_auto(self, wall):
        if self.next_auto is None:
            self.next_auto = (math.floor(wall / self.interval) + 1) * self.interval

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
        self.next_auto += (math.floor((wall - self.next_auto) / self.interval) + 1) * self.interval
        if not self.scan_lock.acquire(blocking=False):
            self.skipped += 1
            return False
        return True

    def finish(self):
        self.scan_lock.release()
