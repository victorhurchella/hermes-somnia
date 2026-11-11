"""One dreaming sweep: Light -> REM -> Deep -> Dream Diary, under one timestamp and one lock."""

from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import diary as diary_mod
from .config import Settings, sessions_db_path, state_dir
from .ingest import pack_chunks, read_fragments
from .llm import Complete
from .phases import LightResult, light_block, light_extract, light_stage, rem, rem_block
from .scoring import Scored, rank
from .sinks import MemoryBackend, SinkResult, ensure_pointer, vault_dir, write_builtin, write_vault
from .store import Store

DAY_S = 86_400.0


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def sweep_lock(directory: Path):
    path = directory / "sweep.lock"
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("another dreaming sweep is running") from exc
        yield
    finally:
        fh.close()


def _tz(settings: Settings):
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
    staged: int = 0
    themes: list = field(default_factory=list)
    truths: list = field(default_factory=list)
    ranked: int = 0
    selected: list = field(default_factory=list)  # (key, target, score, text)
    near_misses: list = field(default_factory=list)  # (score, failed gates, sessions, text)
    sinks: list = field(default_factory=list)  # SinkResult
    vault_note: Optional[str] = None
    vault_lines: list = field(default_factory=list)
    diary: str = ""
    diary_degraded: bool = False
    errors: list = field(default_factory=list)
    cursor: tuple = (0, 0)

    @property
    def changed(self) -> bool:
        return any(s.staged_id or any(a[1] != "skipped" for a in s.applied) for s in self.sinks if not self.dry_run) \
            or bool(self.vault_note and not self.dry_run)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "fragments": self.light.fragments, "llm_calls": self.light.llm_calls,
            "llm_errors": self.light.llm_errors, "observations": self.light.observations,
            "new_entries": self.light.new_entries, "reinforced": self.light.reinforced,
            "staged": self.staged, "themes": [t[0] for t in self.themes],
            "truths": [t for t in self.truths], "ranked": self.ranked, "selected": self.selected,
            "near_misses": self.near_misses,
            "sinks": [{"target": s.target, "applied": s.applied, "staged_id": s.staged_id, "deferred": s.deferred,
                       "error": s.error, "plan": s.plan_source, "rejected_plan": s.rejected_plan}
                      for s in self.sinks],
            "vault_note": self.vault_note, "diary_degraded": self.diary_degraded, "errors": self.errors,
            "cursor": list(self.cursor),
        }


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
            lines += [f"  - {action}: {text}" for _, action, text in acted]
        skipped = [a for a in s.applied if a[1] == "skipped"]
        if skipped:
            lines.append(f"- Already in {s.target.upper()}.md: {len(skipped)}.")
        if s.deferred:
            lines.append(f"- Deferred ({s.target}): " + ", ".join(sorted({r for _, r in s.deferred})) +
                         f" ({len(s.deferred)}).")
        if s.rejected_plan:
            lines.append(f"- Model plan rejected, used append-only fallback: {s.rejected_plan}")
        if s.error:
            lines.append(f"- Error ({s.target}): {s.error}")
    if summary.vault_note:
        lines.append(f"- Vault inbox: {summary.vault_note}")
    if len(lines) == 1 and not summary.selected:
        lines.append("- No durable changes.")
    return lines


def run_sweep(settings: Settings, *, complete: Optional[Complete], backend: Optional[MemoryBackend],
              dry_run: bool = False, max_messages: Optional[int] = None, now: Optional[float] = None,
              base_dir: Optional[Path] = None, sessions_db: Optional[Path] = None) -> SweepSummary:
    base = base_dir or state_dir()
    base.mkdir(parents=True, exist_ok=True)
    now = now or time.time()
    tz = _tz(settings)
    summary = SweepSummary(started_at=now, dry_run=dry_run)
    with sweep_lock(base):
        db_path = base / "state.db"
        tmp_dir = None
        if dry_run:  # work on a throwaway copy of the short-term store
            tmp_dir = Path(tempfile.mkdtemp(prefix="dreaming-dry-"))
            if db_path.exists():
                shutil.copy2(db_path, tmp_dir / "state.db")
            db_path = tmp_dir / "state.db"
        store = Store(db_path)
        run_id = store.start_run(dry_run)
        try:
            _phases(store, settings, summary, complete=complete, backend=backend, now=now, tz=tz, base=base,
                    max_messages=max_messages or settings.max_messages,
                    sessions_db=sessions_db or sessions_db_path())
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


