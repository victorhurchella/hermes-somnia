"""Principles: switchable thinking rules, each one a concrete mechanism (not prompt decoration).

Every principle has at most one line of prompt "lens" and at least one deterministic hook:

| Principle | Hook | Effect |
|---|---|---|
| pareto | ``pareto_split`` | Under budget pressure, the head = Somnia entries that account for 80% of re-confirmations; the tail becomes "move to vault / remove" suggestions and the first eviction candidates. |
| lindy | ``score_adjustments``, ``protected`` | A fact that keeps being re-confirmed over a long time gets a bonus and cannot be evicted or superseded by a younger fact with less evidence. |
| inversion | ``score_adjustments`` | Corrections of the agent and pitfalls ("never do X", "you got Y wrong") rank higher: they prevent repeated mistakes. |
| occam | ``validate_entry`` | A merge may not grow an entry unless it adds a fact; of two phrasings, the shorter wins. |
| feynman | ``validate_entry`` | An entry must stand alone: no leading "this/that/it/isso/aquele", no dangling references. |

Precedence when they disagree: an explicit owner instruction > lindy > pareto.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional

ALL = ("pareto", "lindy", "inversion", "occam", "feynman")
# inversion is opt-in: it showed no measurable gain in bench/run.py (see docs/bench.md)
DEFAULT = ["pareto", "lindy", "occam", "feynman"]

LABELS = {
    "pareto": "Pareto: keep the vital few",
    "lindy": "Lindy: what lasted, lasts",
    "inversion": "Inversion: remember mistakes first",
    "occam": "Occam: the shortest true phrasing",
    "feynman": "Feynman: every entry stands alone",
}

DESCRIPTIONS = {
    "pareto": "When memory is tight, the few entries that keep getting re-confirmed stay; the long tail is offered "
              "for the vault or removal.",
    "lindy": "A fact re-confirmed over a long time gets a bonus and is protected from younger facts with less evidence.",
    "inversion": "Corrections and pitfalls rank higher, because they stop the agent from repeating a mistake.",
    "occam": "Merges may not make an entry longer unless they add a fact; the shorter phrasing wins.",
    "feynman": "Entries must make sense on their own: no 'this', 'that project' or unexplained references.",
}

# One line each, appended to the extraction / consolidation system prompts only when enabled.
EXTRACT_LENS = {
    "inversion": "Pay special attention to corrections of the agent and pitfalls (\"never do X\", \"that was wrong\"): "
                 "extract them with kind \"correction\".",
    "feynman": "Every text must make sense to someone who never saw these fragments: name the project, tool or "
               "person; never start with \"this\", \"that\" or \"it\".",
    "pareto": "Prefer the few facts that will matter most often; skip trivia even when it is true.",
}
CONSOLIDATE_LENS = {
    "occam": "Prefer the shortest phrasing that keeps every fact; a merge must not be longer than its parts unless "
             "it adds a fact.",
    "feynman": "Each final text must stand alone, naming what it refers to.",
    "lindy": "Entries marked lindy have been confirmed for a long time: never supersede them unless the candidate "
             "explicitly contradicts them.",
    "pareto": "When room is needed, evict from the entries marked tail first.",
}

LINDY_MIN_AGE_DAYS = 30
LINDY_MIN_CONFIRMATIONS = 3
LINDY_BONUS_MAX = 0.05
INVERSION_BONUS = 0.06
PARETO_SHARE = 0.80
PARETO_PRESSURE = 0.85  # pareto only acts when a store is at least this full


def enabled(settings_principles: Optional[Iterable[str]]) -> set[str]:
    if settings_principles is None:
        return set(DEFAULT)
    return {p.strip().lower() for p in settings_principles if p and p.strip().lower() in ALL}


def lens(kind: str, active: set[str]) -> str:
    table = EXTRACT_LENS if kind == "extract" else CONSOLIDATE_LENS
    lines = [table[p] for p in ALL if p in active and p in table]
    return ("\n\nPrinciples in force:\n" + "\n".join(f"- {l}" for l in lines)) if lines else ""


# --- scoring hooks ---

def lindy_strength(age_days: float, confirmations: int) -> float:
    if age_days < LINDY_MIN_AGE_DAYS or confirmations < LINDY_MIN_CONFIRMATIONS:
        return 0.0
    return min(1.0, math.log1p(age_days / LINDY_MIN_AGE_DAYS) / math.log1p(6)) * \
        min(1.0, confirmations / (LINDY_MIN_CONFIRMATIONS * 3))


def score_adjustments(*, kind: str, first_seen_at: float, signal_count: int, now: float,
                      active: set[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    if "inversion" in active and kind == "correction":
        out["inversion"] = INVERSION_BONUS
    if "lindy" in active and first_seen_at:
        age = max(0.0, (now - first_seen_at) / 86_400.0)
        strength = lindy_strength(age, signal_count)
        if strength:
            out["lindy"] = round(LINDY_BONUS_MAX * strength, 4)
    return out


# --- pareto ---

@dataclass
class ParetoSplit:
    head: set  # entry indexes
    tail: set
    active: bool


def pareto_split(entries: list[str], owned_info: dict[int, dict], used: int, limit: int,
                 active: set[str]) -> ParetoSplit:
    """``owned_info``: index -> {confirmations, protected}. Human entries are never in the tail."""
    if "pareto" not in active or not limit or used / limit < PARETO_PRESSURE or len(owned_info) < 3:
        return ParetoSplit(set(owned_info), set(), False)
    ranked = sorted(owned_info.items(), key=lambda kv: -max(1, kv[1].get("confirmations", 1)))
    total = sum(max(1, info.get("confirmations", 1)) for _, info in ranked)
    head, acc = set(), 0
    for idx, info in ranked:
        if acc / total >= PARETO_SHARE and not info.get("protected"):
            continue
        head.add(idx)
        acc += max(1, info.get("confirmations", 1))
    tail = set(owned_info) - head
    return ParetoSplit(head, tail, True)


# --- entry validators (occam, feynman) ---

_DANGLING_RE = re.compile(
    r"^(this|that|it|these|those|they|isso|isto|esse|essa|este|esta|aquele|aquela|ele|ela|eles|elas|"
    r"o mesmo|a mesma|ditto|same)\b", re.I)
_VAGUE_REF_RE = re.compile(r"\b(the (above|previous|same) (one|thing|project)|o projeto acima|como antes|"
                           r"as discussed|como combinado)\b", re.I)


def validate_entry(text: str, candidate: str, priors: list[str], action: str, active: set[str]) -> Optional[str]:
    """Reason the final entry text violates an active principle, else None."""
    if "feynman" in active:
        if _DANGLING_RE.match(text.strip()):
            return "feynman: the entry starts with a dangling reference"
        if _VAGUE_REF_RE.search(text):
            return "feynman: the entry refers to something it does not name"
    if "occam" in active:
        if action == "added" and len(text) > len(candidate) * 1.15 + 10:
            return "occam: the entry is padded beyond the candidate"
        if action == "superseded" and len(text) > max([len(candidate)] + [len(p) for p in priors]) * 1.15 + 10:
            return "occam: the replacement is longer than both versions"
        if action == "merged" and priors and len(text) > (len(candidate) + sum(len(p) for p in priors)) * 0.95:
            return "occam: the merge does not compress its parts"
    return None
