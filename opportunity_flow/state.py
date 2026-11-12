"""Separate SQLite ledger with per-material-change claims, errors and usage."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .common import canonical, digest
from .review import PROMPT_VERSION, ROLE_TASKS, RULES


class Ledger:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(self.folder/"flow.sqlite3",timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reviews (key TEXT PRIMARY KEY, status TEXT NOT NULL,
                updated REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY, at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, at TEXT NOT NULL, role TEXT NOT NULL,
                status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feed_events (id INTEGER PRIMARY KEY, at TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def key(self, candidate, model):
        return digest([candidate["material_key"],model,PROMPT_VERSION,ROLE_TASKS,RULES])

    def lookup(self,key,now,p):
        row = self.db.execute("SELECT status,updated,data FROM reviews WHERE key=?",(key,)).fetchone()
        if not row:
            return None
        status, updated, data = row
        ttl = (p["review_max_age_seconds"] if status == "VALID" else
               p["model_timeout_seconds"]*2+60 if status == "IN_FLIGHT" else p["failed_review_retry_seconds"])
        if 0 <= now-updated < ttl:
            return dict(status=status,data=json.loads(data),updated=updated)
        return None

    def claim(self,key,now,p):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.lookup(key,now,p):
                self.db.rollback()
                return False
            self.db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?)",(key,"IN_FLIGHT",now,"{}"))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def finish(self,key,status,now,data):
        self.db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?)",(key,status,now,canonical(data)))
        self.db.commit()

    def call(self,at,role,status,data):
        self.db.execute("INSERT INTO calls(at,role,status,data) VALUES (?,?,?,?)",(at,role,status,canonical(data)))
        self.db.commit()

    def cycle(self,at,data):
        cur = self.db.execute("INSERT INTO cycles(at,data) VALUES (?,?)",(at,canonical(data)))
        self.db.commit()
        return cur.lastrowid

    def history(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM cycles ORDER BY id")]

    def feed(self,at,kind,data):
        self.db.execute("INSERT INTO feed_events(at,kind,data) VALUES (?,?,?)",(at,kind,canonical(data)))
        self.db.commit()
