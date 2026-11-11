"""``hermes dreaming ...`` CLI (also used by the cron shim and the /dreaming slash command)."""

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

from . import __version__
from .config import PLUGIN_ID, hermes_home, load_settings, state_dir
from .diary import recent_entries
from .llm import default_complete
from .scoring import score_entry
from .store import Store
from .sweep import AlreadyRunning, render_summary, run_sweep

JOB_NAME = "hermes-dreaming"
SHIM_NAME = "hermes-dreaming.py"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _backend():
    try:
        from .sinks import HermesMemory

        return HermesMemory()
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
        print(str(exc))
        return 0
    if args.json:
        print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
        return 0
    text = render_summary(summary, verbose=args.verbose or args.dry_run)
    if args.cron:
        mode = settings.notify
        if mode == "never" or (mode == "changes" and not summary.changed and not summary.errors):
            return 0
    print(text)
    return 0


def _find_job() -> Optional[dict]:
    try:
        from cron.jobs import list_jobs

        return next((j for j in list_jobs(include_disabled=True) if j.get("name") == JOB_NAME), None)
    except Exception:
        return None


def cmd_setup(args) -> int:
    settings = load_settings()
    scripts = hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PLUGIN_DIR / "scripts" / SHIM_NAME, scripts / SHIM_NAME)
    print(f"Installed cron script: {scripts / SHIM_NAME}")
    if args.no_cron:
        return 0
    job = _find_job()
    if job:
        print(f"Cron job already exists: {job.get('id')} ({job.get('schedule_display') or job.get('schedule')}).")
    else:
        try:
            from cron.jobs import create_job

            job = create_job(None, args.schedule or settings.schedule, name=JOB_NAME, script=SHIM_NAME,
                             no_agent=True, deliver="local")
            print(f"Created cron job {job.get('id')}: {args.schedule or settings.schedule} (script-only, no agent).")
        except Exception as exc:
            print(f"Could not create the cron job ({exc}). Create it by hand:\n"
                  f"  hermes cron create \"{settings.schedule}\" --name {JOB_NAME} --script {SHIM_NAME} "
                  "--no-agent --deliver local")
            return 1
    print("Next:\n"
          "  hermes dreaming run --dry-run -v       # preview: nothing is written\n"
          "  hermes dreaming run --max-messages 3000 # optional one-time backfill of the last "
          f"{settings.ingest_days} days\n"
          "  hermes dreaming status")
    return 0


