"""SQLite persistence (datasets, jobs, event log, sandbox audit trail) + live event fan-out."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncIterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, filename TEXT NOT NULL, ext TEXT NOT NULL,
    size INTEGER NOT NULL, created REAL NOT NULL, sample INTEGER NOT NULL DEFAULT 0,
    profile TEXT, suggestions TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, dataset_id TEXT, question TEXT,
    status TEXT NOT NULL, created REAL NOT NULL, finished REAL, answer TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS jobs_dataset ON jobs(dataset_id, created);
CREATE TABLE IF NOT EXISTS events (
    job_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE TABLE IF NOT EXISTS sandboxes (
    id TEXT PRIMARY KEY, job_id TEXT, purpose TEXT, runtime TEXT, limits TEXT, started REAL,
    duration_ms INTEGER, exit_code INTEGER, ok INTEGER, timed_out INTEGER, oom INTEGER, flood INTEGER,
    containment TEXT, guard TEXT
);
CREATE INDEX IF NOT EXISTS sandboxes_started ON sandboxes(started);
"""

TERMINAL = {"job_finished"}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._seq: dict[str, int] = {}

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, args)

    # ---- datasets -----------------------------------------------------------
    def add_dataset(self, *, id: str, name: str, filename: str, ext: str, size: int, sample: bool) -> None:
        self._x("INSERT OR REPLACE INTO datasets (id,name,filename,ext,size,created,sample) VALUES (?,?,?,?,?,?,?)",
                (id, name, filename, ext, size, time.time(), int(sample)))

    def set_profile(self, dataset_id: str, profile: dict, suggestions: list[str]) -> None:
        self._x("UPDATE datasets SET profile=?, suggestions=? WHERE id=?",
                (json.dumps(profile), json.dumps(suggestions), dataset_id))

    def dataset(self, dataset_id: str) -> dict | None:
        rows = self._q("SELECT * FROM datasets WHERE id=?", (dataset_id,))
        return _ds(rows[0]) if rows else None

    def datasets(self) -> list[dict]:
        return [_ds(r) for r in self._q("SELECT * FROM datasets ORDER BY sample DESC, created DESC")]

    def delete_dataset(self, dataset_id: str) -> None:
        self._x("DELETE FROM datasets WHERE id=?", (dataset_id,))

    # ---- jobs ---------------------------------------------------------------
    def create_job(self, *, id: str, kind: str, dataset_id: str | None, question: str | None) -> None:
        self._x("INSERT INTO jobs (id,kind,dataset_id,question,status,created) VALUES (?,?,?,?,?,?)",
                (id, kind, dataset_id, question, "running", time.time()))
        self._seq[id] = 0

    def finish_job(self, job_id: str, status: str, answer: str | None = None, meta: dict | None = None) -> None:
        self._x("UPDATE jobs SET status=?, finished=?, answer=?, meta=? WHERE id=?",
                (status, time.time(), answer, json.dumps(meta or {}), job_id))

    def job(self, job_id: str) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _job(rows[0]) if rows else None

    def jobs_for_dataset(self, dataset_id: str, limit: int = 20) -> list[dict]:
        rows = self._q("SELECT * FROM jobs WHERE dataset_id=? AND kind='analysis' ORDER BY created DESC LIMIT ?",
                       (dataset_id, limit))
        return [_job(r) for r in reversed(rows)]

    def mark_interrupted(self) -> None:
        """Jobs that were running when the server stopped will never finish."""
        for row in self._q("SELECT id FROM jobs WHERE status='running'"):
            self._seq[row["id"]] = self._max_seq(row["id"])
            self.emit_sync(row["id"], "job_finished", {"status": "interrupted"})
        self._x("UPDATE jobs SET status='interrupted', finished=? WHERE status='running'", (time.time(),))

    # ---- events -------------------------------------------------------------
    def _max_seq(self, job_id: str) -> int:
        rows = self._q("SELECT COALESCE(MAX(seq),0) AS m FROM events WHERE job_id=?", (job_id,))
        return rows[0]["m"]

    def emit_sync(self, job_id: str, type_: str, data: dict[str, Any]) -> dict:
        seq = self._seq.get(job_id, self._max_seq(job_id)) + 1
        self._seq[job_id] = seq
        event = {"seq": seq, "ts": time.time(), "type": type_, "data": data}
        self._x("INSERT INTO events (job_id,seq,ts,type,data) VALUES (?,?,?,?,?)",
                (job_id, seq, event["ts"], type_, json.dumps(data, default=str)))
        for q in list(self._subs.get(job_id, [])):
            q.put_nowait(event)
        return event

    async def emit(self, job_id: str, type_: str, data: dict[str, Any] | None = None) -> dict:
        return self.emit_sync(job_id, type_, data or {})

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        rows = self._q("SELECT seq,ts,type,data FROM events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after))
        return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "data": json.loads(r["data"])} for r in rows]

    async def stream(self, job_id: str, after: int = 0) -> AsyncIterator[dict]:
        """Replay stored events after `after`, then follow live ones until the job ends."""
        q: asyncio.Queue = asyncio.Queue()
        self._subs[job_id].append(q)
        try:
            last = after
            for ev in self.events(job_id, after):
                last = ev["seq"]
                yield ev
                if ev["type"] in TERMINAL:
                    return
            job = self.job(job_id)
            if job and job["status"] != "running" and not q.qsize():
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                yield ev
                if ev["type"] in TERMINAL:
                    return
        finally:
            self._subs[job_id].remove(q)
            if not self._subs[job_id]:
                self._subs.pop(job_id, None)

    # ---- sandbox audit --------------------------------------------------------
    def record_sandbox(self, job_id: str, summary: dict, started: float, guard: list[dict] | None = None) -> None:
        self._x("INSERT OR REPLACE INTO sandboxes (id,job_id,purpose,runtime,limits,started,duration_ms,exit_code,"
                "ok,timed_out,oom,flood,containment,guard) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (summary["sandbox_id"], job_id, summary["purpose"], summary["runtime"], json.dumps(summary["limits"]),
                 started, summary["duration_ms"], summary["exit_code"], int(summary["ok"]),
                 int(summary["timed_out"]), int(summary["oom_killed"]), int(summary["output_flood"]),
                 json.dumps(summary["containment"]), json.dumps(guard or [])))

    def sandboxes(self, limit: int = 200) -> list[dict]:
        rows = self._q("SELECT s.*, j.kind AS job_kind, j.question AS question FROM sandboxes s "
                       "LEFT JOIN jobs j ON j.id = s.job_id ORDER BY s.started DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            for k in ("limits", "containment", "guard"):
                d[k] = json.loads(d[k] or "[]")
            for k in ("ok", "timed_out", "oom", "flood"):
                d[k] = bool(d[k])
            out.append(d)
        return out

    def stats(self) -> dict:
        total = self._q("SELECT COUNT(*) AS n, COALESCE(SUM(duration_ms),0) AS ms FROM sandboxes")[0]
        kinds: dict[str, int] = defaultdict(int)
        for r in self._q("SELECT containment FROM sandboxes WHERE containment != '[]'"):
            for c in json.loads(r["containment"]):
                kinds[c["kind"]] += 1
        jobs = self._q("SELECT COUNT(*) AS n FROM jobs WHERE kind='analysis'")[0]["n"]
        return {"sandboxes": total["n"], "sandbox_seconds": round(total["ms"] / 1000, 1),
                "containment_events": sum(kinds.values()), "by_kind": dict(kinds),
                "analyses": jobs, "live_sandboxes_now": 0, "secrets_exposed": 0}


def _ds(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["sample"] = bool(d["sample"])
    d["profile"] = json.loads(d["profile"]) if d["profile"] else None
    d["suggestions"] = json.loads(d["suggestions"]) if d["suggestions"] else []
    return d


def _job(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
    return d
