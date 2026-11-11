"""``hermes somnia ...`` CLI (also used by the cron shim, the /somnia slash command and desktop jobs)."""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__, hermes_api
from . import principles as P
from .config import hermes_home, load_settings, state_dir
from .diary import recent_entries
from .llm import default_complete
from .scoring import score_entry
from .store import Store
from .sweep import AlreadyRunning, morning_report, render_summary, run_sweep
from .textutil import sha256_hex

JOB_NAME = "hermes-somnia"
SHIM_NAME = "hermes-somnia.py"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _backend():
    try:
        return hermes_api.HermesMemory()
    except Exception:
        return None


def _diary_path(settings) -> Path:
    if settings.wants_vault:
        try:
            from .sinks import vault_dir

            return vault_dir(settings) / "DREAMS.md"
        except Exception:
            pass
    return state_dir() / "DREAMS.md"


def _finish_job(args, *, ok: bool, summary: str = "", result=None, error: Optional[str] = None) -> None:
    if getattr(args, "job", None):
        from .rpc import write_job

        write_job(args.job, state="done" if ok else "failed", finished_at=time.time(), summary=summary,
                  result=result, error=error)


def _alarm_now(settings) -> Optional[dict]:
    try:
        from .insights import alarm, budgets

        store = Store(state_dir() / "state.db")
        try:
            b = budgets(_backend(), store, settings, time.time())
            return alarm(store, settings, b, {"installed": False}, time.time())
        finally:
            store.close()
    except Exception:
        return None


# --- commands ---

def cmd_run(args) -> int:
    settings = load_settings()
    if args.no_llm:
        settings.llm = False
    complete = default_complete() if settings.llm else None
    try:
        summary = run_sweep(settings, complete=complete, backend=_backend(), dry_run=args.dry_run,
                            max_messages=args.max_messages)
    except AlreadyRunning as exc:
        _finish_job(args, ok=False, error=str(exc))
        print(str(exc))
        return 0
    except Exception as exc:
        _finish_job(args, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    _finish_job(args, ok=True, summary=render_summary(summary, verbose=True), result=summary.as_dict())
    if args.json:
        print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.cron:
        alarm = _alarm_now(settings)
        mode = settings.notify
        if mode == "never" or (mode == "changes" and not summary.changed and not summary.errors and not alarm):
            return 0
        print(morning_report(summary, alarm))
        return 0
    print(render_summary(summary, verbose=args.verbose or args.dry_run))
    return 0


def cmd_promote(args) -> int:
    settings = load_settings()
    if args.target == "vault":
        settings.destinations = "vault"
    elif args.target in ("memory", "user"):
        settings.destinations = "builtin"
    store = Store(state_dir() / "state.db")
    try:
        for key in args.keys:
            e = store.entry(key)
            if e and args.target in ("memory", "user"):
                e.target = args.target
                store.save(e)
        store.commit()
    finally:
        store.close()
    try:
        summary = run_sweep(settings, complete=default_complete() if settings.llm else None, backend=_backend(),
                            only_keys=list(args.keys))
    except Exception as exc:
        _finish_job(args, ok=False, error=f"{type(exc).__name__}: {exc}")
        print(f"Promote failed: {exc}")
        return 1
    text = render_summary(summary, verbose=True)
    _finish_job(args, ok=not summary.errors, summary=text, result=summary.as_dict(),
                error="; ".join(summary.errors) or None)
    print(text)
    return 0 if not summary.errors else 1


def cmd_setup(args) -> int:
    settings = load_settings()
    scripts = hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PLUGIN_DIR / "scripts" / SHIM_NAME, scripts / SHIM_NAME)
    print(f"Installed cron script: {scripts / SHIM_NAME}")
    if args.no_cron:
        return 0
    job = hermes_api.find_cron_job(JOB_NAME)
    schedule = args.schedule or settings.schedule
    if job:
        print(f"Cron job already exists: {job.get('id')}.")
    else:
        ok, out = hermes_api.create_cron_job(JOB_NAME, schedule, SHIM_NAME)
        if not ok:
            print(f"Could not create the cron job ({out[-300:]}). Create it by hand:\n"
                  f"  hermes cron create \"{schedule}\" --name {JOB_NAME} --script {SHIM_NAME} --no-agent --deliver local")
            return 1
        print(f"Created the nightly job: {schedule} (script-only, no agent turn).")
    print("Next:\n"
          "  hermes somnia run --dry-run -v        # preview: nothing is written\n"
          f"  hermes somnia run --max-messages 3000 # optional one-time backfill of the last {settings.ingest_days} days\n"
          "  hermes somnia doctor                  # check everything is wired")
    return 0