def cmd_status(args) -> int:
    settings = load_settings()
    store = Store(state_dir() / "state.db")
    try:
        entries = store.entries()
        promoted = sum(1 for e in entries if e.promoted_at)
        print(f"hermes-dreaming {__version__}")
        print(f"- destinations: {settings.destinations} | schedule: {settings.schedule} | "
              f"deletions: {settings.destructive}")
        if settings.wants_vault:
            print(f"- vault: {settings.resolved_vault()} / {settings.vault_folder}")
        print(f"- diary: {_diary_path(settings)}")
        print(f"- short-term store: {len(entries)} candidate(s), {promoted} promoted, "
              f"cursor at message {store.get('cursor', 0)}")
        print(f"- promotion policy: score>={settings.min_score}, signals>={settings.min_signals}, "
              f"sessions>={settings.min_sessions}, durability>={settings.min_durability}, "
              f"max {settings.max_promotions}/night")
        job = _find_job()
        print(f"- cron: {job.get('id')} {job.get('state') or ''} next={job.get('next_run_at')}" if job
              else "- cron: not installed (run `hermes dreaming setup`)")
        try:
            from tools import write_approval as wa

            pending = [r for r in wa.list_pending(wa.MEMORY) if "dreaming" in (r.get("summary") or "")]
            if pending:
                print(f"- staged for approval: {len(pending)} (review with /memory pending)")
        except Exception:
            pass
        for run in store.last_runs(3):
            s = run["summary"] or {}
            when = datetime.fromtimestamp(run["started_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"- run {when}{' (dry)' if run['dry_run'] else ''}: {s.get('fragments', 0)} fragments, "
                  f"{s.get('staged', 0)} staged, {len(s.get('selected') or [])} promoted"
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
        for e in matches[: args.limit]:
            s = score_entry(e, now, half_life=settings.recency_half_life_days, min_score=settings.min_score,
                            min_signals=settings.min_signals, min_sessions=settings.min_sessions,
                            min_durability=settings.min_durability, max_age_days=settings.max_age_days)
            print(e.key)
            print(f"  {e.text}")
            print(f"  target={e.target} origin={e.origin} explicit={e.explicit} signals={e.signal_count} "
                  f"sessions={len(e.sessions)} days={len(e.days)} promoted={e.promoted_to or '-'}")
            print("  score=%.3f components: %s" % (s.raw, " ".join(f"{k}={v:.2f}" for k, v in s.components.items())))
            print("  gates: " + " ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in s.passes.items()))
            if e.concept_tags:
                print("  concepts: " + ", ".join(e.concept_tags))
    finally:
        store.close()
    return 0


def cmd_diary(args) -> int:
    entries = recent_entries(_diary_path(load_settings()), args.n)
    if not entries:
        print("The dream diary is empty.")
    for e in entries:
        print(f"- {e}\n")
    return 0


def cmd_undo(args) -> int:
    runs = sorted((state_dir() / "runs").glob("*/ops-*.json"))
    if not runs:
        print("Nothing to undo.")
        return 1
    run_dir = runs[-1].parent
    try:
        from tools.memory_tool import get_memory_dir

        mem_dir = Path(get_memory_dir())
    except Exception:
        mem_dir = hermes_home() / "memories"
    from .textutil import sha256_hex

    restored = []
    for manifest_path in sorted(run_dir.glob("ops-*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        name = "USER.md" if manifest["target"] == "user" else "MEMORY.md"
        current, backup = mem_dir / name, run_dir / name
        if not backup.exists():
            continue
        expected = manifest.get("after_sha256", {}).get(manifest["target"])
        if expected and current.exists() and sha256_hex(current.read_text(encoding="utf-8")) != expected \
                and not args.force:
            print(f"{name} changed after the dream; not restoring (use --force to override).")
            continue
        with open(current.with_name(name + ".lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            shutil.copy2(backup, current)
        restored.append(name)
        manifest_path.rename(manifest_path.with_suffix(".undone"))
    print(f"Restored {', '.join(restored)} from {run_dir.name}." if restored else "Nothing restored.")
    return 0 if restored else 1


# --- argparse wiring ---

def build_parser(parser: Optional[argparse.ArgumentParser] = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(prog="hermes dreaming", description="Offline memory consolidation.")
    sub = parser.add_subparsers(dest="dreaming_cmd")
    p = sub.add_parser("run", help="run one sweep now (Light -> REM -> Deep -> diary)")
    p.add_argument("--dry-run", action="store_true", help="plan only: nothing is written")
    p.add_argument("--no-llm", action="store_true", help="heuristic extraction, append-only writes")
    p.add_argument("--max-messages", type=int, help="override the per-sweep message cap (backfill)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--cron", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(dreaming_func=cmd_run)
    p = sub.add_parser("setup", help="install the cron script and the nightly job")
    p.add_argument("--schedule", help='cron expression (default from settings, "0 3 * * *")')
    p.add_argument("--no-cron", action="store_true", help="only copy the script")
    p.set_defaults(dreaming_func=cmd_setup)
    sub.add_parser("status", help="settings, cron job, last runs").set_defaults(dreaming_func=cmd_status)
    p = sub.add_parser("explain", help="why a candidate did (not) promote")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(dreaming_func=cmd_explain)
    p = sub.add_parser("diary", help="print recent dream diary entries")
    p.add_argument("-n", type=int, default=3)
    p.set_defaults(dreaming_func=cmd_diary)
    p = sub.add_parser("undo", help="restore MEMORY.md/USER.md from before the last dream write")
    p.add_argument("--force", action="store_true")
    p.set_defaults(dreaming_func=cmd_undo)
    return parser


def dispatch(args) -> int:
    func = getattr(args, "dreaming_func", None)
    if func is None:
        return cmd_status(args)
    return func(args)


def main(argv: Optional[list[str]] = None) -> int:
    return dispatch(build_parser().parse_args(argv))


def cron_main() -> int:
    return main(["run", "--cron"] + sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
