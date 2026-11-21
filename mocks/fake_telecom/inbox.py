"""The simulated provider owns its receipts independently of the alerting database."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class ReceiptConflict(Exception):
    pass


class Inbox:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS receipts (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    channel TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    text TEXT NOT NULL,
                    gen_sent_at INTEGER,
                    received_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS receipts_recipient ON receipts(recipient);
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT NOT NULL
                );
                INSERT OR IGNORE INTO settings VALUES (1, 'ok');
            """)

    def mode(self):
        with self.connect() as db:
            return db.execute("SELECT mode FROM settings WHERE id=1").fetchone()[0]

    def set_mode(self, mode):
        with self.connect() as db:
            db.execute("UPDATE settings SET mode=? WHERE id=1", (mode,))

    def receive(self, key, channel, recipient, text, gen_sent_at, received_at):
        with self.connect() as db:
            # The mode switch and receipt each take effect in a single write transaction.
            db.execute("BEGIN IMMEDIATE")
            mode = db.execute("SELECT mode FROM settings WHERE id=1").fetchone()[0]
            if mode in ("down", "503"):
                return mode
            inserted = db.execute(
                """INSERT INTO receipts (idempotency_key, channel, recipient, text, gen_sent_at, received_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING""",
                (key, channel, recipient, text, gen_sent_at, received_at),
            )
            if inserted.rowcount == 0:
                previous = db.execute(
                    "SELECT channel, recipient, text FROM receipts WHERE idempotency_key=?", (key,)
                ).fetchone()
                if tuple(previous) != (channel, recipient, text):
                    raise ReceiptConflict
            return mode

    def messages(self, recipient=None, limit=None):
        where = "WHERE recipient=?" if recipient is not None else ""
        params = [recipient] if recipient is not None else []
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT idempotency_key, channel, recipient AS "to", text, gen_sent_at, received_at
                    FROM receipts {where} ORDER BY rowid DESC LIMIT ?""",
                [*params, limit if limit is not None else -1],
            )
            return [dict(row) for row in rows]

    def numbers(self):
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT recipient FROM receipts ORDER BY recipient")]
