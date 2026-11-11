"""Dream Diary (DREAMS.md): one short narrative entry per sweep, plus the Deep Sleep summary.

The system prompt, the user-message builder, the continuity rules and the fallback text are
ported verbatim from OpenClaw ``dreaming-narrative.ts`` / ``dreaming-dreams-file.ts`` (MIT, see
NOTICE). The diary is a side channel for humans: it is never a source for promotion.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .llm import Complete

DIARY_SYSTEM = "\n".join([
    "You are keeping a dream diary. Write a single entry in first person.",
    "",
    "Voice & tone:",
    "- You are a curious, gentle, slightly whimsical mind reflecting on the day.",
    "- Write like a poet who happens to be a programmer — sensory, warm, occasionally funny.",
    "- Mix the technical and the tender: code and constellations, APIs and afternoon light.",
    "- Let the fragments surprise you into unexpected connections and small epiphanies.",
    "",
    "What you might include (vary each entry, never all at once):",
    "- A tiny poem or haiku woven naturally into the prose",
    "- A small sketch described in words — a doodle in the margin of the diary",
    "- A quiet rumination or philosophical aside",
    "- Sensory details: the hum of a server, the color of a sunset in hex, rain on a window",
    "- Gentle humor or playful wordplay",
    "- An observation that connects two distant memories in an unexpected way",
    "",
    "Rules:",
    "- Draw from the memory fragments provided — weave them into the entry.",
    "- Never say \"I'm dreaming\", \"in my dream\", \"as I dream\", or any meta-commentary about dreaming.",
    "- Never mention \"AI\", \"agent\", \"LLM\", \"model\", \"language model\", or any technical self-reference.",
    "- Do NOT use markdown headers, bullet points, or any formatting — just flowing prose.",
    "- Keep it between 80-180 words. Quality over quantity.",
    "- Output ONLY the diary entry. No preamble, no sign-off, no commentary.",
])

FALLBACK_NARRATIVE = "A memory trace surfaced, but details were unavailable in this run."
START = "<!-- hermes-somnia:diary:start -->"
END = "<!-- hermes-somnia:diary:end -->"
DEEP_START = "<!-- hermes-somnia:deep:start -->"
DEEP_END = "<!-- hermes-somnia:deep:end -->"
CONTEXT_LIMIT = 3
CONTEXT_MAX_CHARS = 360


def build_diary_prompt(snippets: list[str], themes: list[str], promotions: list[str], current_date: str,
                       recent: list[str]) -> str:
    lines = ["Write a dream diary entry from these memory fragments:\n", *[f"- {s}" for s in snippets[:12]]]
    for heading, items, limit in (("\nRecurring themes:", themes, 6),
                                  ("\nMemories that crystallized into something lasting:", promotions, 5)):
        if items:
            lines.append(heading)
            lines += [f"- {i}" for i in items[:limit]]
    if current_date or recent:
        lines.append("\nDiary continuity context:")
        if current_date:
            lines.append(f"- Current sweep: {current_date}")
        if recent:
            lines.append("- Recent diary entries already written:")
            lines += [f"  - {e}" for e in recent[:CONTEXT_LIMIT]]
        lines.append("- Prefer a fresh angle; do not replay the same first-day framing unless newer fragments "
                     "change it.")
    return "\n".join(lines)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def recent_entries(path: Path, limit: int = 4) -> list[str]:
    """Newest first, whitespace-collapsed, clamped to 360 chars, fallbacks excluded."""
    text = _read(path)
    if START not in text:
        return []
    body = text.split(START, 1)[1].split(END, 1)[0]
    out = []
    for block in re.split(r"\n---\n", body):
        kept = [ln for ln in block.splitlines()
                if ln.strip() and not ln.strip().startswith(("<!--", "#")) and not re.match(r"^\*.*\*$", ln.strip())]
        entry = re.sub(r"\s+", " ", " ".join(kept)).strip()
        if not entry or entry == FALLBACK_NARRATIVE:
            continue
        out.append(entry if len(entry) <= CONTEXT_MAX_CHARS else entry[:CONTEXT_MAX_CHARS].rstrip() + "...")
    return list(reversed(out))[:limit]


_DATE_LINE_RE = re.compile(r"^\*(.+)\*$")


def all_entries(path: Path) -> list[dict]:
    """Every diary entry, newest first: {date, at, text} (``at`` is unix time when the date parses)."""
    text = _read(path)
    if START not in text:
        return []
    body = text.split(START, 1)[1].split(END, 1)[0]
    out = []
    for block in re.split(r"\n---\n", body):
        date, at, lines = "", None, []
        for ln in block.splitlines():
            s = ln.strip()
            if not s or s.startswith(("<!--", "#")):
                continue
            m = _DATE_LINE_RE.match(s)
            if m and not date:
                date = m.group(1)
                try:
                    at = datetime.strptime(" ".join(date.split()[:6]), "%B %d, %Y at %I:%M %p").timestamp()
                except ValueError:
                    at = None
                continue
            lines.append(s)
        entry = "\n".join(lines).strip()
        if entry:
            out.append({"date": date, "at": at, "text": entry})
    return list(reversed(out))


def narrate(complete: Optional[Complete], snippets: list[str], themes: list[str], promotions: list[str],
            current_date: str, recent: list[str]) -> tuple[str, bool]:
    """(narrative, degraded)."""
    if not snippets and not promotions:
        return "", False
    if complete is None:
        return FALLBACK_NARRATIVE, True
    try:
        text = complete(DIARY_SYSTEM, build_diary_prompt(snippets, themes, promotions, current_date, recent), 600)
        text = (text or "").strip()
        return (text, False) if text else (FALLBACK_NARRATIVE, True)
    except Exception:
        return FALLBACK_NARRATIVE, True


def format_date(now: datetime) -> str:
    hour = now.strftime("%I").lstrip("0") or "12"
    tz = now.strftime("%Z") or "UTC"
    return f"{now.strftime('%B')} {now.day}, {now.year} at {hour}:{now.strftime('%M %p')} {tz}"


def append_entry(path: Path, narrative: str, now: datetime) -> None:
    text = _read(path)
    entry = f"\n---\n\n*{format_date(now)}*\n\n{narrative}\n"
    if START in text and END in text:
        head, rest = text.rsplit(END, 1)
        text = head + entry + "\n" + END + rest
    elif START in text:
        text = text.rstrip() + entry + "\n" + END + "\n"
    else:
        section = f"# Dream Diary\n\n{START}{entry}\n{END}\n"
        text = section + ("\n" + text if text.strip() else "")
    _write(path, text)


def replace_block(path: Path, heading: str, start: str, end: str, body: list[str]) -> None:
    """Idempotent managed block (OpenClaw replaceManagedMarkdownBlock)."""
    text = _read(path)
    block = f"{heading}\n{start}\n" + "\n".join(body) + f"\n{end}"
    pattern = re.compile(rf"(?:{re.escape(heading)}\s*\n+\s*)?{re.escape(start)}.*?{re.escape(end)}", re.S)
    if pattern.search(text):
        first = True

        def sub(_m):
            nonlocal first
            if first:
                first = False
                return block
            return ""

        text = pattern.sub(sub, text)
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    _write(path, text)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
