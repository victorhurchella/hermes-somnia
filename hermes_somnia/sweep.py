"""One Somnia sweep: Light -> REM -> Deep -> Dream Diary, under one timestamp and one lock."""

from __future__ import annotations

import dataclasses
import fcntl
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import diary as diary_mod
from . import principles as P
from .config import Settings, sessions_db_path, state_dir
from .hermes_api import mask_secrets
from .ingest import fit_budget, pack_chunks, read_fragments
from .llm import Complete
from .phases import LightResult, RejectFilter, light_block, light_extract, light_stage, rem, rem_block
from .scoring import rank
from .sinks import MemoryBackend, ensure_pointer, vault_dir, write_builtin, write_vault
from .store import Store

DAY_S = 86_400.0


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def sweep_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    fh = open(directory / "sweep.lock", "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("another Somnia sweep is running") from exc
        yield
    finally:
        fh.close()


def tz_of(settings: Settings):
    if settings.timezone:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(settings.timezone)
        except Exception:
            return None
    return None


@dataclass
class SweepSummary:
    started_at: float
    dry_run: bool
    light: LightResult = field(default_factory=LightResult)
    quiet: bool = False  # no new conversation: Light made no LLM call
    staged: int = 0
    themes: list = field(default_factory=list)
    truths: list = field(default_factory=list)
    ranked: int = 0
    selected: list = field(default_factory=list)  # (key, target, score, text)
    near_misses: list = field(default_factory=list)  # (score, failed gates, sessions, text)
    skills: list = field(default_factory=list)  # procedure candidates ready to become skills: (key, text)
    sinks: list = field(default_factory=list)  # SinkResult
    vault_note: Optional[str] = None
    vault_lines: list = field(default_factory=list)
    diary: str = ""
    diary_degraded: bool = False
    errors: list = field(default_factory=list)
    cursor: tuple = (0, 0)
    llm_calls: int = 0
    principles: list = field(default_factory=list)
    changes: list = field(default_factory=list)  # dry runs only: what a real night would log (rpc Change shape)

    def counts(self) -> dict[str, int]:
        c = {"promoted": 0, "merged": 0, "deferred": 0, "evicted": 0, "staged": 0}
        for s in self.sinks:
            for _, action, *_ in s.applied:
                if action == "added":
                    c["promoted"] += 1
                elif action in ("merged", "superseded"):
                    c["merged"] += 1
            c["deferred"] += len(s.deferred)
            c["evicted"] += len(s.evicted)
            c["staged"] += 1 if s.staged_id else 0
        if self.vault_note and not self.dry_run:
            c["promoted"] += len(self.selected)
        return c

    @property
    def changed(self) -> bool:
        if self.dry_run:
            return False
        c = self.counts()
        return bool(c["promoted"] or c["merged"] or c["evicted"] or c["staged"])

    def as_dict(self) -> dict[str, Any]:
        c = self.counts()
        return {
            "dry_run": self.dry_run, "quiet": self.quiet,
            "fragments": self.light.fragments, "llm_calls": self.llm_calls,
            "llm_errors": self.light.llm_errors, "observations": self.light.observations,
            "new_entries": self.light.new_entries, "reinforced": self.light.reinforced,
            "confirmed": self.light.confirmed, "rejected_hits": self.light.rejected_hits,
            "ungrounded": self.light.ungrounded, "reasked": self.light.reasked,
            "staged": self.staged, "themes": [t[0] for t in self.themes],
            "truths": [list(t) for t in self.truths], "ranked": self.ranked, "selected": self.selected,
            "near_misses": self.near_misses, "skills": self.skills, "changes": self.changes, **c,
            "noop": not self.quiet and not self.changed and self.light.fragments > 0,
            "sinks": [{"target": s.target, "applied": s.applied, "evicted": s.evicted, "staged_id": s.staged_id,
                       "deferred": s.deferred, "error": s.error, "plan": s.plan_source,
                       "rejected_plan": s.rejected_plan} for s in self.sinks],
            "vault_note": self.vault_note, "diary_degraded": self.diary_degraded, "errors": self.errors,
            "cursor": list(self.cursor), "principles": self.principles,
        }


class CountingLLM:
    def __init__(self, complete: Optional[Complete]):
        self.complete = complete
        self.calls = 0

    def __call__(self, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        assert self.complete is not None
        return self.complete(system, user, max_tokens)


def _memory_snapshot(backend: Optional[MemoryBackend]) -> dict[str, list[str]]:
    if backend is None:
        return {}
    try:
        return {"memory": backend.entries("memory"), "user": backend.entries("user")}
    except Exception:
        return {}


def deep_block(summary: SweepSummary) -> list[str]:
    lines = [f"- Ranked {summary.ranked} candidate(s); {len(summary.selected)} passed every gate."]
    for s in summary.sinks:
        acted = [a for a in s.applied if a[1] != "skipped"]
        verb = "Staged for /memory pending" if s.staged_id else ("Would write" if summary.dry_run else "Wrote")
        if acted:
            lines.append(f"- {verb} {len(acted)} change(s) to {s.target.upper()}.md ({s.plan_source} plan).")
            lines += [f"  - {a[1]}: {a[2]}" for a in acted]
        if s.evicted:
            lines.append(f"- Evicted from {s.target.upper()}.md: {len(s.evicted)} Somnia entr{'y' if len(s.evicted) == 1 else 'ies'}.")
        skipped = [a for a in s.applied if a[1] == "skipped"]
        if skipped:
            lines.append(f"- Already in {s.target.upper()}.md: {len(skipped)}.")
        if s.deferred:
            lines.append(f"- Deferred ({s.target}): " + "; ".join(sorted({r for _, r in s.deferred})) +
                         f" ({len(s.deferred)}).")
        if s.rejected_plan:
            lines.append(f"- Model plan rejected, used append-only fallback: {s.rejected_plan}")
        if s.error:
            lines.append(f"- Error ({s.target}): {s.error}")
    if summary.vault_note:
        lines.append(f"- Vault inbox: {summary.vault_note}")
    if summary.skills:
        lines.append(f"- Skill ideas: {len(summary.skills)} recurring procedure(s) would fit better as skills.")
    if len(lines) == 1 and not summary.selected:
        lines.append("- No durable changes.")
    return lines


def run_sweep(settings: Settings, *, complete: Optional[Complete], backend: Optional[MemoryBackend],
              dry_run: bool = False, max_messages: Optional[int] = None, now: Optional[float] = None,
              base_dir: Optional[Path] = None, sessions_db: Optional[Path] = None,
              only_keys: Optional[list[str]] = None) -> SweepSummary:
    """``only_keys``: promote exactly these candidates now (desktop "Promote now"); skips Light/REM."""
    base = base_dir or state_dir()
    base.mkdir(parents=True, exist_ok=True)
    now = now or time.time()
    summary = SweepSummary(started_at=now, dry_run=dry_run)
    with sweep_lock(base):
        db_path = base / "state.db"
        tmp_dir = None
        if dry_run:  # work on a throwaway copy of the short-term store
            tmp_dir = Path(tempfile.mkdtemp(prefix="somnia-dry-"))
            if db_path.exists():
                shutil.copy2(db_path, tmp_dir / "state.db")
            db_path = tmp_dir / "state.db"
        store = Store(db_path)
        run_id = store.start_run(dry_run)
        try:
            _phases(store, settings, summary, complete=complete, backend=backend, now=now, base=base, run_id=run_id,
                    max_messages=max_messages or settings.max_messages,
                    sessions_db=sessions_db or sessions_db_path(), only_keys=only_keys)
            store.finish_run(run_id, summary.as_dict())
            store.commit()
        except Exception:
            store.rollback()
            raise
        finally:
            store.close()
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
    return summary


def _phases(store: Store, settings: Settings, summary: SweepSummary, *, complete, backend, now, base: Path,
            run_id: int, max_messages: int, sessions_db: Path, only_keys: Optional[list[str]]) -> None:
    tz = tz_of(settings)
    active = P.enabled(settings.principles)
    summary.principles = sorted(active)
    llm = CountingLLM(complete) if (complete is not None and settings.llm) else None
    day = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    report = base / "reports" / f"{day}.md"
    diary_path = base / "DREAMS.md"
    if settings.wants_vault:
        try:
            diary_path = vault_dir(settings) / "DREAMS.md"
        except Exception as exc:
            summary.errors.append(f"vault: {exc}")
    recent = diary_mod.recent_entries(diary_path, 4)
    staged, rem_result = [], None

    if only_keys is None:
        # --- Light: ingest new turns, extract candidates, stage the fresh ones ---
        cursor = int(store.get("cursor", 0) or 0)
        fragments, new_cursor = read_fragments(
            sessions_db, after_id=cursor, since_ts=now - settings.ingest_days * DAY_S,
            sources=settings.interactive_sources, max_messages=max_messages, tz=tz, until_ts=now)
        if llm is not None:  # never read past what tonight's extraction calls can cover
            fragments, new_cursor = fit_budget(fragments, new_cursor,
                                               int(settings.max_extract_calls * settings.extract_chunk_chars * 0.9))
        summary.cursor = (cursor, new_cursor)
        summary.quiet = not fragments  # wake gate: a quiet night costs zero extraction calls
        if settings.mask_secrets:
            fragments = [dataclasses.replace(f, text=mask_secrets(f.text)) for f in fragments]
        rejects = RejectFilter(store, now, settings.reask_days, settings.reask_min_recur)
        light_extract(store, pack_chunks(fragments, settings.extract_chunk_chars), _memory_snapshot(backend), llm,
                      max_calls=settings.max_extract_calls, dedupe=settings.light_dedupe_similarity,
                      result=summary.light, rejects=rejects, active=active)
        store.put("cursor", new_cursor)
        staged = light_stage(store, now, lookback_days=settings.light_lookback_days, limit=settings.light_limit,
                             dedupe=settings.light_dedupe_similarity, recent_diary=recent)
        summary.staged = len(staged)

        # --- REM: themes + possible lasting truths (deterministic) ---
        rem_result = rem(store, now, lookback_days=settings.rem_lookback_days, limit=settings.rem_limit,
                         min_strength=settings.rem_min_pattern_strength)
        summary.themes = rem_result.themes
        summary.truths = [(e.text, round(c, 2)) for e, c in rem_result.truths]

    # --- Deep: rank, gate, write ---
    rank_kw = dict(half_life=settings.recency_half_life_days, min_score=settings.min_score,
                   min_signals=settings.min_signals, min_sessions=settings.min_sessions,
                   min_durability=settings.min_durability, max_age_days=settings.max_age_days, principles=active)
    pool = store.entries()
    if only_keys is not None:
        pool = [e for e in pool if e.key in set(only_keys) and e.promoted_at is None]
        for e in pool:  # an explicit request from the owner
            e.pinned = True
        selected, scored = rank(pool, now, len(pool), **rank_kw)
        selected = [s for s in scored if s.passes["origin"] and s.passes["placement"] and s.passes["not_promoted"]]
    else:
        selected, scored = rank(pool, now, settings.max_promotions, **rank_kw)
    summary.ranked = len(scored)
    summary.selected = [(s.entry.key, s.entry.target, round(s.score, 3), s.entry.text) for s in selected]
    misses = sorted((s for s in scored if not s.eligible and s.entry.promoted_at is None
                     and s.entry.kind != "procedure"), key=lambda s: -s.raw)
    summary.near_misses = [(round(s.raw, 3), [k for k, ok in s.passes.items() if not ok], len(s.entry.sessions),
                            s.entry.text) for s in misses[:5]]
    summary.skills = [(s.entry.key, s.entry.text) for s in scored
                      if s.entry.kind == "procedure" and s.entry.promoted_at is None
                      and len(s.entry.sessions) >= settings.min_sessions]
    run_dir = base / "runs" / datetime.fromtimestamp(now, tz).strftime("%Y%m%dT%H%M%S")
    promoted: dict[str, str] = {}
    by_key = {s.entry.key: s for s in selected}

    def log(key, action, target, text, *, replaced=None, reason=None):
        s = by_key.get(key)
        if summary.dry_run:
            summary.changes.append({
                "run_at": now, "key": key, "action": action, "target": target, "text": text,
                "replaced": list(replaced or []), "score": round(s.score, 3) if s else 0.0,
                "components": {k: round(v, 4) for k, v in s.components.items()} if s else {},
                "sessions": [{"id": sid} for sid in reversed(s.entry.sessions[-8:])] if s else [],
                "reason": reason, "undoable": False})
            return
        store.log_change(run_id, now, key=key, action=action, target=target, text=text, replaced=replaced,
                         score=round(s.score, 3) if s else None,
                         components={k: round(v, 4) for k, v in s.components.items()} if s else None,
                         sessions=list(s.entry.sessions[-8:]) if s else None, reason=reason)

    if settings.wants_builtin and selected:
        if backend is None:
            summary.errors.append("builtin: Hermes memory store not available")
        for target in ("memory", "user"):
            cands = [s for s in selected if s.entry.target == target]
            if not cands or backend is None or target not in settings.targets:
                continue
            res = write_builtin(backend, store, target, cands, llm, settings, dry_run=summary.dry_run,
                                run_dir=run_dir, now=now, run_id=run_id)
            summary.sinks.append(res)
            if res.error:
                summary.errors.append(f"{target}: {res.error}")
                continue
            deferred = {k for k, _ in res.deferred}
            for key, action, text, replaced in res.applied:
                if key in deferred:
                    continue
                if action == "skipped":
                    promoted[key] = f"builtin:{target}:present"
                    log(key, "skipped", target, text or by_key[key].entry.text, reason="already in memory")
                else:
                    promoted[key] = f"staged:{res.staged_id}" if res.staged_id else f"builtin:{target}"
                    log(key, "staged" if res.staged_id else action, target, text, replaced=replaced)
            for text in res.evicted:
                log(None, "evicted", target, text, reason="pareto tail" if text in res.tail else "room for new facts")
            for key, reason in res.deferred:
                e = store.entry(key)
                if e:
                    e.deferred_reason = reason
                    store.save(e)
                log(key, "deferred", target, by_key[key].entry.text if key in by_key else "", reason=reason)

    if settings.wants_vault and selected:
        try:
            note, lines = write_vault(settings, selected, rem_result, day, dry_run=summary.dry_run)
            summary.vault_note = str(note) if note else None
            summary.vault_lines = lines
            for s in selected:
                if s.entry.key not in promoted:
                    promoted[s.entry.key] = f"vault:{note}"
                    log(s.entry.key, "vault", "vault", s.entry.text)
            if not summary.dry_run and settings.vault_pointer and backend is not None:
                err = ensure_pointer(backend, vault_dir(settings))
                if err:
                    summary.errors.append(f"vault pointer: {err}")
        except Exception as exc:
            summary.errors.append(f"vault: {exc}")

    for key, where in promoted.items():
        e = store.entry(key)
        if e:
            e.promoted_at, e.promoted_to, e.deferred_reason, e.pinned = now, where, None, False
            store.save(e)

    summary.llm_calls = llm.calls if llm else 0
    if summary.dry_run:
        return

    # --- reports + diary ---
    diary_mod.replace_block(report, "## Light Sleep", "<!-- hermes-somnia:light:start -->",
                            "<!-- hermes-somnia:light:end -->", light_block(staged))
    if rem_result is not None:
        diary_mod.replace_block(report, "## REM Sleep", "<!-- hermes-somnia:rem:start -->",
                                "<!-- hermes-somnia:rem:end -->", rem_block(rem_result))
    diary_mod.replace_block(report, "## Deep Sleep", diary_mod.DEEP_START, diary_mod.DEEP_END, deep_block(summary))
    if settings.diary and (staged or promoted):  # wake gate: no material, no diary call
        promotions = [e.text for e in (store.entry(k) for k in promoted) if e]
        snippets = [e.text for e in staged] or [t for t, _ in summary.truths]
        narrative, degraded = diary_mod.narrate(llm, snippets, [t[0] for t in (rem_result.themes if rem_result else [])],
                                                promotions, day, recent[:diary_mod.CONTEXT_LIMIT],
                                                settings.diary_language)
        if narrative:
            diary_mod.append_entry(diary_path, narrative, datetime.fromtimestamp(now, tz).astimezone())
            summary.diary, summary.diary_degraded = narrative, degraded
        diary_mod.replace_block(diary_path, "## Deep Sleep", diary_mod.DEEP_START, diary_mod.DEEP_END,
                                deep_block(summary))
    summary.llm_calls = llm.calls if llm else 0
    store.prune()


def morning_report(summary: SweepSummary, alarm: Optional[dict] = None) -> str:
    """The 3-5 lines the nightly cron delivers."""
    d = summary
    lines = []
    if d.dry_run:
        lines.append("Somnia (dry run)")
    c = d.counts()
    learned = [a for s in d.sinks for a in s.applied if a[1] in ("added", "merged", "superseded")]
    if d.vault_note and not d.dry_run:
        learned += [(k, "vault", t, []) for k, _, _, t in d.selected]
    if learned:
        lines.append(f"✦ Somnia remembered {len(learned)} thing{'s' if len(learned) != 1 else ''} last night:")
        lines += [f"  • {a[2]}" for a in learned[:5]]
    elif d.quiet:
        lines.append("☾ Somnia: a quiet night, no new conversations.")
    else:
        lines.append(f"☾ Somnia read {d.light.fragments} new turns; nothing was ready to remember yet.")
    if c["staged"]:
        lines.append("  Waiting for your approval: /memory pending")
    if c["evicted"]:
        lines.append(f"  Made room by retiring {c['evicted']} older Somnia entr{'y' if c['evicted'] == 1 else 'ies'}.")
    if d.skills:
        lines.append(f"  {len(d.skills)} recurring procedure(s) could become skills (see /somnia).")
    if alarm:
        lines.append(f"⚠ {alarm['message']}")
    for e in d.errors[:2]:
        lines.append(f"⚠ {e}")
    if learned or alarm:
        lines.append("  Undo: hermes somnia undo · Details: hermes somnia status")
    return "\n".join(lines)


def render_summary(summary: SweepSummary, verbose: bool = False) -> str:
    d = summary
    head = "Somnia (dry run)" if d.dry_run else "Somnia"
    lines = [f"{head}: {d.light.fragments} new fragment(s), {d.light.observations} observation(s) "
             f"({d.light.new_entries} new, {d.light.reinforced} reinforced), {d.staged} staged, "
             f"{len(d.selected)} promoted of {d.ranked} candidate(s), {d.llm_calls} LLM call(s)."]
    lines += deep_block(d)[1:] if (d.selected or verbose) else []
    if verbose and d.themes:
        lines.append("- Themes: " + ", ".join(t[0] for t in d.themes))
    if verbose and d.truths:
        lines += [f"- Possible truth: {t} ({c})" for t, c in d.truths]
    if verbose and d.near_misses:
        lines.append("- Closest misses:")
        lines += [f"  - {sc:.2f} [{', '.join(f)}] ({n} session(s)) {text}" for sc, f, n, text in d.near_misses]
    if verbose and d.skills:
        lines += [f"- Skill idea: {t}" for _, t in d.skills]
    if d.light.llm_errors:
        lines.append(f"- {d.light.llm_errors} extraction call(s) failed (heuristic fallback used).")
    if d.light.rejected_hits:
        lines.append(f"- {d.light.rejected_hits} observation(s) matched facts you told Somnia to forget.")
    lines += [f"- Error: {e}" for e in d.errors]
    if verbose and d.dry_run and d.vault_lines:
        lines += ["", "Vault note preview:", *d.vault_lines]
    return "\n".join(lines)