def _phases(store: Store, settings: Settings, summary: SweepSummary, *, complete, backend, now, tz, base: Path,
            max_messages: int, sessions_db: Path) -> None:
    day = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    report = base / "reports" / f"{day}.md"
    diary_path = base / "DREAMS.md"
    if settings.wants_vault:
        try:
            diary_path = vault_dir(settings) / "DREAMS.md"
        except Exception as exc:
            summary.errors.append(f"vault: {exc}")

    # --- Light: ingest new turns, extract candidates, stage the fresh ones ---
    cursor = int(store.get("cursor", 0) or 0)
    fragments, new_cursor = read_fragments(sessions_db, after_id=cursor, since_ts=now - settings.ingest_days * DAY_S,
                                           sources=settings.interactive_sources, max_messages=max_messages, tz=tz)
    summary.cursor = (cursor, new_cursor)
    chunks = pack_chunks(fragments, settings.extract_chunk_chars)
    light_extract(store, chunks, _memory_snapshot(backend), complete if settings.llm else None,
                  max_calls=settings.max_extract_calls, dedupe=settings.light_dedupe_similarity, result=summary.light)
    store.put("cursor", new_cursor)
    recent = diary_mod.recent_entries(diary_path, 4)
    staged = light_stage(store, now, lookback_days=settings.light_lookback_days, limit=settings.light_limit,
                         dedupe=settings.light_dedupe_similarity, recent_diary=recent)
    summary.staged = len(staged)

    # --- REM: themes + possible lasting truths (deterministic) ---
    rem_result = rem(store, now, lookback_days=settings.rem_lookback_days, limit=settings.rem_limit,
                     min_strength=settings.rem_min_pattern_strength)
    summary.themes = rem_result.themes
    summary.truths = [(e.text, round(c, 2)) for e, c in rem_result.truths]

    # --- Deep: rank, gate, write ---
    selected, scored = rank(store.entries(), now, settings.max_promotions,
                            half_life=settings.recency_half_life_days, min_score=settings.min_score,
                            min_signals=settings.min_signals, min_sessions=settings.min_sessions,
                            min_durability=settings.min_durability, max_age_days=settings.max_age_days)
    summary.ranked = len(scored)
    summary.selected = [(s.entry.key, s.entry.target, round(s.score, 3), s.entry.text) for s in selected]
    misses = sorted((s for s in scored if not s.eligible and s.entry.promoted_at is None), key=lambda s: -s.raw)
    summary.near_misses = [(round(s.raw, 3), [k for k, ok in s.passes.items() if not ok], len(s.entry.sessions),
                            s.entry.text) for s in misses[:5]]
    run_dir = base / "runs" / datetime.fromtimestamp(now, tz).strftime("%Y%m%dT%H%M%S")
    llm = complete if settings.llm else None
    promoted: dict[str, str] = {}

    if settings.wants_builtin and selected:
        if backend is None:
            summary.errors.append("builtin: Hermes memory store not available")
        for target in ("memory", "user"):
            cands = [s for s in selected if s.entry.target == target]
            if not cands or backend is None:
                continue
            if target not in settings.targets:
                continue
            res = write_builtin(backend, store, target, cands, llm, settings, dry_run=summary.dry_run, run_dir=run_dir)
            summary.sinks.append(res)
            if res.error:
                continue
            deferred = {k for k, _ in res.deferred}
            for key, action, _ in res.applied:
                if key in deferred:
                    continue
                if action == "skipped":
                    promoted[key] = f"builtin:{target}:present"
                else:
                    promoted[key] = f"staged:{res.staged_id}" if res.staged_id else f"builtin:{target}"
            for key, reason in res.deferred:
                e = store.entry(key)
                if e:
                    e.deferred_reason = reason
                    store.save(e)

    if settings.wants_vault and selected:
        try:
            note, lines = write_vault(settings, selected, rem_result, day, dry_run=summary.dry_run)
            summary.vault_note = str(note) if note else None
            summary.vault_lines = lines
            for s in selected:
                promoted.setdefault(s.entry.key, f"vault:{note}")
            if not summary.dry_run and settings.vault_pointer and backend is not None:
                err = ensure_pointer(backend, vault_dir(settings))
                if err:
                    summary.errors.append(f"vault pointer: {err}")
        except Exception as exc:
            summary.errors.append(f"vault: {exc}")

    for key, where in promoted.items():
        e = store.entry(key)
        if e:
            e.promoted_at, e.promoted_to, e.deferred_reason = now, where, None
            store.save(e)

    # --- reports + diary (never in a dry run) ---
    if summary.dry_run:
        return
    diary_mod.replace_block(report, "## Light Sleep", "<!-- hermes-dreaming:light:start -->",
                            "<!-- hermes-dreaming:light:end -->", light_block(staged))
    diary_mod.replace_block(report, "## REM Sleep", "<!-- hermes-dreaming:rem:start -->",
                            "<!-- hermes-dreaming:rem:end -->", rem_block(rem_result))
    diary_mod.replace_block(report, "## Deep Sleep", diary_mod.DEEP_START, diary_mod.DEEP_END, deep_block(summary))
    if settings.diary:
        promotions = [store.entry(k).text for k in promoted if store.entry(k)]  # type: ignore[union-attr]
        snippets = [e.text for e in staged] or [t for t, _ in summary.truths]
        narrative, degraded = diary_mod.narrate(llm, snippets, [t[0] for t in rem_result.themes], promotions,
                                                day, recent[:diary_mod.CONTEXT_LIMIT])
        if narrative:
            diary_mod.append_entry(diary_path, narrative, datetime.fromtimestamp(now, tz).astimezone())
            summary.diary, summary.diary_degraded = narrative, degraded
        diary_mod.replace_block(diary_path, "## Deep Sleep", diary_mod.DEEP_START, diary_mod.DEEP_END,
                                deep_block(summary))
    store.prune()


