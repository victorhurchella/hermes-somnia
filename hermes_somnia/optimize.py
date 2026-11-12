"""Optimize: rewrite MEMORY.md / USER.md in fewer characters without losing what the agent does with them.

The model proposes; the code checks; the user approves entry by entry on the desktop page.

1. One call rewrites the store: each output entry names the original entries it replaces (``from``), so
   entries may be shortened, merged with others on the same subject, or kept as they are. Nothing may be
   dropped: an original the model leaves out simply stays.
2. Code guards, per rewritten entry: it must be shorter than what it replaces, keep every specific of its
   originals (numbers, paths, URLs, commands, code spans, @handles, identifiers, ACRONYMS) and add none.
3. A second call checks behaviour: would an agent that reads only the new text act the same way?
   Anything that fails a guard or the check is offered as "kept", with the reason.

Applying goes through Hermes' MemoryStore in one batch (``replace`` keeps the entry's position), with the
same backup + manifest as a nightly write, so ``hermes somnia undo`` restores it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .textutil import novel_specifics, sha1_12, sha256_hex

Complete = Callable[[str, str, int], str]
SEP = 2  # MemoryStore joins entries with a two-character delimiter
MIN_SAVING = 8  # smaller rewrites are not worth a review

_SPECIFIC_RES = (
    re.compile(r"`[^`]+`"),                                   # code spans
    re.compile(r"https?://\S+"),                              # URLs
    re.compile(r"[~/.]?[\w.-]*/[\w./<>*-]+"),                 # paths, branches (feat/v2), routes
    re.compile(r"@[\w.-]+"),                                  # handles
    re.compile(r"(?<![\w.])\d[\d.,:x%]*\b"),                  # numbers, ports, versions, ratios
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b"),                    # ACRONYMS, ENV_VARS
    re.compile(r"\b\w+(?:[_.:]\w+)+\b"),                      # identifiers: brain-sync.sh, sync:riot, snake_case
    re.compile(r"\b(?=[\w-]*\d)\w+(?:-\w+)+\b"),                # kebab with a digit: claude-opus-5-5
)
# Plain hyphenated words ("user-space", "read-only") are prose, not specifics; names like "cards-api" are
# left to the behaviour check, which compares the whole meaning.


def specifics(text: str) -> set[str]:
    out: set[str] = set()
    for rx in _SPECIFIC_RES:
        for m in rx.findall(text or ""):
            tok = m.strip(".,;:()[]\"'").strip("`").strip()
            if len(tok) >= 2:
                out.add(tok.lower())
    return out


def lost_specifics(after: str, befores: list[str]) -> list[str]:
    hay = (after or "").lower()
    lost = sorted({s for b in befores for s in specifics(b) if s not in hay})
    return lost


OPTIMIZE_SYSTEM = """You compress an AI agent's persistent memory. These entries are injected into EVERY conversation,
so each character costs tokens forever. Rewrite them to use as few characters as possible while the agent
behaves exactly the same after reading them.

Rules:
- Keep every instruction, preference, name, number, path, URL, command, identifier, port, branch and code span
  exactly as written (same spelling and case). Never add a fact, number or path that is not in the originals.
- Remove filler, hedges, repeated context, explanations the instruction already implies, and markdown noise.
- Do not rewrite an entry to save fewer than 8 characters: copy it unchanged.
- You may merge entries that are about the same subject into one. Never merge unrelated subjects.
- Each output entry must make sense alone (no "this", "that project") and keep the language of its originals.
- Keep the voice: a fact stays a fact ("User prefers X"), never becomes an order ("Do X"), and an order
  never becomes a fact. Who the entry is about stays explicit.
- One line per entry. An entry that is already tight stays unchanged (copy it exactly).
- Every original index must appear in exactly one output entry's "from".

Answer only JSON: {"entries": [{"from": [<original indexes>], "text": "<new entry>"}]}"""

VERIFY_SYSTEM = """You audit a compression of an AI agent's persistent memory. For each pair, the agent will read ONLY
the "after" text from now on. Say whether it would still behave the same in every situation the "before"
text covers: same instructions, same constraints, same facts, nothing weakened (e.g. "never" turned into
"avoid"), nothing dropped, nothing added.

