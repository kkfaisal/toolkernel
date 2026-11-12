"""SQLite store: sessions, runs (what the model saw vs. what stayed in the sandbox) and metrics."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    client TEXT,
    created REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts REAL,
    tool TEXT,
    input TEXT,
    code TEXT,
    output TEXT,
    sandbox TEXT,
    state TEXT,
    duration_ms INTEGER,
    tool_calls TEXT,
    bytes_from_tools INTEGER,
    bytes_returned INTEGER,
    is_error INTEGER,
    restarted INTEGER,
    spill TEXT
);
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id, id);
CREATE INDEX IF NOT EXISTS runs_ts ON runs(ts);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def touch_session(self, sid: str, client: str | None) -> None:
        now = time.time()
        with self.lock:
            self.db.execute(
                "INSERT INTO sessions(id, client, created, last_seen) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, client=COALESCE(excluded.client, client)",
                (sid, client, now, now),
            )
            self.db.commit()

    def add_run(self, **run: Any) -> int:
        run = {**run, "tool_calls": json.dumps(run.get("tool_calls", [])), "input": json.dumps(run.get("input", {}))}
        cols = ",".join(run)
        with self.lock:
            cur = self.db.execute(f"INSERT INTO runs({cols}) VALUES ({','.join('?' * len(run))})", list(run.values()))
            self.db.commit()
            return int(cur.lastrowid or 0)

    @staticmethod
    def _row(r: sqlite3.Row, full: bool) -> dict[str, Any]:
        d = dict(r)
        d["tool_calls"] = json.loads(d["tool_calls"] or "[]")
        d["input"] = json.loads(d["input"] or "{}")
        if not full:
            d.pop("sandbox", None)
            d["output"] = (d["output"] or "")[:400]
            d["code"] = (d["code"] or "")[:400]
        return d

    def runs(self, session_id: str | None = None, limit: int = 50, before: int | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        if before:
            q += " AND id<?"
            args.append(before)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [self._row(r, False) for r in self.db.execute(q, args).fetchall()]

    def run(self, run_id: int) -> dict[str, Any] | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row(r, True) if r else None

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT s.*, COUNT(r.id) AS runs, COALESCE(SUM(r.bytes_from_tools),0) AS bytes_from_tools, "
                "COALESCE(SUM(r.bytes_returned),0) AS bytes_returned FROM sessions s "
                "LEFT JOIN runs r ON r.session_id = s.id GROUP BY s.id ORDER BY s.last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def totals(self, since: float = 0) -> dict[str, Any]:
        with self.lock:
            r = self.db.execute(
                "SELECT COUNT(*) AS runs, COALESCE(SUM(bytes_from_tools),0) AS bytes_from_tools, "
                "COALESCE(SUM(bytes_returned),0) AS bytes_returned, COALESCE(SUM(json_array_length(tool_calls)),0) AS tool_calls, "
                "COALESCE(SUM(is_error),0) AS errors, COUNT(DISTINCT session_id) AS sessions FROM runs WHERE ts>=?",
                (since,),
            ).fetchone()
        return dict(r)

    def daily(self, days: int = 14) -> list[dict[str, Any]]:
        since = time.time() - days * 86400
        with self.lock:
            rows = self.db.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS runs, "
                "SUM(bytes_from_tools) AS bytes_from_tools, SUM(bytes_returned) AS bytes_returned "
                "FROM runs WHERE ts>=? GROUP BY day ORDER BY day",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]
