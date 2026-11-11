"""Somnia benchmark: does each principle earn its place?

A seeded, synthetic 45-day history of a fictional user (Maya, an indie developer) is generated from a
catalog of facts with ground-truth labels: durable facts, one-offs, a correction, procedures, a fact
that changes mid-way, agent-only guesses and low-value trivia. The sweep runs nightly over it with a
deliberately tight memory budget, once per configuration (all principles on, all off, each one off).

Two extractor/consolidator modes:
* ``--oracle`` (default, free, deterministic, runs in CI): a scripted model that returns the catalog's
  observations, with realistic noise: some paraphrases use dangling references ("That project ..."),
  and some merges come back padded. This measures the deterministic machinery: gates, scoring,
  principles, budget handling.
* ``--llm``: the real model through Hermes (costs tokens; run inside Hermes' Python).

Metrics per configuration:
  precision  promoted facts that are truly durable / all promoted facts
  recall     durable facts that ended up in memory / durable facts in the catalog
  stale      facts in memory that were later contradicted (lower is better)
  dangling   memory entries that do not stand alone (Feynman violations)
  chars      memory chars used at the end (budget: 900)
  skills     procedures surfaced as skill ideas
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_somnia import principles as P  # noqa: E402
from hermes_somnia.config import Settings  # noqa: E402
from hermes_somnia.sinks import DELIM  # noqa: E402
from hermes_somnia.sweep import run_sweep  # noqa: E402

DAY = 86_400.0
START = 1_790_000_000.0
DAYS = 45
LIMIT = 900


@dataclass
class Fact:
    id: str
    text: str
    said: str  # what the user says
    label: str  # durable | oneoff | trivia | procedure | guess | stale
    kind: str = "fact"
    target: str = "memory"
    days: tuple = ()
    explicit: bool = False
    durability: float = 0.8
    stated_by: str = "user"
    supersedes: str = ""  # id of the fact this one replaces
    dangling: str = ""  # a paraphrase the noisy extractor sometimes returns instead


CATALOG = [
    Fact("bullets", "Maya wants answers as short bullet points.", "please keep it to short bullet points, as usual",
         "durable", "preference", "user", (1, 4, 9, 15, 22, 30, 38), durability=0.9),
    Fact("godot", "Maya's game Lumen is built with Godot 4.3.", "Lumen is on Godot 4.3, don't suggest Unity APIs",
         "durable", days=(1, 2, 6, 12, 19, 27, 33, 41), durability=0.9,
         dangling="That project is built with Godot 4.3."),
    Fact("migrations", "Never run Ledgerly database migrations on production without a backup first.",
         "you ran the migration on prod without a backup, never do that again", "durable", "correction",
         days=(3,), explicit=True, durability=0.95),
    Fact("fly", "Ledgerly is hosted on Fly.io.", "Ledgerly lives on Fly.io", "stale", days=(1, 2, 5, 8),
         durability=0.8),
    Fact("railway", "Ledgerly is hosted on Railway, not Fly.io.", "we moved Ledgerly to Railway, Fly.io is gone",
         "durable", days=(26, 29, 34, 40), durability=0.85, supersedes="fly"),
    Fact("lisbon", "Maya lives in Lisbon (Europe/Lisbon timezone).", "remember: I'm in Lisbon, use my timezone",
         "durable", "rule", "user", (11,), explicit=True, durability=0.9),
    Fact("release", "To release Lumen: bump the version, export builds, upload to itch.io, then tag the commit.",
         "release steps again: bump version, export, upload to itch, tag", "procedure", "procedure",
         days=(7, 21, 35), durability=0.8),
    Fact("tired", "Maya is tired today.", "ugh I'm so tired today", "oneoff", days=(3,), durability=0.1),
    Fact("button", "Rename the Ledgerly export button to Download.", "rename the export button to Download",
         "oneoff", days=(10,), durability=0.2),
    Fact("darkmode", "Maya probably prefers dark mode.", "", "guess", days=(13,), stated_by="agent",
         durability=0.6),
    Fact("coffee", "Maya drinks coffee while coding.", "brb, coffee", "trivia", days=(2, 9, 16, 23, 31, 39),
         durability=0.3),
    Fact("tests", "Ledgerly tests run with pytest -n auto.", "run the Ledgerly tests with pytest -n auto",
         "durable", days=(6, 14, 18, 25, 32, 44), durability=0.8,
         dangling="It runs its tests with pytest -n auto."),
    Fact("pr", "Maya reviews pull requests in the morning, before coding.", "I review PRs in the morning first",
         "durable", "preference", "user", (5, 17, 28, 43), durability=0.75),
    # inversion: a correction nobody phrased as a rule, seen twice, with modest confidence
    Fact("forcepush", "Force-pushing to Ledgerly's shared branches broke CI before; avoid git push --force there.",
         "the force push broke CI again on the shared branch", "durable", "correction",
         days=(20, 31), durability=0.6),
    # lindy: a jam prototype briefly looks like a stack change
    Fact("unity", "Maya's game Lumen is built with Unity.", "for the game jam prototype we're trying Unity this week",
         "stale", days=(36, 37), durability=0.7, supersedes="godot"),
    # pareto: durable facts that arrive late, when memory is already tight
    Fact("steam", "Lumen's Steam page goes live on the 1st of next month.", "Lumen's Steam page goes live on the 1st",
         "durable", days=(38, 41, 44), durability=0.8),
    Fact("sentry", "Ledgerly errors are tracked in Sentry; check it before debugging.",
         "check Sentry first for Ledgerly errors", "durable", days=(39, 42, 45), durability=0.8),
    Fact("jam", "Maya has a game jam on weekends this month.", "game jam again this weekend", "oneoff",
         days=(8, 9), durability=0.55),
]
BY_ID = {f.id: f for f in CATALOG}
DURABLE = {f.id for f in CATALOG if f.label == "durable"}


def build_sessions(path: Path, rng: random.Random) -> dict[str, str]:
    """state.db-shaped sessions. Returns message text -> fact id."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table sessions (id text primary key, source text, chat_type text);
        create table messages (id integer primary key autoincrement, session_id text, role text, content text,
                               timestamp real, display_kind text, _compressed_summary integer default 0);""")
    said: dict[str, str] = {}
    filler = ["can you look at the build error", "ok next", "thanks, that worked", "let's continue"]
    for day in range(1, DAYS + 1):
        todays = [f for f in CATALOG if day in f.days]
        for n in range(2):
            sid = f"d{day:02d}s{n}"
            conn.execute("insert into sessions values(?, 'desktop', null)", (sid,))
            ts = START + day * DAY + n * 3600
            for f in todays[n::2]:
                text = f.said or f"(agent) {f.text}"
                role = "user" if f.said else "assistant"
                conn.execute("insert into messages(session_id, role, content, timestamp) values(?,?,?,?)",
                             (sid, role, text, ts))
                said[text] = f.id
                ts += 60
            conn.execute("insert into messages(session_id, role, content, timestamp) values(?,?,?,?)",
                         (sid, "user", rng.choice(filler), ts))
            conn.execute("insert into messages(session_id, role, content, timestamp) values(?,?,?,?)",
                         (sid, "assistant", "Done.", ts + 5))
    conn.commit()
    conn.close()
    return said


class Oracle:
    """Scripted model with realistic noise (dangling paraphrases, padded merges)."""

    def __init__(self, said: dict[str, str], rng: random.Random):
        self.said, self.rng = said, rng
        self.calls = 0

    def __call__(self, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        if system.startswith("You read fragments"):
            return json.dumps(self.extract(user))
        if system.startswith("Choose how to incorporate"):
            return json.dumps(self.consolidate(json.loads(user)))
        return "A quiet night of small lights."

    def extract(self, prompt: str) -> dict:
        obs = []
        label = None
        for line in prompt.split("FRAGMENTS:", 1)[-1].splitlines():
            m = re.match(r"### Session (\w+)", line)
            if m:
                label = m.group(1)
                continue
            m = re.match(r"\[[\d-]+\] (?:User|Agent): (.*)", line)
            if not m or m.group(1) not in self.said:
                continue
            f = BY_ID[self.said[m.group(1)]]
            text = f.dangling if f.dangling and self.rng.random() < 0.35 else f.text
            obs.append({"ref": None, "text": text, "target": f.target, "kind": f.kind, "durability": f.durability,
                        "explicit": f.explicit, "stated_by": f.stated_by, "sessions": [label],
                        "quote": m.group(1)[:60]})
        return {"observations": obs}

    def consolidate(self, payload: dict) -> dict:
        ops, used = [], set()
        for c in payload["candidates"]:
            fid = next((f.id for f in CATALOG if f.text == c["text"] or f.dangling == c["text"]), None)
            f = BY_ID.get(fid) if fid else None
            prior = None
            if f and f.supersedes:
                old = BY_ID[f.supersedes]
                prior = next((e["id"] for e in payload["entries"]
                              if e["id"] not in used and (e["text"] == old.text or old.text[:20] in e["text"])), None)
            if prior is not None:
                used.add(prior)
                ops.append({"candidateKey": c["key"], "action": "superseded", "priorEntries": [prior],
                            "text": c["text"]})
                continue
            if any(e["text"] == c["text"] for e in payload["entries"]):
                ops.append({"candidateKey": c["key"], "action": "skipped", "priorEntries": [], "text": ""})
                continue
            text = c["text"]
            if self.rng.random() < 0.3:  # a verbose model pads the entry
                text = text.rstrip(".") + ", which is important to keep in mind for future work."
            ops.append({"candidateKey": c["key"], "action": "added", "priorEntries": [], "text": text})
        evict = []
        if payload["budget"]["used"] > payload["budget"]["goal"] * 0.9:
            free = [e for e in payload["entries"] if e["evictable"] and e["id"] not in used]
            # a model picks the flagged tail when it has one, else the oldest evictable entry
            pick = [e["id"] for e in free if e.get("tail")] or [e["id"] for e in free]
            evict = pick[:1]
        return {"operations": ops, "evictions": evict}


HUMAN = [  # what the user already wrote by hand: ~70% of the budget, so the night works under pressure
    "Maya's repos live in ~/code; Lumen and Ledgerly are separate git repos.",
    "Ledgerly is a small invoicing SaaS for freelancers; Lumen is a 2D puzzle game.",
    "Use British English in user-facing copy for Ledgerly.",
    "Do not commit to main; open a branch and a PR for every change.",
    "Lumen's art style is pixel art at 320x180, scaled 4x.",
    "Ledgerly's API is FastAPI + Postgres; the frontend is SvelteKit.",
    "Maya's CI runs on GitHub Actions; keep workflows under 10 minutes.",
    "Ask before adding a new dependency to either project.",
]


class Memory:
    def __init__(self, pressure: bool = True):
        self.data = {"memory": list(HUMAN) if pressure else [], "user": []}

    def entries(self, target):
        return list(self.data[target])

    def limit(self, target):
        return LIMIT if target == "memory" else 500

    def apply(self, target, ops):
        working = list(self.data[target])
        for op in ops:
            if op["action"] == "remove":
                if op["old_text"] not in working:
                    return {"success": False, "error": "no match"}
                working.remove(op["old_text"])
            else:
                working.append(op["content"])
        if len(DELIM.join(working)) > self.limit(target):
            return {"success": False, "error": "over limit"}
        self.data[target] = working
        return {"success": True}

    def stage(self, target, ops, summary):
        return {"success": False, "error": "no staging in the bench"}

    def files(self):
        return {}


def classify(entry: str) -> str | None:
    for f in CATALOG:
        if entry.startswith(f.text.rstrip(".")) or (f.dangling and entry.startswith(f.dangling.rstrip("."))):
            return f.id
    return None


def run_config(name: str, principles: list[str], seed: int, llm=None) -> dict:
    rng = random.Random(seed)
    tmp = Path(tempfile.mkdtemp(prefix="somnia-bench-"))
    try:
        said = build_sessions(tmp / "sessions.db", rng)
        model = llm or Oracle(said, random.Random(seed + 1))
        mem = Memory()
        settings = Settings(principles=principles, min_score=0.6, diary=False, light_lookback_days=7,
                            max_age_days=60, ingest_days=DAYS + 5, max_extract_calls=4)
        skills = set()
        for day in range(1, DAYS + 1):
            s = run_sweep(settings, complete=model, backend=mem, now=START + day * DAY + 20 * 3600,
                          base_dir=tmp / "state", sessions_db=tmp / "sessions.db")
            skills |= {classify(t) for _, t in s.skills}
        final = [e for e in mem.data["memory"] + mem.data["user"] if e not in HUMAN]
        ids = [classify(e) for e in final]
        promoted = [i for i in ids if i]
        good = [i for i in promoted if i in DURABLE]
        return {
            "config": name,
            "precision": round(len(good) / len(promoted), 2) if promoted else 0.0,
            "recall": round(len(set(good)) / len(DURABLE), 2),
            "stale": sum(1 for i in ids if i and BY_ID[i].label == "stale"),
            "dangling": sum(1 for e in final if P.validate_entry(e, e, [], "added", {"feynman"})),
            "chars": len(DELIM.join(mem.data["memory"])),
            "skills": len(skills - {None}),
            "entries": len(final),
            "human_kept": sum(1 for e in HUMAN if e in mem.data["memory"]),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="use the real model through Hermes (costs tokens)")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    llm = None
    if args.llm:
        from hermes_somnia.hermes_api import complete

        llm = complete
        args.seeds = 1
    configs = [("all on", list(P.ALL)), ("all off", [])] + [
        (f"without {p}", [x for x in P.ALL if x != p]) for p in P.ALL]
    rows = []
    for name, pr in configs:
        runs = [run_config(name, pr, seed, llm) for seed in range(args.seeds)]
        avg = {k: round(sum(r[k] for r in runs) / len(runs), 2) for k in runs[0] if k != "config"}
        rows.append({"config": name, **avg})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    cols = ["config", "precision", "recall", "stale", "dangling", "chars", "skills", "entries", "human_kept"]
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
