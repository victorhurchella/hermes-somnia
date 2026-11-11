"""Shared fixtures: a fake Hermes session store, a fake memory backend and a scripted LLM."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_somnia.sinks import DELIM  # noqa: E402


def make_sessions_db(path: Path, rows: list[tuple]) -> None:
    """rows: (session_id, source, role, content, timestamp, display_kind)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table sessions (id text primary key, source text, chat_type text);
        create table messages (id integer primary key autoincrement, session_id text, role text, content text,
                               timestamp real, display_kind text, _compressed_summary integer default 0);
    """)
    for sid, source, *_ in rows:
        conn.execute("insert or ignore into sessions(id, source) values(?, ?)", (sid, source))
    for sid, _source, role, content, ts, kind in rows:
        conn.execute("insert into messages(session_id, role, content, timestamp, display_kind) values(?,?,?,?,?)",
                     (sid, role, content, ts, kind))
    conn.commit()
    conn.close()


class FakeMemory:
    def __init__(self, tmp: Path, memory=None, user=None, limits=(2200, 1375)):
        self.dir = tmp / "memories"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data = {"memory": list(memory or []), "user": list(user or [])}
        self.limits = {"memory": limits[0], "user": limits[1]}
        self.staged: list = []
        self._flush()

    def _flush(self):
        for t, name in (("memory", "MEMORY.md"), ("user", "USER.md")):
            (self.dir / name).write_text(DELIM.join(self.data[t]), encoding="utf-8")

    def entries(self, target):
        return list(self.data[target])

    def limit(self, target):
        return self.limits[target]

    def apply(self, target, ops):
        working = list(self.data[target])
        for op in ops:
            if op["action"] == "remove":
                if op["old_text"] not in working:
                    return {"success": False, "error": "no match"}
                working.remove(op["old_text"])
            elif op["action"] == "add":
                working.append(op["content"])
        if len(DELIM.join(working)) > self.limits[target]:
            return {"success": False, "error": "over limit"}
        self.data[target] = working
        self._flush()
        return {"success": True}

    def stage(self, target, ops, summary):
        self.staged.append((target, ops, summary))
        return {"success": True, "pending_id": f"p{len(self.staged)}"}

    def files(self):
        return {"memory": self.dir / "MEMORY.md", "user": self.dir / "USER.md"}


class ScriptedLLM:
    """Routes by system prompt: extraction, consolidation or diary."""

    def __init__(self, extract=None, consolidate=None, diary="Soft rain on the build logs."):
        self.extract = extract or (lambda user: {"observations": []})
        self.consolidate = consolidate
        self.diary = diary
        self.calls: list = []

    def __call__(self, system, user, max_tokens):
        if system.startswith("You read fragments"):
            kind, out = "extract", self.extract(user)
        elif system.startswith("Choose how to incorporate"):
            kind = "consolidate"
            out = self.consolidate(json.loads(user)) if self.consolidate else {"operations": []}
        else:
            kind, out = "diary", self.diary
        self.calls.append((kind, user))
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out)
