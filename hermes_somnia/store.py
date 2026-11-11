"""Short-term store: candidate memories, evidence, phase hits, reject list, change log, owned entries.

Mirrors OpenClaw's short-term recall store (``ShortTermRecallEntry`` + phase-signal rows) in one
SQLite file under ``$HERMES_HOME/plugin-data/hermes-somnia/state.db``, plus what Somnia adds: a
reject list, the per-night change log the desktop page reads, and confirmation tracking for the
memory entries Somnia wrote. Bounded like the original: 512 entries, 32 sessions, 16 days.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .textutil import normalize_claim, sha1_12

MAX_ENTRIES = 512
MAX_SESSIONS = 32
MAX_DAYS = 16
MAX_SOURCES = 16
MAX_EVIDENCE = 3
HIT_CAP = 9999

_SCHEMA = """
create table if not exists entries (key text primary key, text text not null);
create table if not exists kv (key text primary key, value text);
create table if not exists owned (
  entry_hash text primary key, target text not null, text text not null, key text, score real,
  created_at real not null, last_confirmed_at real, run_id integer
);
create table if not exists runs (
  id integer primary key autoincrement, started_at real not null, finished_at real,
  dry_run integer not null default 0, summary text
);
create table if not exists rejected (
  claim_hash text primary key, key text, text text not null, reason text, rejected_at real not null,
  recur integer not null default 0, last_recur_at real
);
create table if not exists changes (
  id integer primary key autoincrement, run_id integer not null, at real not null, key text,
  action text not null, target text not null, text text not null, replaced text not null default '[]',
  score real, components text not null default '{}', sessions text not null default '[]', reason text
);
create index if not exists changes_run on changes(run_id);
"""

_JSON_FIELDS = ("sessions", "days", "sources", "concept_tags", "evidence")
# column DDL for every Entry field except key/text (added on demand, so old stores upgrade in place)
_ENTRY_COLUMNS = {
    "target": "text", "kind": "text", "origin": "text", "explicit": "integer", "pinned": "integer",
    "first_seen_at": "real", "last_seen_at": "real", "signal_count": "integer", "total_score": "real",
    "sessions": "text", "days": "text", "sources": "text", "concept_tags": "text", "evidence": "text",
    "light_hits": "integer", "last_light_at": "real", "rem_hits": "integer", "last_rem_at": "real",
    "last_rem_considered_at": "real", "promoted_at": "real", "promoted_to": "text", "deferred_reason": "text",
    "reask": "integer",
}


@dataclass
class Entry:
    key: str
    text: str
    target: str = "memory"
    kind: str = "fact"  # fact | preference | rule | procedure
    origin: str = "agent"  # owner | agent | untrusted | system
    explicit: bool = False
    pinned: bool = False
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    signal_count: int = 0
    total_score: float = 0.0
    sessions: list = field(default_factory=list)
    days: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    concept_tags: list = field(default_factory=list)
    evidence: list = field(default_factory=list)  # [{source_session, day, quote}]
    light_hits: int = 0
    last_light_at: Optional[float] = None
    rem_hits: int = 0
    last_rem_at: Optional[float] = None
    last_rem_considered_at: Optional[float] = None
    promoted_at: Optional[float] = None
    promoted_to: Optional[str] = None
    deferred_reason: Optional[str] = None
    reask: bool = False

    @property
    def avg_score(self) -> float:
        return max(0.0, min(1.0, self.total_score / self.signal_count)) if self.signal_count else 0.0


_ORIGIN_RANK = {"owner": 0, "agent": 1, "system": 2, "untrusted": 3}


def worst_origin(a: str, b: str) -> str:
    """Least-trusted class wins when evidence merges (OpenClaw provenance merge)."""
    return a if _ORIGIN_RANK.get(a, 3) >= _ORIGIN_RANK.get(b, 3) else b


def claim_hash(text: str) -> str:
    return sha1_12(normalize_claim(text))


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        have = {r[1] for r in self.conn.execute("pragma table_info(entries)")}
        for col, ddl in _ENTRY_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"alter table entries add column {col} {ddl}")
        owned = {r[1] for r in self.conn.execute("pragma table_info(owned)")}
        for col, ddl in (("last_confirmed_at", "real"), ("run_id", "integer")):
            if col not in owned:
                self.conn.execute(f"alter table owned add column {col} {ddl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # --- kv ---
    def get(self, key: str, default=None):
        row = self.conn.execute("select value from kv where key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value) -> None:
        self.conn.execute("insert or replace into kv(key, value) values(?, ?)", (key, json.dumps(value)))

    # --- entries ---
    def _row_to_entry(self, row: sqlite3.Row) -> Entry:
        data = {k: row[k] for k in row.keys() if k in Entry.__dataclass_fields__}
        for name in _JSON_FIELDS:
            data[name] = json.loads(data.get(name) or "[]")
        for name in ("explicit", "pinned", "reask"):
            data[name] = bool(data.get(name))
        for name, default in (("target", "memory"), ("kind", "fact"), ("origin", "agent")):
            data[name] = data.get(name) or default
        for name in ("first_seen_at", "last_seen_at", "total_score"):
            data[name] = float(data.get(name) or 0.0)
        for name in ("signal_count", "light_hits", "rem_hits"):
            data[name] = int(data.get(name) or 0)
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
        for name in ("explicit", "pinned", "reask"):
            data[name] = int(bool(data[name]))
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        self.conn.execute(f"insert or replace into entries({cols}) values({marks})", tuple(data.values()))

    def delete(self, key: str) -> None:
        self.conn.execute("delete from entries where key=?", (key,))

    def record_signal(self, e: Entry, *, score: float, session_id: str, day: str, at: float, origin: str,
                      explicit: bool = False, quote: str = "") -> bool:
        """One observation of ``e`` in ``session_id`` on ``day``. Deduped per (session, day),
        like OpenClaw's ``dedupeByQueryPerDay``. Returns True when it counted."""
        if quote:
            ev = {"source_session": session_id, "day": day, "quote": quote}
            e.evidence = ([ev] + [x for x in e.evidence if x.get("quote") != quote])[:MAX_EVIDENCE]
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
                f"update entries set {col_hits}=min({HIT_CAP}, coalesce({col_hits},0)+1), {col_at}=? where key=?",
                (at, key))

    def mark_rem_considered(self, keys: Iterable[str], at: float) -> None:
        for key in keys:
            self.conn.execute("update entries set last_rem_considered_at=? where key=?", (at, key))

    def prune(self) -> int:
        """Keep at most MAX_ENTRIES: drop the stalest unpromoted, unpinned entries first."""
        count = self.conn.execute("select count(*) from entries").fetchone()[0]
        excess = count - MAX_ENTRIES
        if excess <= 0:
            return 0
        self.conn.execute(
            "delete from entries where key in (select key from entries order by (promoted_at is not null) + "
            "coalesce(pinned,0), last_seen_at asc limit ?)", (excess,))
        return excess

    # --- reject list ---
    def reject(self, text: str, key: Optional[str], reason: str, at: Optional[float] = None) -> None:
        self.conn.execute(
            "insert or replace into rejected(claim_hash, key, text, reason, rejected_at, recur, last_recur_at) "
            "values(?,?,?,?,?,0,null)", (claim_hash(text), key, text, reason, at or time.time()))

    def unreject(self, key_or_text: str) -> bool:
        cur = self.conn.execute("delete from rejected where key=? or claim_hash=? or claim_hash=?",
                                (key_or_text, key_or_text, claim_hash(key_or_text)))
        return cur.rowcount > 0

    def rejected(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("select * from rejected order by rejected_at desc")]

    def bump_rejected(self, claim: str, at: float) -> None:
        self.conn.execute("update rejected set recur=recur+1, last_recur_at=? where claim_hash=?", (at, claim))

    # --- owned memory entries (only these are ever evicted) ---
    def own(self, entry_hash: str, target: str, text: str, key: Optional[str], score: Optional[float],
            run_id: Optional[int] = None, at: Optional[float] = None) -> None:
        at = at or time.time()
        self.conn.execute(
            "insert or replace into owned(entry_hash, target, text, key, score, created_at, last_confirmed_at, run_id) "
            "values(?,?,?,?,?,?,?,?)", (entry_hash, target, text, key, score, at, at, run_id))

    def owned(self, target: Optional[str] = None) -> dict[str, dict]:
        q, args = ("select * from owned where target=?", (target,)) if target else ("select * from owned", ())
        return {r["entry_hash"]: dict(r) for r in self.conn.execute(q, args)}

    def confirm_owned(self, key: str, at: float) -> None:
        self.conn.execute("update owned set last_confirmed_at=max(coalesce(last_confirmed_at,0), ?) where key=?",
                          (at, key))

    def disown(self, entry_hash: str) -> None:
        self.conn.execute("delete from owned where entry_hash=?", (entry_hash,))

    # --- runs + changes ---
    def start_run(self, dry_run: bool) -> int:
        cur = self.conn.execute("insert into runs(started_at, dry_run) values(?, ?)", (time.time(), int(dry_run)))
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.conn.execute("update runs set finished_at=?, summary=? where id=?",
                          (time.time(), json.dumps(summary, ensure_ascii=False), run_id))

    def runs(self, n: int = 30, include_dry: bool = False) -> list[dict]:
        q = "select * from runs" + ("" if include_dry else " where dry_run=0") + " order by id desc limit ?"
        return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in self.conn.execute(q, (n,))]

    def last_runs(self, n: int = 5) -> list[dict]:
        return self.runs(n, include_dry=True)

    def log_change(self, run_id: int, at: float, *, key: Optional[str], action: str, target: str, text: str,
                   replaced: Optional[list] = None, score: Optional[float] = None, components: Optional[dict] = None,
                   sessions: Optional[list] = None, reason: Optional[str] = None) -> None:
        self.conn.execute(
            "insert into changes(run_id, at, key, action, target, text, replaced, score, components, sessions, reason) "
            "values(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, at, key, action, target, text, json.dumps(replaced or [], ensure_ascii=False), score,
             json.dumps(components or {}), json.dumps(sessions or []), reason))

    def changes(self, run_id: int) -> list[dict]:
        out = []
        for r in self.conn.execute("select * from changes where run_id=? order by id", (run_id,)):
            d = dict(r)
            for k in ("replaced", "components", "sessions"):
                d[k] = json.loads(d[k] or ("{}" if k == "components" else "[]"))
            out.append(d)
        return out

    def latest_change_run(self) -> Optional[int]:
        row = self.conn.execute("select c.run_id from changes c join runs r on r.id=c.run_id where r.dry_run=0 "
                                "order by c.run_id desc limit 1").fetchone()
        return int(row[0]) if row else None
