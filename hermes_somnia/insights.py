"""Read models for the desktop page, the CLI and the morning report: nights, alarm, budgets, candidates."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from . import principles as P
from .config import Settings
from .scoring import WEIGHTS, score_entry
from .sinks import POINTER_PREFIX, DELIM
from .store import Entry, Store
from .textutil import sha1_12

DAY_S = 86_400.0


def r3(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 3) if x == x and abs(x) != float("inf") else 0.0
    if isinstance(x, dict):
        return {k: r3(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [r3(v) for v in x]
    return x


def rank_kw(settings: Settings) -> dict:
    return dict(half_life=settings.recency_half_life_days, min_score=settings.min_score,
                min_signals=settings.min_signals, min_sessions=settings.min_sessions,
                min_durability=settings.min_durability, max_age_days=settings.max_age_days,
                principles=P.enabled(settings.principles))


# --- nights + alarm ---

def nights(store: Store, n: int = 30, tz=None) -> list[dict]:
    by_day: dict[str, dict] = {}
    for run in reversed(store.runs(200)):
        s = run["summary"] or {}
        if not s:
            continue
        day = datetime.fromtimestamp(run["started_at"], tz).strftime("%Y-%m-%d")
        by_day[day] = {
            "date": day, "started_at": run["started_at"], "dry_run": bool(run["dry_run"]),
            "fragments": int(s.get("fragments", 0)), "observations": int(s.get("observations", 0)),
            "staged": int(s.get("staged", 0)), "themes": len(s.get("themes") or []),
            "truths": len(s.get("truths") or []), "promoted": int(s.get("promoted", 0)),
            "merged": int(s.get("merged", 0)), "deferred": int(s.get("deferred", 0)),
            "llm_calls": int(s.get("llm_calls", 0)), "quiet": bool(s.get("quiet")),
            "noop": bool(s.get("noop")), "errors": len(s.get("errors") or []),
        }
    return list(by_day.values())[-n:]


def alarm(store: Store, settings: Settings, budgets: list[dict], cron: Optional[dict], now: float,
          near: Optional[list] = None) -> Optional[dict]:
    """Loud failure modes. The OpenClaw post-mortem: a week of 0 promotions while the diary looked healthy."""
    ns = nights(store, 60)
    streak = 0
    for night in reversed(ns):
        if night["quiet"]:
            continue
        if night["noop"]:
            streak += 1
        else:
            break
    if streak >= settings.alarm_noop_nights:
        best = near[0] if near else None
        msg = f"{streak} nights in a row with new conversations but nothing remembered."
        if best:
            msg += f" Closest: {best['score']:.2f} — {best['missing'][0] if best['missing'] else 'blocked'}."
        return {"kind": "noop", "nights": streak, "message": msg, "best": best, "action": "settings"}
    errs = [x for x in ns[-3:] if x["errors"]]
    if len(errs) >= 2:
        return {"kind": "errors", "nights": len(errs), "message": f"The last {len(errs)} sweeps reported errors.",
                "best": None, "action": "review"}
    if cron and cron.get("installed"):
        last = ns[-1]["started_at"] if ns else None
        if last is None or now - last > 2.5 * DAY_S:
            days = "never" if last is None else f"{(now - last) / DAY_S:.0f} days ago"
            return {"kind": "stale_cron", "nights": 0, "action": "run",
                    "message": f"The nightly job is installed but the last sweep ran {days}. Is the Hermes gateway running?",
                    "best": None}
    for b in budgets:
        if b["limit"] and b["used"] / b["limit"] >= 0.97 and not any(e["owner"] == "somnia" for e in b["entries"]):
            return {"kind": "budget_full", "nights": 0, "action": "review", "best": None,
                    "message": f"{b['target'].upper()}.md is {b['used']}/{b['limit']} chars and every entry is yours: "
                               "Somnia cannot add anything there. Free some room or use the vault."}
    return None


# --- memory budgets ---

def budgets(backend, store: Store, settings: Settings, now: float) -> list[dict]:
    if backend is None:
        return []
    out = []
    for target in ("memory", "user"):
        if target not in settings.targets:
            continue
        try:
            entries = backend.entries(target)
            limit = backend.limit(target)
        except Exception:
            continue
        owned = store.owned(target)
        rows = []
        for i, text in enumerate(entries):
            row = owned.get(sha1_12(text))
            confirmed = row.get("last_confirmed_at") if row else None
            rows.append({
                "index": i, "text": text, "chars": len(text),
                "owner": "somnia" if row else "human",
                "key": row.get("key") if row else None,
                "created_at": row.get("created_at") if row else None,
                "last_confirmed_at": confirmed,
                "stale": bool(row) and (now - float(confirmed or row.get("created_at") or now)) > settings.stale_days * DAY_S,
                "pointer": text.startswith(POINTER_PREFIX),
            })
        out.append({"target": target, "used": len(DELIM.join(entries)), "limit": limit,
                    "goal": int(limit * (1 - settings.headroom)), "entries": rows})
    return out


# --- candidates ---

def session_titles(db_path, ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        cols = {r[1] for r in conn.execute("pragma table_info(sessions)")}
        title = "title" if "title" in cols else "null"
        started = "started_at" if "started_at" in cols else "null"
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(f"select id, {title}, source, {started} from sessions where id in ({marks})",
                            tuple(ids)).fetchall()
        conn.close()
    except Exception:
        return {}
    return {r[0]: {"title": r[1], "source": r[2], "started_at": r[3]} for r in rows}


def candidate_view(e: Entry, now: float, settings: Settings, titles: dict[str, dict], rejected: set[str]) -> dict:
    s = score_entry(e, now, **rank_kw(settings))
    if e.key in rejected:
        status = "rejected"
    elif e.promoted_to and e.promoted_to.startswith("staged:"):
        status = "staged"
    elif e.promoted_at:
        status = "promoted"
    elif e.kind == "procedure":
        status = "skill"
    else:
        status = "candidate"
    day_of = {}
    for marker in e.sources:
        sid, _, day = marker.rpartition("@")
        day_of.setdefault(sid, day)
    sessions = []
    for sid in reversed(e.sessions[-8:]):
        t = titles.get(sid, {})
        sessions.append({"id": sid, "title": t.get("title"), "source": t.get("source"), "day": day_of.get(sid, "")})
    return r3({
        "key": e.key, "text": e.text, "target": e.target, "kind": e.kind, "origin": e.origin,
        "explicit": e.explicit, "pinned": e.pinned, "score": s.score, "raw": s.raw, "min_score": settings.min_score,
        "components": s.components, "passes": {**s.passes, "not_rejected": e.key not in rejected},
        "missing": s.missing(settings.min_signals, settings.min_sessions, settings.min_durability),
        "signal_count": e.signal_count, "sessions": sessions, "days": e.days, "tags": e.concept_tags,
        "evidence": e.evidence[:3], "first_seen_at": e.first_seen_at, "last_seen_at": e.last_seen_at,
        "status": status, "promoted_to": e.promoted_to, "promoted_at": e.promoted_at,
        "deferred_reason": e.deferred_reason, "reask": e.reask,
    })


def stats(store: Store, settings: Settings, now: float) -> dict:
    hist = [0] * 20
    by_gate: dict[str, int] = {}
    total = 0
    for e in store.entries():
        if e.promoted_at:
            continue
        s = score_entry(e, now, **rank_kw(settings))
        total += 1
        hist[min(19, max(0, int(s.score / 0.05)))] += 1
        for gate, ok in s.passes.items():
            if not ok and gate != "not_promoted":
                by_gate[gate] = by_gate.get(gate, 0) + 1
    entries = store.entries()
    return {
        "histogram": [{"bucket": round(i * 0.05, 2), "count": c} for i, c in enumerate(hist)],
        "min_score": settings.min_score, "by_gate": by_gate,
        "promoted_total": sum(1 for e in entries if e.promoted_at),
        "rejected_total": len(store.rejected()), "candidates_total": total, "weights": WEIGHTS,
    }


def now_ts() -> float:
    return time.time()
