"""Read new conversation material from Hermes' session store (``$HERMES_HOME/state.db``).

Port of OpenClaw's session ingestion firewalls (session-ingestion.ts, session-entry-render.ts):
only interactive sessions; only user text and the agent's final reply per turn (tool output never
enters); turns injected by the runtime (delegation results, process notices, compaction,
system notes) are dropped so dreaming never learns its own machinery; a cursor on
``messages.id`` makes every sweep incremental.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .textutil import clip

# Runtime-injected user-role rows (Hermes ``messages.display_kind``) that are not the owner talking.
_INJECTED_KINDS = {"async_delegation_complete", "process_complete", "auto_continue", "content_refused",
                   "model_switch"}
_INJECTED_PREFIXES = ("[IMPORTANT", "[ASYNC DELEGATION", "[CONTEXT COMPACTION", "[STILL IN PROGRESS",
                      "[SYSTEM", "[CRON", "[BACKGROUND", "<memory-context>", "<system-reminder>")
_OOB_RE = re.compile(r"^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*(.*?)\s*(?:\[/OUT-OF-BAND USER MESSAGE\])?\s*$", re.S)
# Dreaming's own fingerprints (contamination guard, OpenClaw isContaminatedDreamingSnippet).
_CONTAMINATION = ("hermes-dreaming:", "Write a dream diary entry from these memory fragments",
                  "You are keeping a dream diary", "extract candidate long-term memories",
                  "## Light Sleep", "## REM Sleep", "## Deep Sleep")

USER_CHARS = 1500
REPLY_CHARS = 800
MIN_CHARS = 12


@dataclass
class Fragment:
    message_id: int
    session_id: str
    role: str  # user | assistant
    origin: str  # owner | agent | untrusted
    text: str
    at: float
    day: str


def is_contaminated(text: str) -> bool:
    return any(marker in text for marker in _CONTAMINATION)


def _clean_user_text(content: str, display_kind: Optional[str]) -> Optional[str]:
    if display_kind in _INJECTED_KINDS:
        return None
    text = (content or "").strip()
    match = _OOB_RE.match(text)
    if match:  # a mid-turn steer is the owner talking; keep the inner text
        text = match.group(1).strip()
    if text.startswith(_INJECTED_PREFIXES):
        return None
    return text or None


def day_of(ts: float, tz=None) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def read_fragments(db_path: Path, *, after_id: int, since_ts: float, sources: list[str],
                   max_messages: int, tz=None) -> tuple[list[Fragment], int]:
    """Fragments with ``messages.id > after_id`` from interactive sessions active since
    ``since_ts``. Returns (fragments, new cursor). The cursor stops at the last row read when
    ``max_messages`` is hit, so the rest is picked up by the next sweep."""
    if not Path(db_path).exists():
        return [], after_id
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("pragma table_info(messages)")}
        scols = {r[1] for r in conn.execute("pragma table_info(sessions)")}
        kind_col = "m.display_kind" if "display_kind" in cols else "null"
        summary_filter = "and coalesce(m._compressed_summary, 0) = 0" if "_compressed_summary" in cols else ""
        chat_type = "s.chat_type" if "chat_type" in scols else "null"
        marks = ",".join("?" for _ in sources) or "''"
        rows = conn.execute(
            f"""select m.id, m.session_id, m.role, m.content, m.timestamp, {kind_col} as kind,
                       s.source, {chat_type} as chat_type
                from messages m join sessions s on s.id = m.session_id
                where m.id > ? and m.timestamp >= ? and m.role in ('user', 'assistant')
                  and s.source in ({marks}) {summary_filter}
                order by m.id asc""",
            (after_id, since_ts, *sources)).fetchall()
    finally:
        conn.close()

    fragments: list[Fragment] = []
    cursor = after_id
    pending_reply: dict[str, sqlite3.Row] = {}  # last assistant text per session, flushed at next user turn

    def flush(session_id: str) -> None:
        row = pending_reply.pop(session_id, None)
        if row is None:
            return
        text = (row["content"] or "").strip()
        if len(text) >= MIN_CHARS and not is_contaminated(text):
            fragments.append(Fragment(row["id"], session_id, "assistant", "agent", clip(text, REPLY_CHARS),
                                      row["timestamp"], day_of(row["timestamp"], tz)))

    for row in rows:
        if len(fragments) + len(pending_reply) >= max_messages:
            break
        cursor = row["id"]
        sid = row["session_id"]
        if row["role"] == "assistant":
            if (row["content"] or "").strip():
                pending_reply[sid] = row
            continue
        text = _clean_user_text(row["content"], row["kind"])
        if text is None:
            continue
        flush(sid)
        if len(text) < MIN_CHARS or is_contaminated(text):
            continue
        group = (row["chat_type"] or "").lower() in ("group", "channel", "supergroup", "thread")
        fragments.append(Fragment(row["id"], sid, "user", "untrusted" if group else "owner",
                                  clip(text, USER_CHARS), row["timestamp"], day_of(row["timestamp"], tz)))
    else:
        cursor = rows[-1]["id"] if rows else after_id
    for sid in list(pending_reply):
        flush(sid)
    fragments.sort(key=lambda f: f.message_id)
    return fragments, cursor


def pack_chunks(fragments: list[Fragment], chunk_chars: int) -> list[list[Fragment]]:
    """Chunks under ``chunk_chars`` that keep each session's fragments contiguous and in order,
    packing several small sessions into one chunk (one extraction call each)."""
    by_session: dict[str, list[Fragment]] = {}
    for f in fragments:
        by_session.setdefault(f.session_id, []).append(f)
    chunks: list[list[Fragment]] = []
    current: list[Fragment] = []
    size = 0
    for frs in by_session.values():
        for f in frs:
            cost = len(f.text) + 24
            if current and size + cost > chunk_chars:
                chunks.append(current)
                current, size = [], 0
            current.append(f)
            size += cost
    if current:
        chunks.append(current)
    return chunks


def now() -> float:
    return time.time()