def cmd_status(args) -> int:
    settings = load_settings()
    store = Store(state_dir() / "state.db")
    try:
        entries = store.entries()
        promoted = sum(1 for e in entries if e.promoted_at)
        print(f"Somnia {__version__}")
        print(f"- destinations: {settings.destinations} | schedule: {settings.schedule} | writes: {settings.destructive}")
        print(f"- principles: {', '.join(p for p in P.ALL if p in P.enabled(settings.principles)) or 'none'}")
        if settings.wants_vault:
            print(f"- vault: {settings.resolved_vault()} / {settings.vault_folder}")
        print(f"- diary: {_diary_path(settings)}")
        print(f"- short-term store: {len(entries)} candidate(s), {promoted} promoted, {len(store.rejected())} forgotten, "
              f"cursor at message {store.get('cursor', 0)}")
        print(f"- promotion policy: score>={settings.min_score}, signals>={settings.min_signals}, "
              f"sessions>={settings.min_sessions}, durability>={settings.min_durability}, "
              f"max {settings.max_promotions}/night")
        job = hermes_api.find_cron_job(JOB_NAME)
        print(f"- cron: {job.get('id')} next={job.get('next_run_at')}" if job
              else "- cron: not installed (run `hermes somnia setup`)")
        backend = _backend()
        if backend is not None and hasattr(backend, "pending"):
            pending = backend.pending()
            if pending:
                print(f"- waiting for approval: {len(pending)} (review with /memory pending)")
        alarm = _alarm_now(settings)
        if alarm:
            print(f"- ⚠ {alarm['message']}")
        for run in store.last_runs(3):
            s = run["summary"] or {}
            when = datetime.fromtimestamp(run["started_at"]).strftime("%Y-%m-%d %H:%M")
            tag = " (dry)" if run["dry_run"] else (" (quiet)" if s.get("quiet") else "")
            print(f"- run {when}{tag}: {s.get('fragments', 0)} turns, {s.get('staged', 0)} staged, "
                  f"{s.get('promoted', 0)} remembered, {s.get('merged', 0)} merged, {s.get('llm_calls', 0)} LLM calls"
                  + (f", errors: {len(s['errors'])}" if s.get("errors") else ""))
    finally:
        store.close()
    return 0


def cmd_explain(args) -> int:
    settings = load_settings()
    store = Store(state_dir() / "state.db")
    try:
        q = args.query.lower()
        matches = [e for e in store.entries() if q in e.text.lower() or q == e.key]
        if not matches:
            print(f'No candidate matched "{args.query}".')
            return 1
        now = time.time()
        rejected = {r["key"] for r in store.rejected()}
        for e in matches[: args.limit]:
            s = score_entry(e, now, half_life=settings.recency_half_life_days, min_score=settings.min_score,
                            min_signals=settings.min_signals, min_sessions=settings.min_sessions,
                            min_durability=settings.min_durability, max_age_days=settings.max_age_days,
                            principles=P.enabled(settings.principles))
            print(e.key + ("  [forgotten]" if e.key in rejected else "") + ("  [pinned]" if e.pinned else ""))
            print(f"  {e.text}")
            print(f"  kind={e.kind} target={e.target} origin={e.origin} explicit={e.explicit} signals={e.signal_count} "
                  f"sessions={len(e.sessions)} days={len(e.days)} promoted={e.promoted_to or '-'}")
            print("  score=%.3f  %s" % (s.raw, " ".join(f"{k}={v:.2f}" for k, v in s.components.items())))
            missing = s.missing(settings.min_signals, settings.min_sessions, settings.min_durability)
            print("  " + ("ready to promote" if s.eligible else "not yet: " + "; ".join(missing or ["already promoted"])))
            for ev in e.evidence[:2]:
                print(f"  evidence {ev.get('day')}: \"{ev.get('quote')}\"")
    finally:
        store.close()
    return 0


def cmd_forget(args) -> int:
    from .rpc import RpcError, forget

    try:
        out = forget({"key": args.key, "reason": "forgotten from the CLI"})
    except RpcError as exc:
        print(str(exc))
        return 1
    print("Forgotten" + (" and removed from memory." if out["removed_entry"] else "."))
    return 0


def cmd_unforget(args) -> int:
    from .rpc import unforget

    print("Restored." if unforget({"key": args.key})["ok"] else "It was not on the forget list.")
    return 0


def cmd_pin(args) -> int:
    from .rpc import RpcError, pin

    try:
        pin({"key": args.key, "pinned": not args.off})
    except RpcError as exc:
        print(str(exc))
        return 1
    print("Unpinned." if args.off else "Pinned: it will be promoted at the next sweep.")
    return 0


def cmd_diary(args) -> int:
    entries = recent_entries(_diary_path(load_settings()), args.n)
    if not entries:
        print("The dream diary is empty.")
    for e in entries:
        print(f"- {e}\n")
    return 0