Answer only JSON: {"checks": [{"id": <id>, "ok": true|false, "lost": "<what would change, empty if ok>"}]}"""


@dataclass
class Item:
    id: int
    sources: list[int]
    before: list[str]
    after: str
    ok: bool = True
    reason: str = ""

    @property
    def saved(self) -> int:
        return self.chars_before - len(self.after)

    @property
    def chars_before(self) -> int:
        return sum(len(b) for b in self.before) + SEP * (len(self.before) - 1)

    def as_dict(self) -> dict:
        return {"id": self.id, "from": self.sources, "before": self.before, "after": self.after,
                "saved": self.saved, "ok": self.ok, "reason": self.reason}


@dataclass
class Proposal:
    target: str
    limit: int
    entries: list[str]
    items: list[Item] = field(default_factory=list)
    llm_calls: int = 0
    error: Optional[str] = None

    @property
    def before_chars(self) -> int:
        return sum(len(e) for e in self.entries) + SEP * max(0, len(self.entries) - 1)

    def as_dict(self) -> dict:
        accepted = sum(i.saved for i in self.items if i.ok)
        return {"target": self.target, "limit": self.limit, "before_chars": self.before_chars,
                "after_chars": self.before_chars - accepted, "saved": accepted,
                "entries_before": len(self.entries), "items": [i.as_dict() for i in self.items],
                "unchanged": len(self.entries) - sum(len(i.sources) for i in self.items),
                "llm_calls": self.llm_calls, "error": self.error, "entries_hash": sha256_hex("\n".join(self.entries))}


def _json(text: str) -> dict:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the model's answer")
    return json.loads(text[start:end + 1])


def propose(target: str, entries: list[str], limit: int, complete: Complete) -> Proposal:
    prop = Proposal(target, limit, list(entries))
    if not entries:
        return prop
    numbered = "\n".join(f"[{i}] {e}" for i, e in enumerate(entries))
    try:
        raw = complete(OPTIMIZE_SYSTEM, f"Target file: {target.upper()}.md\nEntries:\n{numbered}", 4000)
        prop.llm_calls += 1
        plan = _json(raw).get("entries") or []
    except Exception as exc:
        prop.error = f"optimize call failed: {exc}"
        return prop

    used: set[int] = set()
    for row in plan:
        if not isinstance(row, dict):
            continue
        src = row.get("from")
        text = " ".join(str(row.get("text") or "").split())
        if not isinstance(src, list) or not src or not all(isinstance(i, int) for i in src):
            continue
        src = sorted(set(src))
        if any(i < 0 or i >= len(entries) or i in used for i in src):
            continue  # unknown or already-claimed index: leave those originals alone
        befores = [entries[i] for i in src]
        if len(src) == 1 and (text == befores[0] or 0 <= len(befores[0]) - len(text) < MIN_SAVING):
            used.update(src)
            continue  # unchanged
        used.update(src)
        item = Item(len(prop.items), src, befores, text)
        if not text:
            item.ok, item.reason = False, "empty rewrite"
        elif len(text) > 600:
            item.ok, item.reason = False, "rewrite longer than one entry may be"
        elif item.saved <= 0:
            item.ok, item.reason = False, "not shorter than the original"
        elif lost := lost_specifics(text, befores):
            item.ok, item.reason = False, "would drop: " + ", ".join(lost[:5])
        elif novel := novel_specifics(text, befores):
            item.ok, item.reason = False, "would add: " + ", ".join(novel[:5])
        prop.items.append(item)

    to_check = [i for i in prop.items if i.ok]
    if to_check:
        pairs = [{"id": i.id, "before": " | ".join(i.before), "after": i.after} for i in to_check]
        try:
            raw = complete(VERIFY_SYSTEM, json.dumps(pairs, ensure_ascii=False), 2000)
            prop.llm_calls += 1
            checks = {c.get("id"): c for c in (_json(raw).get("checks") or []) if isinstance(c, dict)}
        except Exception as exc:
            checks = {}
            prop.error = f"verification call failed ({exc}); nothing is pre-selected"
        for i in to_check:
            c = checks.get(i.id)
            if c is None:
                i.ok, i.reason = False, "not verified"
            elif not c.get("ok"):
                i.ok, i.reason = False, "would change behaviour: " + str(c.get("lost") or "")[:200]
    return prop


class StaleProposal(RuntimeError):
    pass


def apply(backend, target: str, proposal: dict, accept: list[int], run_dir: Path, store=None) -> dict:
    """Apply the accepted items of a proposal. Refuses if an accepted item's originals are no longer in
    memory exactly as proposed (the user or the agent edited them since)."""
    items = {i["id"]: i for i in proposal.get("items", [])}
    chosen = [items[a] for a in accept if a in items]
    if not chosen:
        return {"ok": True, "applied": 0, "saved": 0}
    current = backend.entries(target)
    for it in chosen:
        missing = [b for b in it["before"] if b not in current]
        if missing:
            raise StaleProposal("memory changed since the proposal; run Optimize again")
    ops: list[dict] = []
    for it in chosen:
        first, *rest = it["before"]
        ops.append({"action": "replace", "old_text": first, "content": it["after"]})
        ops += [{"action": "remove", "old_text": b} for b in rest]

    from .sinks import snapshot

    snapshot(backend, run_dir)
    out = backend.apply(target, ops)
    if not out.get("success"):
        raise RuntimeError(str(out.get("error") or out)[:300])
    if store is not None:
        owned = store.owned(target)
        for it in chosen:
            rows = [owned.get(sha1_12(b)) for b in it["before"]]
            for b in it["before"]:
                store.disown(sha1_12(b))
            row = next((r for r in rows if r), None)
            if row:  # a Somnia entry stays Somnia's (the user approved the new wording)
                store.own(sha1_12(it["after"]), target, it["after"], row.get("key"), row.get("score"),
                          row.get("run_id"), at=row.get("created_at"))
        store.commit()
    manifest = {"target": target, "ops": ops, "at": time.time(), "kind": "optimize",
                "after_sha256": {n: sha256_hex(p.read_text(encoding="utf-8")) for n, p in backend.files().items()
                                 if p.exists()}}
    (run_dir / f"ops-{target}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "applied": len(chosen), "saved": sum(int(i["saved"]) for i in chosen)}
