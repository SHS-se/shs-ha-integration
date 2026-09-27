"""Bounded diagnostic history and named query timing, isolated from HA storage."""
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time


class Diagnostics:
    def __init__(self, directory: Path):
        self.path = directory / "diagnostics.sqlite3"
        self.lock = threading.Lock()
        self.timings = {}
        with self.operation("initialize") as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported diagnostics database schema {version}")
            db.execute("CREATE TABLE IF NOT EXISTS resource_samples (sampled_at TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("PRAGMA user_version = 1")
            db.execute("SELECT count(*) FROM dbstat").fetchone()  # Required image capability.

    @contextmanager
    def operation(self, name):
        queued = time.perf_counter()
        with self.lock:
            started = time.perf_counter()
            error = False
            db = None
            try:
                db = sqlite3.connect(self.path)
                with db:
                    yield db
            except Exception:
                error = True
                raise
            finally:
                if db is not None:
                    db.close()
                timing = self.timings.setdefault(name, {"count": 0, "errors": 0, "samples": deque(maxlen=120), "queue_ms": 0})
                timing["count"] += 1
                timing["errors"] += int(error)
                timing["samples"].append((time.perf_counter() - started) * 1000)
                timing["queue_ms"] = (started - queued) * 1000

    def record(self, sampled_at, values):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with self.operation("record_resources") as db:
            db.execute("INSERT INTO resource_samples VALUES (?, ?)", (sampled_at, json.dumps(values)))
            db.execute("DELETE FROM resource_samples WHERE sampled_at < ?", (cutoff,))

    def snapshot(self):
        with self.operation("diagnostics_census") as db:
            tables = [{"name": name, "bytes": size, "pages": pages,
                       "rows": db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]}
                      for name, size, pages in db.execute("SELECT name, sum(pgsize), count(*) FROM dbstat WHERE name IN (SELECT name FROM sqlite_master WHERE type='table') GROUP BY name")]
            history = [{"sampled_at": date, **json.loads(body)} for date, body in
                       db.execute("SELECT sampled_at, payload FROM resource_samples ORDER BY sampled_at DESC LIMIT 240")][::-1]
            result = {"file_bytes": self.path.stat().st_size, "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
                      "page_size": db.execute("PRAGMA page_size").fetchone()[0],
                      "free_pages": db.execute("PRAGMA freelist_count").fetchone()[0],
                      "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0],
                      "tables": tables, "history": history,
                      "query_plan": [row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT payload FROM resource_samples WHERE sampled_at > ? ORDER BY sampled_at", ("",))],
                      "operations": [{"name": name, "count": value["count"], "errors": value["errors"],
                          "last_ms": value["samples"][-1], "max_recent_ms": max(value["samples"]), "queue_ms": value["queue_ms"]}
                          for name, value in self.timings.items() if value["samples"]]}
        return result
