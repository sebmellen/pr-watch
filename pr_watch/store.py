"""Private durable state and process locks, shared by every provider."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .model import WatchError, utc


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get("PR_WATCH_STATE_DIR") or
                              Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "pr-watch").resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS watches
                (id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL,
                 state TEXT NOT NULL, created REAL NOT NULL)""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.directory / "watches.sqlite3", timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, request):
        watch_id = uuid.uuid4().hex
        result = dict(id=watch_id, state="waiting", terminal=False, created_at=utc(time.time()),
                      observed_at=None, request=request, last_error=None)
        with self.connection() as db:
            db.execute("INSERT INTO watches VALUES (?, ?, ?, ?, ?)",
                       (watch_id, json.dumps(request), json.dumps(result), "waiting", time.time()))
        return result

    def get(self, watch_id):
        if not re.fullmatch(r"[0-9a-f]{32}", watch_id):
            raise WatchError("Invalid watch ID")
        with self.connection() as db:
            row = db.execute("SELECT result FROM watches WHERE id=?", (watch_id,)).fetchone()
        if row is None:
            raise WatchError("Watch not found")
        return json.loads(row[0])

    def update(self, watch_id, observation):
        # Cancellation and terminal outcomes are immutable; a slow worker cannot overwrite them.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT result FROM watches WHERE id=?", (watch_id,)).fetchone()
            if row is None:
                raise WatchError("Watch not found")
            current = json.loads(row[0])
            if not current["terminal"]:
                current.update(observation)
                current["terminal"] = current["state"] != "waiting"
                current["updated_at"] = utc(time.time())
                db.execute("UPDATE watches SET result=?, state=? WHERE id=?",
                           (json.dumps(current), current["state"], watch_id))
        return current

    def list(self, active=False):
        with self.connection() as db:
            rows = db.execute("SELECT result FROM watches" + (" WHERE state='waiting'" if active else "") +
                              " ORDER BY created DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    @contextmanager
    def lock(self, watch_id):
        self.get(watch_id)  # Validate ID before using it as a filename.
        with (self.directory / (watch_id + ".lock")).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def status(self, watch_id):
        result = self.get(watch_id)
        with self.lock(watch_id) as acquired:
            result["worker_active"] = not acquired
        result["needs_resume"] = not result["terminal"] and not result["worker_active"]
        return result
