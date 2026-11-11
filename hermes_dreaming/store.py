"""Short-term store: candidate memories, their evidence signals and phase hits.

Mirrors OpenClaw's short-term recall store (``ShortTermRecallEntry`` + phase-signal rows)
in one SQLite file under ``$HERMES_HOME/plugin-data/hermes-dreaming/state.db``. Bounded
like the original: 512 entries, 32 sessions, 16 days, 16 sources, 8 tags per entry.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

MAX_ENTRIES = 512
MAX_SESSIONS = 32
MAX_DAYS = 16
MAX_SOURCES = 16
HIT_CAP = 9999

_SCHEMA = """
create table if not exists entries (
  key text primary key,
  text text not null,
  target text not null default 'memory',
  origin text not null default 'agent',
  explicit integer not null default 0,
  first_seen_at real not null,
  last_seen_at real not null,
  signal_count integer not null default 0,
  total_score real not null default 0,
  sessions text not null default '[]',
  days text not null default '[]',
  sources text not null default '[]',
  concept_tags text not null default '[]',
  light_hits integer not null default 0,
  last_light_at real,
  rem_hits integer not null default 0,
  last_rem_at real,
  last_rem_considered_at real,
  promoted_at real,
  promoted_to text,
  deferred_reason text
);
create table if not exists kv (key text primary key, value text);
create table if not exists owned (
  entry_hash text primary key,
  target text not null,
  text text not null,
  key text,
  score real,
  created_at real not null
);
create table if not exists runs (
  id integer primary key autoincrement,
  started_at real not null,
  finished_at real,
  dry_run integer not null default 0,
  summary text
);
"""

_JSON_FIELDS = ("sessions", "days", "sources", "concept_tags")


@dataclass
class Entry:
    key: str
    text: str
    target: str = "memory"
    origin: str = "agent"  # owner | agent | untrusted | system
    explicit: bool = False
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    signal_count: int = 0
    total_score: float = 0.0
    sessions: list = field(default_factory=list)
    days: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    concept_tags: list = field(default_factory=list)
    light_hits: int = 0
    last_light_at: Optional[float] = None
    rem_hits: int = 0
    last_rem_at: Optional[float] = None
    last_rem_considered_at: Optional[float] = None
    promoted_at: Optional[float] = None
    promoted_to: Optional[str] = None
    deferred_reason: Optional[str] = None

    @property
    def avg_score(self) -> float:
        return max(0.0, min(1.0, self.total_score / self.signal_count)) if self.signal_count else 0.0


_ORIGIN_RANK = {"owner": 0, "agent": 1, "system": 2, "untrusted": 3}


def worst_origin(a: str, b: str) -> str:
    """Least-trusted class wins when evidence merges (OpenClaw provenance merge)."""
    return a if _ORIGIN_RANK.get(a, 3) >= _ORIGIN_RANK.get(b, 3) else b


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- kv ---
    def get(self, key: str, default=None):
        row = self.conn.execute("select value from kv where key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value) -> None:
        self.conn.execute("insert or replace into kv(key, value) values(?, ?)", (key, json.dumps(value)))

    # --- entries ---
    def _row_to_entry(self, row: sqlite3.Row) -> Entry:
        data = dict(row)
        for name in _JSON_FIELDS:
            data[name] = json.loads(data[name] or "[]")
        data["explicit"] = bool(data["explicit"])
        return Entry(**data)

    def entries(self) -> list[Entry]:
        return [self._row_to_entry(r) for r in self.conn.execute("select * from entries order by last_seen_at desc")]

    def entry(self, key: str) -> Optional[Entry]:
        row = self.conn.execute("select * from entries where key=?", (key,)).fetchone()
        return self._row_to_entry(row) if row else None

    def save(self, e: Entry) -> None:
        data = asdict(e)
        for name in _JSON_FIELDS:
            data[name] = json.dumps(data[name], ensure_ascii=False)
        data["explicit"] = int(bool(data["explicit"]))
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        self.conn.execute(f"insert or replace into entries({cols}) values({marks})", tuple(data.values()))

    def record_signal(self, e: Entry, *, score: float, session_id: str, day: str, source: str,
                      at: float, origin: str, explicit: bool = False) -> bool:
        """One observation of ``e`` in ``session_id`` on ``day``. Deduped per (session, day),
        like OpenClaw's ``dedupeByQueryPerDay``. Returns True when it counted."""
        marker = f"{session_id}@{day}"
        if marker in e.sources:
            return False
        e.sources = (e.sources + [marker])[-MAX_SOURCES:]
        e.signal_count += 1
        e.total_score += max(0.0, min(1.0, score))
        if session_id not in e.sessions:
            e.sessions = (e.sessions + [session_id])[-MAX_SESSIONS:]
        if day not in e.days:
            e.days = sorted(e.days + [day])[-MAX_DAYS:]
        e.first_seen_at = min(e.first_seen_at or at, at)
        e.last_seen_at = max(e.last_seen_at or 0.0, at)
        e.origin = worst_origin(e.origin, origin) if e.signal_count > 1 else origin
        e.explicit = e.explicit or explicit
        return True

    def bump_phase(self, keys: Iterable[str], phase: str, at: float) -> None:
        col_hits, col_at = ("light_hits", "last_light_at") if phase == "light" else ("rem_hits", "last_rem_at")
        for key in keys:
            self.conn.execute(
                f"update entries set {col_hits}=min({HIT_CAP}, {col_hits}+1), {col_at}=? where key=?", (at, key))

    def mark_rem_considered(self, keys: Iterable[str], at: float) -> None:
        for key in keys:
            self.conn.execute("update entries set last_rem_considered_at=? where key=?", (at, key))

    def prune(self) -> int:
        """Keep at most MAX_ENTRIES: drop the stalest unpromoted entries first."""
        count = self.conn.execute("select count(*) from entries").fetchone()[0]
        excess = count - MAX_ENTRIES
        if excess <= 0:
            return 0
        self.conn.execute(
            "delete from entries where key in (select key from entries order by (promoted_at is not null), "
            "last_seen_at asc limit ?)", (excess,))
        return excess

    # --- dreaming-owned memory entries (only these are ever evicted) ---
    def own(self, entry_hash: str, target: str, text: str, key: str | None, score: float | None) -> None:
        self.conn.execute(
            "insert or replace into owned(entry_hash, target, text, key, score, created_at) values(?,?,?,?,?,?)",
            (entry_hash, target, text, key, score, time.time()))

    def owned(self, target: str) -> dict[str, dict]:
        return {r["entry_hash"]: dict(r) for r in self.conn.execute("select * from owned where target=?", (target,))}

    def disown(self, entry_hash: str) -> None:
        self.conn.execute("delete from owned where entry_hash=?", (entry_hash,))

    # --- runs ---
    def start_run(self, dry_run: bool) -> int:
        cur = self.conn.execute("insert into runs(started_at, dry_run) values(?, ?)", (time.time(), int(dry_run)))
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.conn.execute("update runs set finished_at=?, summary=? where id=?",
                          (time.time(), json.dumps(summary, ensure_ascii=False), run_id))

    def last_runs(self, n: int = 5) -> list[dict]:
        rows = self.conn.execute("select * from runs order by id desc limit ?", (n,)).fetchall()
        return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in rows]

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