def undo_last(force: bool = False) -> tuple[list[str], str]:
    manifests = sorted((state_dir() / "runs").glob("*/ops-*.json"))
    if not manifests:
        return [], "Nothing to undo."
    run_dir = manifests[-1].parent
    backend = _backend()
    files = backend.files() if backend is not None else {
        "memory": hermes_home() / "memories" / "MEMORY.md", "user": hermes_home() / "memories" / "USER.md"}
    restored, notes = [], []
    for manifest_path in sorted(run_dir.glob("ops-*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        current = files[manifest["target"]]
        backup = run_dir / current.name
        if not backup.exists():
            continue
        expected = manifest.get("after_sha256", {}).get(manifest["target"])
        if expected and current.exists() and sha256_hex(current.read_text(encoding="utf-8")) != expected and not force:
            notes.append(f"{current.name} changed after the dream; not restoring (use --force to override).")
            continue
        with open(current.with_name(current.name + ".lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            shutil.copy2(backup, current)
        restored.append(current.name)
        manifest_path.rename(manifest_path.with_suffix(".undone"))
    if restored:
        return restored, f"Restored {', '.join(restored)} from {run_dir.name}."
    return [], " ".join(notes) or "Nothing restored."


def cmd_undo(args) -> int:
    restored, message = undo_last(args.force)
    print(message)
    return 0 if restored else 1


def cmd_doctor(args) -> int:
    settings = load_settings()
    checks: list[tuple[bool, str]] = []
    checks.append((hermes_api.llm_available(), f"LLM access through Hermes ({hermes_api.describe_llm().get('model') or 'default model'})"))
    backend = _backend()
    checks.append((backend is not None, "MEMORY.md / USER.md readable"))
    if backend is not None:
        checks.append((getattr(backend, "native_store", True), "writes go through Hermes' own MemoryStore (lock + threat scan)"))
    from .config import sessions_db_path

    checks.append((sessions_db_path().exists(), f"session store found ({sessions_db_path()})"))
    job = hermes_api.find_cron_job(JOB_NAME)
    checks.append((job is not None, "nightly job installed" if job else "nightly job missing: run `hermes somnia setup`"))
    checks.append(((hermes_home() / "scripts" / SHIM_NAME).exists(), "cron script in $HERMES_HOME/scripts"))
    if settings.wants_vault:
        try:
            from .sinks import vault_dir

            vault_dir(settings)
            checks.append((True, f"vault inbox: {settings.resolved_vault()}/{settings.vault_folder}"))
        except Exception as exc:
            checks.append((False, f"vault: {exc}"))
    alarm = _alarm_now(settings)
    checks.append((alarm is None, alarm["message"] if alarm else "no alarms"))
    for ok, label in checks:
        print(("✓ " if ok else "✗ ") + label)
    return 0 if all(ok for ok, _ in checks) else 1


# --- argparse wiring ---

def build_parser(parser: Optional[argparse.ArgumentParser] = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(prog="hermes somnia", description="Somnia: offline memory consolidation.")
    sub = parser.add_subparsers(dest="somnia_cmd")
    p = sub.add_parser("run", help="run one sweep now (Light -> REM -> Deep -> diary)")
    p.add_argument("--dry-run", action="store_true", help="plan only: nothing is written")
    p.add_argument("--no-llm", action="store_true", help="heuristic extraction, append-only writes")
    p.add_argument("--max-messages", type=int, help="override the per-sweep message cap (backfill)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--cron", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--job", help=argparse.SUPPRESS)
    p.set_defaults(somnia_func=cmd_run)
    p = sub.add_parser("promote", help="promote these candidates now")
    p.add_argument("keys", nargs="+")
    p.add_argument("--target", choices=["memory", "user", "vault"])
    p.add_argument("--job", help=argparse.SUPPRESS)
    p.set_defaults(somnia_func=cmd_promote)
    p = sub.add_parser("setup", help="install the cron script and the nightly job")
    p.add_argument("--schedule", help='cron expression (default from settings, "0 3 * * *")')
    p.add_argument("--no-cron", action="store_true", help="only copy the script")
    p.set_defaults(somnia_func=cmd_setup)
    sub.add_parser("status", help="settings, cron job, last runs").set_defaults(somnia_func=cmd_status)
    sub.add_parser("doctor", help="check the wiring").set_defaults(somnia_func=cmd_doctor)
    p = sub.add_parser("explain", help="why a candidate did (not) promote")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(somnia_func=cmd_explain)
    p = sub.add_parser("forget", help="forget a candidate for good (and remove its memory entry)")
    p.add_argument("key")
    p.set_defaults(somnia_func=cmd_forget)
    p = sub.add_parser("unforget", help="take a candidate off the forget list")
    p.add_argument("key")
    p.set_defaults(somnia_func=cmd_unforget)
    p = sub.add_parser("pin", help="promote a candidate at the next sweep regardless of recurrence")
    p.add_argument("key")
    p.add_argument("--off", action="store_true")
    p.set_defaults(somnia_func=cmd_pin)
    p = sub.add_parser("diary", help="print recent dream diary entries")
    p.add_argument("-n", type=int, default=3)
    p.set_defaults(somnia_func=cmd_diary)
    p = sub.add_parser("undo", help="restore MEMORY.md/USER.md from before the last write")
    p.add_argument("--force", action="store_true")
    p.set_defaults(somnia_func=cmd_undo)
    return parser


def dispatch(args) -> int:
    func = getattr(args, "somnia_func", None)
    if func is None:
        return cmd_status(args)
    return func(args)


def main(argv: Optional[list[str]] = None) -> int:
    return dispatch(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
