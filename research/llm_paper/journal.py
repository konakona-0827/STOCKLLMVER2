"""Hash-chained append-only paper events and recoverable book checkpoints."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .common import ROOT,digest,stamp


class Journal:
    def __init__(self,path):
        self.path=Path(path).resolve()
        if not self.path.is_relative_to(ROOT/"research-runs"):
            raise ValueError("ISOLATED_PAPER_JOURNAL_REQUIRED")
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,at TEXT NOT NULL,
                  kind TEXT NOT NULL,payload TEXT NOT NULL,previous_sha256 TEXT NOT NULL,sha256 TEXT NOT NULL);
              CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
              CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
            """)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def append(self,kind,payload,at=None,event_id=None):
        return self.append_many([dict(kind=kind,payload=payload,at=at,event_id=event_id)])[0]

    def append_many(self,events):
        """A fill/decision and its recoverable state must commit together."""
        identifiers=[]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            previous=prior[0] if prior else "0"*64
            for event in events:
                kind,payload=event['kind'],event['payload']
                at=event.get('at') or stamp()
                # Windows clock ticks may be identical for two legitimate,
                # identical checkpoints. Only explicit claims are idempotent.
                ident=event.get('event_id') or uuid4().hex
                sha=digest(dict(id=ident,at=at,kind=kind,payload=payload,previous_sha256=previous))
                db.execute("INSERT INTO events(id,at,kind,payload,previous_sha256,sha256) VALUES(?,?,?,?,?,?)",
                    (ident,at,kind,json.dumps(payload,ensure_ascii=False,allow_nan=False),previous,sha))
                identifiers.append(ident);previous=sha
        return identifiers

    def latest(self,kind):
        with self.connect() as db:
            row=db.execute("SELECT payload FROM events WHERE kind=? ORDER BY seq DESC LIMIT 1",(kind,)).fetchone()
            return json.loads(row[0]) if row else None

    def contains(self,event_id):
        with self.connect() as db:return bool(db.execute("SELECT 1 FROM events WHERE id=?",(event_id,)).fetchone())

    def verify(self):
        previous="0"*64
        with self.connect() as db:
            rows=db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        for row in rows:
            if row["previous_sha256"]!=previous or row["sha256"]!=digest(dict(id=row["id"],at=row["at"],kind=row["kind"],payload=json.loads(row["payload"]),previous_sha256=previous)):
                raise ValueError("PAPER_JOURNAL_HASH_CHAIN_BROKEN")
            previous=row["sha256"]
        return len(rows)


def checkpoint(books):
    states={}
    for arm,book in books.items():
        state=book.state()
        # Detailed decisions, orders and fills have their own immutable events.
        # Running risk and daily NAV aggregates survive this bounded checkpoint.
        state["equity"]=state["equity"][-1:]
        state["decisions"]=[]
        states[arm]=state
    return states
