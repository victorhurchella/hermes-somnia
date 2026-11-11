"""Deep-phase ranking math, ported from OpenClaw ``short-term-promotion.ts`` (MIT, see NOTICE).

Same weights, saturation points, half-lives and phase boost. Two signals are remapped because
Hermes has no ``memory_search`` recall log (docs/how-it-works.md, "What changed"):

* relevance  = mean durability the Light extractor gave each observation (OpenClaw: mean search score)
* diversity  = distinct sessions the fact showed up in (OpenClaw: distinct interactive search queries)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .store import Entry

DAY_S = 86_400.0
WEIGHTS = {"frequency": 0.24, "relevance": 0.30, "diversity": 0.15, "recency": 0.15,
           "consolidation": 0.10, "conceptual": 0.06}
LIGHT_BOOST_MAX = 0.06
REM_BOOST_MAX = 0.09
PHASE_HALF_LIFE_DAYS = 14


def clamp(x: float) -> float:
    return 0.0 if not math.isfinite(x) else max(0.0, min(1.0, x))


def recency_component(age_days: float, half_life_days: float) -> float:
    if not math.isfinite(age_days) or age_days < 0:
        return 1.0
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        return 1.0
    return math.exp(-(math.log(2) / half_life_days) * age_days)


def _day_ts(day: str) -> Optional[float]:
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def consolidation_component(days: list[str]) -> float:
    if not days:
        return 0.0
    ts = sorted(t for t in (_day_ts(d) for d in days) if t is not None)
    if len(ts) <= 1:
        return 0.2
    span_days = max(0.0, (ts[-1] - ts[0]) / DAY_S)
    spacing = clamp(math.log1p(len(ts) - 1) / math.log1p(4))
    span = clamp(span_days / 7)
    return clamp(0.55 * spacing + 0.45 * span)


def phase_boost(e: Entry, now: float) -> float:
    def contrib(hits: int, last: Optional[float], maximum: float) -> float:
        strength = clamp(math.log1p(max(0, hits)) / math.log1p(6))
        rec = clamp(recency_component(max(0.0, (now - last) / DAY_S), PHASE_HALF_LIFE_DAYS)) if last else 0.0
        return maximum * strength * rec

    return clamp(contrib(e.light_hits, e.last_light_at, LIGHT_BOOST_MAX)
                 + contrib(e.rem_hits, e.last_rem_at, REM_BOOST_MAX))


@dataclass
class Scored:
    entry: Entry
    score: float
    raw: float
    components: dict = field(default_factory=dict)
    passes: dict = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return all(self.passes.values())


def score_entry(e: Entry, now: float, *, half_life: float = 14, min_score: float = 0.65, min_signals: int = 2,
                min_sessions: int = 2, min_durability: float = 0.5, max_age_days: int = 30) -> Scored:
    n = e.signal_count
    sessions = len(e.sessions)
    age_days = max(0.0, (now - e.last_seen_at) / DAY_S) if e.last_seen_at else 0.0
    comp = {
        "frequency": clamp(math.log1p(n) / math.log1p(10)) if n > 0 else 0.0,
        "relevance": e.avg_score,
        "diversity": clamp(sessions / 5),
        "recency": clamp(recency_component(age_days, half_life)),
        "consolidation": consolidation_component(e.days),
        "conceptual": clamp(len(e.concept_tags) / 6),
    }
    boost = phase_boost(e, now)
    raw = sum(WEIGHTS[k] * v for k, v in comp.items()) + boost
    comp["phase_boost"] = boost
    passes = {
        "origin": e.origin in ("owner", "agent"),
        "signals": n >= min_signals or (e.explicit and n >= 1),
        # an explicit owner instruction ("remember...", "from now on...") stands in for recurrence
        "sessions": sessions >= min_sessions or (e.explicit and e.origin == "owner"),
        "durability": e.avg_score >= min_durability,
        "age": max_age_days < 0 or age_days <= max_age_days,
        "score": raw >= min_score,
        "not_promoted": e.promoted_at is None,
    }
    return Scored(e, clamp(raw), raw, comp, passes)


def rank(entries: list[Entry], now: float, limit: int, **kw) -> tuple[list[Scored], list[Scored]]:
    """(selected, all scored). Order: score desc, signals desc, key asc (OpenClaw :194-196)."""
    scored = [score_entry(e, now, **kw) for e in entries]
    eligible = sorted((s for s in scored if s.eligible),
                      key=lambda s: (-s.raw, -s.entry.signal_count, s.entry.key))
    return eligible[:max(0, limit)], scored