def render_summary(summary: SweepSummary, verbose: bool = False) -> str:
    d = summary
    head = "Dreaming (dry run)" if d.dry_run else "Dreaming"
    lines = [f"{head}: {d.light.fragments} new fragment(s), {d.light.observations} observation(s) "
             f"({d.light.new_entries} new, {d.light.reinforced} reinforced), {d.staged} staged, "
             f"{len(d.selected)} promoted of {d.ranked} candidate(s)."]
    lines += deep_block(d)[1:] if (d.selected or verbose) else []
    if verbose and d.themes:
        lines.append("- Themes: " + ", ".join(t[0] for t in d.themes))
    if verbose and d.truths:
        lines += [f"- Possible truth: {t} ({c})" for t, c in d.truths]
    if verbose and d.near_misses:
        lines.append("- Closest misses:")
        lines += [f"  - {sc:.2f} [{', '.join(f)}] ({n} session(s)) {text}" for sc, f, n, text in d.near_misses]
    if d.light.llm_errors:
        lines.append(f"- {d.light.llm_errors} extraction call(s) failed (heuristic fallback used).")
    lines += [f"- Error: {e}" for e in d.errors]
    if verbose and d.dry_run and d.vault_lines:
        lines += ["", "Vault note preview:", *d.vault_lines]
    return "\n".join(lines)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")
