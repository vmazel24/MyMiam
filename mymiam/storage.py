import json
import os
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "mymiam.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, renfo_token TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS foods (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL,
                    source TEXT NOT NULL, nutrients TEXT NOT NULL, flags TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meals (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, day TEXT NOT NULL,
                    slot TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
                    items TEXT NOT NULL, created TEXT NOT NULL, request_id TEXT NOT NULL,
                    UNIQUE(user_id, request_id));
                CREATE INDEX IF NOT EXISTS meals_by_day ON meals(user_id, day);
                CREATE TABLE IF NOT EXISTS days (
                    user_id TEXT NOT NULL, day TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0,
                    goal TEXT, PRIMARY KEY(user_id, day));
                CREATE TABLE IF NOT EXISTS favorites (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                    items TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS weights (
                    user_id TEXT NOT NULL, day TEXT NOT NULL, weight REAL NOT NULL,
                    PRIMARY KEY(user_id, day));
                CREATE TABLE IF NOT EXISTS garmin_days (
                    user_id TEXT NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(user_id, day));
            """)
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def bind_owner(self, user_id):
        # SQLite serializes first-owner assignment, including simultaneous logins.
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('owner',?)", (json.dumps(user_id),))
            owner = json.loads(db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()[0])
        return owner == user_id
