"""JSON-RPC handlers for the desktop page (``plugin.hermes-somnia.<name>``). Contract: docs/rpc.md.

Read handlers are fast and side-effect free. Long work (a sweep, a dry run, promote-now) runs in a
detached subprocess on Hermes' own interpreter and is tracked as a job file the page polls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__, hermes_api
from . import principles as P
from .config import PAGE_SETTINGS, Settings, load_settings, sessions_db_path, state_dir
from .diary import FALLBACK_NARRATIVE, all_entries
from .insights import alarm, budgets, candidate_view, nights, r3, rank_kw, session_titles, stats
from .scoring import score_entry
from .sinks import DELIM, append_to_vault, vault_dir
from .store import Store
from .textutil import jaccard, sha1_12

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SHIM = PLUGIN_DIR / "scripts" / "hermes-somnia.py"
JOB_NAME = "hermes-somnia"


class RpcError(RuntimeError):
    pass


def _store() -> Store:
    return Store(state_dir() / "state.db")


def _backend():
    try:
        return hermes_api.HermesMemory()
    except Exception:
        return None


def _str(params: dict, key: str, required: bool = True, max_len: int = 4000) -> str:
    v = params.get(key)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > max_len:
        raise RpcError(f"'{key}' must be a non-empty string")
    return v


def _target(params: dict) -> str:
    t = params.get("target")
    if t not in ("memory", "user"):
        raise RpcError("'target' must be 'memory' or 'user'")
    return t


def _cron() -> dict:
    job = hermes_api.find_cron_job(JOB_NAME)
    if not job:
        return {"installed": False, "id": None, "next_run_at": None, "state": None}
    return {"installed": True, "id": job.get("id"), "next_run_at": job.get("next_run_at"),
            "state": job.get("state") or ("paused" if job.get("paused") else "scheduled")}


def _rejected_keys(store: Store) -> set[str]:
    return {r["key"] for r in store.rejected() if r.get("key")}


# --- read ---

def overview(params: dict, **_) -> dict:
    s = load_settings()
    now = time.time()
    backend = _backend()
    store = _store()
    try:
        b = budgets(backend, store, s, now)
        entries = store.entries()
        rejected = _rejected_keys(store)
        near = _near(store, s, now, limit=1)
        cron = _cron()
        runs = store.runs(1, include_dry=True)
        last = None
        if runs:
            r = runs[0]
            summ = r["summary"] or {}
            last = {"at": r["started_at"], "dry_run": bool(r["dry_run"]),
                    "summary": f"{summ.get('fragments', 0)} turns read, {summ.get('promoted', 0)} remembered, "
                               f"{summ.get('merged', 0)} merged"}
        vault = None
        if s.wants_vault:
            try:
                folder = vault_dir(s)
                notes = sorted(folder.glob("20*.md")) if folder.exists() else []
                open_items = sum(n.read_text(encoding="utf-8", errors="ignore").count("- [ ] ") for n in notes[-30:])
                vault = {"path": str(s.resolved_vault()), "folder": s.vault_folder, "inbox_notes": len(notes),
                         "open_items": open_items}
            except Exception as exc:
                vault = {"path": str(s.resolved_vault() or ""), "folder": s.vault_folder, "inbox_notes": 0,
                         "open_items": 0, "error": str(exc)}
        pending = len(backend.pending()) if backend is not None and hasattr(backend, "pending") else 0
        result = {
            "version": __version__, "destinations": s.destinations, "destructive": s.destructive,
            "schedule": s.schedule, "timezone": s.timezone, "cron": cron, "llm": hermes_api.describe_llm(),
            "last_run": last, "budgets": b, "nights": nights(store, 30),
            "alarm": alarm(store, s, b, cron, now, near),
            "counts": {
                "candidates": sum(1 for e in entries if not e.promoted_at),
                "near": len(_near(store, s, now, limit=500)),
                "promoted": sum(1 for e in entries if e.promoted_at),
                "rejected": len(rejected),
                "skills": sum(1 for e in entries if e.kind == "procedure" and not e.promoted_at),
                "stale": sum(1 for x in b for e in x["entries"] if e["stale"]),
                "pending": pending,
            },
            "vault": vault,
            "pressure": {x["target"]: round(x["used"] / x["limit"], 3) if x["limit"] else 0 for x in b},
            "principles": {"active": sorted(P.enabled(s.principles)), "all": list(P.ALL),
                           "labels": P.LABELS, "descriptions": P.DESCRIPTIONS},
            "settings": {k: getattr(s, k) for k in PAGE_SETTINGS},
        }
        return r3(result)
    finally:
        store.close()


def _near(store: Store, s: Settings, now: float, limit: int) -> list[dict]:
    rejected = _rejected_keys(store)
    entries = [e for e in store.entries() if not e.promoted_at and e.key not in rejected and e.kind != "procedure"
               and e.origin in ("owner", "agent")]
    scored = sorted(((score_entry(e, now, **rank_kw(s)), e) for e in entries), key=lambda x: -x[0].raw)
    out = []
    for sc, e in scored[:limit]:
        out.append({"key": e.key, "text": e.text, "score": round(sc.raw, 3),
                    "missing": sc.missing(s.min_signals, s.min_sessions, s.min_durability)})
    return out


def changes(params: dict, **_) -> dict:
    store = _store()
    try:
        run = params.get("run", "latest")
        run_id = store.latest_change_run() if run in (None, "latest") else int(run)
        if run_id is None:
            return {"run_at": None, "date": None, "changes": []}
        row = store.conn.execute("select started_at from runs where id=?", (run_id,)).fetchone()
        at = row[0] if row else None
        items = store.changes(run_id)
        ids = {sid for c in items for sid in c["sessions"]}
        titles = session_titles(sessions_db_path(), ids)
        latest_applied = store.latest_change_run()
        out = []
        for c in items:
            out.append({
                "run_at": c["at"], "key": c["key"], "action": c["action"], "target": c["target"], "text": c["text"],
                "replaced": c["replaced"], "score": c["score"] or 0.0, "components": c["components"],
                "sessions": [{"id": sid, **{k: titles.get(sid, {}).get(k) for k in ("title", "source")}, "day": ""}
                             for sid in reversed(c["sessions"])],
                "reason": c["reason"],
                "undoable": run_id == latest_applied and c["action"] in ("added", "merged", "superseded", "evicted"),
            })
        return r3({"run_at": at, "date": datetime.fromtimestamp(at).strftime("%Y-%m-%d") if at else None,
                   "changes": out})
    finally:
        store.close()


def candidates(params: dict, **_) -> dict:
    s = load_settings()
    now = time.time()
    flt = params.get("filter") or "near"
    q = (params.get("q") or "").strip().lower()
    limit = max(1, min(int(params.get("limit") or 50), 500))
    store = _store()
    try:
        rejected_rows = store.rejected()
        rejected = {r["key"] for r in rejected_rows if r.get("key")}
        entries = store.entries()
        if flt == "near":
            pool = [e for e in entries if not e.promoted_at and e.key not in rejected and e.kind != "procedure"]
        elif flt == "promoted":
            pool = [e for e in entries if e.promoted_at]
        elif flt == "skills":
            pool = [e for e in entries if e.kind == "procedure" and not e.promoted_at and e.key not in rejected]
        elif flt == "rejected":
            known = {e.key for e in entries}
            pool = [e for e in entries if e.key in rejected]
            from .store import Entry
            pool += [Entry(key=r["key"] or "reject:" + r["claim_hash"], text=r["text"]) for r in rejected_rows
                     if (r.get("key") or "") not in known]
            rejected |= {e.key for e in pool}
        else:
            pool = list(entries)
        if q:
            pool = [e for e in pool if q in e.text.lower() or any(q in t for t in e.concept_tags)]
        ids = {sid for e in pool for sid in e.sessions[-8:]}
        titles = session_titles(sessions_db_path(), ids)
        views = [candidate_view(e, now, s, titles, rejected) for e in pool]
        if flt in ("near", "skills"):
            views.sort(key=lambda v: -v["raw"])
        elif flt == "promoted":
            views.sort(key=lambda v: -(v["promoted_at"] or 0))
        return {"items": views[:limit], "total": len(views)}
    finally:
        store.close()


def candidate(params: dict, **_) -> dict:
    key = _str(params, "key")
    s = load_settings()
    store = _store()
    try:
        e = store.entry(key)
        if e is None:
            raise RpcError(f"unknown candidate {key}")
        titles = session_titles(sessions_db_path(), set(e.sessions[-8:]))
        return candidate_view(e, time.time(), s, titles, _rejected_keys(store))
    finally:
        store.close()


def themes(params: dict, **_) -> dict:
    store = _store()
    try:
        now = time.time()
        pool = [e for e in store.entries() if e.last_seen_at >= now - 30 * 86_400 and e.origin in ("owner", "agent")]
        counts: dict[str, list[str]] = {}
        for e in pool:
            for tag in set(e.concept_tags):
                counts.setdefault(tag, []).append(e.key)
        n = max(1, len(pool))
        top = sorted(((t, ks) for t, ks in counts.items() if len(ks) >= 2), key=lambda x: (-len(x[1]), x[0]))[:24]
        nodes = [{"tag": t, "count": len(ks), "strength": round(min(1.0, 2 * len(ks) / n), 3), "keys": ks[:50]}
                 for t, ks in top]
        names = [x["tag"] for x in nodes]
        links = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                w = len(set(counts[a]) & set(counts[b]))
                if w:
                    links.append({"a": a, "b": b, "weight": w})
        return {"themes": nodes, "links": sorted(links, key=lambda l: -l["weight"])[:60]}
    finally:
        store.close()


def diary(params: dict, **_) -> dict:
    s = load_settings()
    limit = max(1, min(int(params.get("limit") or 20), 200))
    path = state_dir() / "DREAMS.md"
    if s.wants_vault:
        try:
            path = vault_dir(s) / "DREAMS.md"
        except Exception:
            pass
    items = all_entries(path)[:limit]
    return {"entries": [{**i, "fallback": i["text"] == FALLBACK_NARRATIVE} for i in items], "path": str(path)}


def stats_rpc(params: dict, **_) -> dict:
    store = _store()
    try:
        return r3(stats(store, load_settings(), time.time()))
    finally:
        store.close()


def sessions_fed(params: dict, **_) -> dict:
    store = _store()
    try:
        fed: dict[str, list[str]] = {}
        for e in store.entries():
            if e.promoted_at and not (e.promoted_to or "").startswith("staged:"):
                for sid in e.sessions:
                    fed.setdefault(sid, []).append(e.key)
        return {"sessions": fed}
    finally:
        store.close()


# --- jobs ---

def _jobs_dir() -> Path:
    d = state_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_job(job_id: str, **fields) -> None:
    path = _jobs_dir() / f"{job_id}.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data.update(fields)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(r3(data), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _running_job() -> Optional[dict]:
    for p in sorted(_jobs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if d.get("state") == "running" and time.time() - float(d.get("started_at") or 0) < 3600:
            pid = d.get("pid")
            if pid:
                try:
                    os.kill(int(pid), 0)
                except OSError:
                    write_job(d["id"], state="failed", error="the job process died", finished_at=time.time())
                    continue
            return d
    return None


def start_job(kind: str, args: list[str]) -> dict:
    running = _running_job()
    if running:
        return {"ok": True, "job": running["id"], "already_running": True}
    job_id = uuid.uuid4().hex[:10]
    write_job(job_id, id=job_id, kind=kind, state="running", started_at=time.time(), finished_at=None,
              summary="", result=None, error=None)
    log = open(_jobs_dir() / f"{job_id}.log", "w")
    env = {**os.environ, "HERMES_SOMNIA_PLUGIN_DIR": str(PLUGIN_DIR)}
    proc = subprocess.Popen([sys.executable, str(SHIM), *args, "--job", job_id], stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env, cwd=str(PLUGIN_DIR))
    write_job(job_id, pid=proc.pid)
    return {"ok": True, "job": job_id}


def job(params: dict, **_) -> dict:
    job_id = _str(params, "id", max_len=40)
    if not job_id.isalnum():
        raise RpcError("bad job id")
    path = _jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise RpcError(f"unknown job {job_id}")
    _running_job()  # reaps dead jobs
    return json.loads(path.read_text(encoding="utf-8"))


def run(params: dict, **_) -> dict:
    dry = bool(params.get("dry_run"))
    return start_job("dry_run" if dry else "run", ["run", "--verbose"] + (["--dry-run"] if dry else []))


def promote(params: dict, **_) -> dict:
    key = _str(params, "key", max_len=80)
    target = params.get("target")
    args = ["promote", key]
    if target in ("memory", "user", "vault"):
        args += ["--target", target]
    return start_job("promote", args)


# --- writes ---

def _find_entry(backend, target: str, index: int, old: str) -> str:
    entries = backend.entries(target)
    if 0 <= index < len(entries) and entries[index] == old:
        return old
    if old in entries:
        return old
    raise RpcError("that memory entry changed since the page loaded; refresh and try again")


def forget(params: dict, **_) -> dict:
    key = _str(params, "key", max_len=80)
    reason = (params.get("reason") or "forgotten from the desktop page")[:200]
    store = _store()
    backend = _backend()
    removed = False
    try:
        e = store.entry(key)
        if e is None:
            raise RpcError(f"unknown candidate {key}")
        remove_entry = params.get("remove_entry", True)
        if remove_entry and e.promoted_to and e.promoted_to.startswith("builtin:") and backend is not None:
            target = e.promoted_to.split(":")[1]
            owned = {r["key"]: r for r in store.owned(target).values() if r.get("key") == key}
            texts = [r["text"] for r in owned.values()]
            entries = backend.entries(target)
            for text in texts or [x for x in entries if jaccard(x, e.text) >= 0.6][:1]:
                if text in entries:
                    out = backend.apply(target, [{"action": "remove", "old_text": text}])
                    if not out.get("success"):
                        raise RpcError(str(out.get("error") or out))
                    store.disown(sha1_12(text))
                    removed = True
        store.reject(e.text, key, reason)
        e.pinned = False
        store.save(e)
        store.commit()
        return {"ok": True, "removed_entry": removed}
    finally:
        store.close()


def unforget(params: dict, **_) -> dict:
    key = _str(params, "key", max_len=80)
    store = _store()
    try:
        ok = store.unreject(key)
        e = store.entry(key)
        if e and not ok:
            ok = store.unreject(e.text)
        store.commit()
        return {"ok": ok}
    finally:
        store.close()


def pin(params: dict, **_) -> dict:
    key = _str(params, "key", max_len=80)
    store = _store()
    try:
        e = store.entry(key)
        if e is None:
            raise RpcError(f"unknown candidate {key}")
        e.pinned = bool(params.get("pinned", True))
        store.save(e)
        store.commit()
        return {"ok": True, "pinned": e.pinned}
    finally:
        store.close()


def entry_edit(params: dict, **_) -> dict:
    target = _target(params)
    old = _str(params, "old")
    text = _str(params, "text", max_len=600).strip()
    backend = _backend()
    if backend is None:
        raise RpcError("Hermes memory is not available")
    old = _find_entry(backend, target, int(params.get("index", -1)), old)
    out = backend.apply(target, [{"action": "remove", "old_text": old}, {"action": "add", "content": text}])
    if not out.get("success"):
        raise RpcError(str(out.get("error") or out))
    store = _store()
    try:
        row = store.owned(target).get(sha1_12(old))
        if row:
            store.disown(sha1_12(old))
            store.own(sha1_12(text), target, text, row.get("key"), row.get("score"), row.get("run_id"),
                      at=row.get("created_at"))
            store.commit()
    finally:
        store.close()
    return {"ok": True}


def entry_remove(params: dict, **_) -> dict:
    target = _target(params)
    old = _str(params, "old")
    backend = _backend()
    if backend is None:
        raise RpcError("Hermes memory is not available")
    old = _find_entry(backend, target, int(params.get("index", -1)), old)
    out = backend.apply(target, [{"action": "remove", "old_text": old}])
    if not out.get("success"):
        raise RpcError(str(out.get("error") or out))
    store = _store()
    try:
        row = store.owned(target).get(sha1_12(old))
        store.disown(sha1_12(old))
        if row:  # removing a Somnia entry by hand is a rejection signal; "reject" makes it explicit
            if params.get("reject", True):
                store.reject(old, row.get("key"), "removed from memory on the desktop page")
        elif params.get("reject"):
            store.reject(old, None, "removed from memory on the desktop page")
        store.commit()
    finally:
        store.close()
    return {"ok": True}


def entry_to_vault(params: dict, **_) -> dict:
    target = _target(params)
    old = _str(params, "old")
    s = load_settings()
    backend = _backend()
    if backend is None:
        raise RpcError("Hermes memory is not available")
    if s.resolved_vault() is None:
        raise RpcError("no vault configured (vault_path, OBSIDIAN_VAULT_PATH or WIKI_PATH)")
    old = _find_entry(backend, target, int(params.get("index", -1)), old)
    note = append_to_vault(s, old, datetime.now().strftime("%Y-%m-%d"), f"moved from {target.upper()}.md")
    out = backend.apply(target, [{"action": "remove", "old_text": old}])
    if not out.get("success"):
        raise RpcError(str(out.get("error") or out))
    store = _store()
    try:
        store.disown(sha1_12(old))
        store.commit()
    finally:
        store.close()
    return {"ok": True, "note": str(note)}


def undo(params: dict, **_) -> dict:
    from .cli import undo_last

    restored, message = undo_last(force=bool(params.get("force")))
    if not restored:
        raise RpcError(message)
    return {"ok": True, "restored": restored}


def skill_dismiss(params: dict, **_) -> dict:
    key = _str(params, "key", max_len=80)
    store = _store()
    try:
        e = store.entry(key)
        if e is None:
            raise RpcError(f"unknown candidate {key}")
        store.reject(e.text, key, "skill idea dismissed")
        store.commit()
        return {"ok": True}
    finally:
        store.close()


def settings_set(params: dict, **_) -> dict:
    key = _str(params, "key", max_len=40)
    if key not in PAGE_SETTINGS:
        raise RpcError(f"'{key}' can only be changed in the Plugins settings page")
    value = params.get("value")
    current = getattr(Settings(), key)
    coerced = Settings.from_mapping({key: value})
    new_value = getattr(coerced, key)
    if key == "principles":
        new_value = [p for p in P.ALL if p in P.enabled(new_value)]
    if key == "destructive" and new_value not in ("apply", "stage"):
        raise RpcError("destructive must be 'apply' or 'stage'")
    if key == "notify" and new_value not in ("changes", "always", "never"):
        raise RpcError("notify must be changes, always or never")
    if type(new_value) is not type(current):
        raise RpcError(f"bad value for {key}")
    if not hermes_api.save_plugin_setting(key, new_value):
        raise RpcError("could not save the setting")
    return {"ok": True, "key": key, "value": new_value}


HANDLERS: dict[str, Callable[..., Any]] = {
    "overview": overview, "changes": changes, "candidates": candidates, "candidate": candidate,
    "themes": themes, "diary": diary, "stats": stats_rpc, "sessions.fed": sessions_fed, "job": job,
    "forget": forget, "unforget": unforget, "pin": pin, "promote": promote, "entry.edit": entry_edit,
    "entry.remove": entry_remove, "entry.to_vault": entry_to_vault, "undo": undo, "run": run,
    "skill.dismiss": skill_dismiss, "settings.set": settings_set,
}


def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    def handler(params=None, **kwargs):
        return fn(params if isinstance(params, dict) else {}, **kwargs)

    handler.__name__ = f"somnia_{fn.__name__}"
    return handler
