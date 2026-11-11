from __future__ import annotations

import math
import shutil
import re
import tempfile
import time
import unittest
from pathlib import Path

from helpers import FakeMemory, ScriptedLLM, make_sessions_db

from hermes_somnia import diary
from hermes_somnia.config import Settings
from hermes_somnia.ingest import read_fragments
from hermes_somnia.scoring import consolidation_component, recency_component, score_entry
from hermes_somnia.sinks import validate_plan, parse_plan, size_of
from hermes_somnia.store import Entry
from hermes_somnia.sweep import run_sweep
from hermes_somnia.textutil import clip, concept_tags, jaccard, novel_specifics

DAY = 86_400.0
NOW = 1_800_000_000.0


class TextTests(unittest.TestCase):
    def test_concept_tags_follow_openclaw_order_and_filters(self):
        tags = concept_tags("Configured the gateway router on vlan-20 with backups to s3 and the 2026-10-01.md note")
        self.assertEqual(tags[:3], ["backup", "backups", "gateway"])
        self.assertIn("vlan-20", tags)
        self.assertNotIn("the", tags)
        self.assertLessEqual(len(tags), 8)

    def test_portuguese_stopwords(self):
        self.assertEqual(concept_tags("isso também não é sobre nada"), ["nada"])

    def test_jaccard(self):
        self.assertEqual(jaccard("a b c", "a b c"), 1.0)
        self.assertAlmostEqual(jaccard("alpha beta", "alpha gamma"), 1 / 3)

    def test_clip_prefers_sentence_break(self):
        text = "First sentence is here and fairly long. Second sentence goes on and on and on forever."
        self.assertEqual(clip(text, 60), "First sentence is here and fairly long.")

    def test_novel_specifics(self):
        self.assertEqual(novel_specifics("Postgres on port 5433", ["Postgres runs on 5433"]), [])
        self.assertEqual(novel_specifics("Postgres on port 5434", ["Postgres runs on 5433"]), ["5434"])


class ScoringTests(unittest.TestCase):
    def test_recency_half_life(self):
        self.assertAlmostEqual(recency_component(14, 14), 0.5)

    def test_consolidation(self):
        self.assertEqual(consolidation_component(["2026-01-01"]), 0.2)
        self.assertAlmostEqual(consolidation_component(["2026-01-01", "2026-01-08"]),
                               0.55 * math.log1p(1) / math.log1p(4) + 0.45)

    def test_single_session_needs_explicit_owner(self):
        e = Entry("k", "Victor prefers Portuguese", origin="owner", signal_count=2, total_score=1.8,
                  sessions=["s1"], days=["2026-01-01"], last_seen_at=NOW)
        self.assertFalse(score_entry(e, NOW).passes["sessions"])
        e.explicit = True
        self.assertTrue(score_entry(e, NOW).passes["sessions"])

    def test_untrusted_never_passes(self):
        e = Entry("k", "x", origin="untrusted", signal_count=9, total_score=9, sessions=list("abcde"),
                  days=["2026-01-01", "2026-01-09"], last_seen_at=NOW)
        self.assertFalse(score_entry(e, NOW).eligible)


class IngestTests(unittest.TestCase):
    def test_filters_injected_turns_tool_noise_and_cron(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            make_sessions_db(db, [
                ("s1", "desktop", "user", "Please always answer me in Portuguese from now on", NOW, None),
                ("s1", "desktop", "assistant", "", NOW + 1, None),
                ("s1", "desktop", "assistant", "Combinado, sempre em português.", NOW + 2, None),
                ("s1", "desktop", "user", "[ASYNC DELEGATION BATCH COMPLETE] results...", NOW + 3, None),
                ("s1", "desktop", "user", "Process finished with exit 0 and lots of output", NOW + 4,
                 "process_complete"),
                ("s1", "desktop", "user",
                 "[OUT-OF-BAND USER MESSAGE — x]\nuse the staging db instead\n[/OUT-OF-BAND USER MESSAGE]",
                 NOW + 5, "steer"),
                ("c1", "cron", "user", "Nightly report prompt that should never count", NOW + 6, None),
            ])
            frags, cursor = read_fragments(db, after_id=0, since_ts=NOW - DAY, sources=["desktop"], max_messages=50)
            self.assertEqual([(f.role, f.text) for f in frags], [
                ("user", "Please always answer me in Portuguese from now on"),
                ("assistant", "Combinado, sempre em português."),
                ("user", "use the staging db instead"),
            ])
            self.assertEqual(cursor, 6)  # cron rows never match the query
            again, _ = read_fragments(db, after_id=cursor, since_ts=0, sources=["desktop"], max_messages=50)
            self.assertEqual(again, [])


class PlanValidationTests(unittest.TestCase):
    def _cands(self, *texts):
        from hermes_somnia.scoring import Scored

        return [Scored(Entry(f"k{i}", t), 0.8 - i * 0.01, 0.8) for i, t in enumerate(texts)]

    def test_rejects_invented_specifics(self):
        cands = self._cands("API runs on port 8080")
        plan = parse_plan('{"operations":[{"candidateKey":"k0","action":"added","priorEntries":[],'
                          '"text":"API runs on port 9090"}]}', "memory", cands)
        with self.assertRaises(ValueError):
            validate_plan(plan, [], set(), cands, max_chars=300, loss_fraction=0.25, limit=2200, goal=1980)

    def test_eviction_only_of_owned_entries(self):
        entries = ["human fact one", "dream fact two", "human fact three", "human fact four"]
        cands = self._cands("new fact")
        raw = ('{"operations":[{"candidateKey":"k0","action":"added","priorEntries":[],"text":"new fact"}],'
               '"evictions":[0]}')
        with self.assertRaises(ValueError):
            validate_plan(parse_plan(raw, "memory", cands), entries, {1}, cands, max_chars=300, loss_fraction=0.25,
                          limit=2200, goal=1980)
        ok = parse_plan(raw.replace('"evictions":[0]', '"evictions":[1]'), "memory", cands)
        final = validate_plan(ok, entries, {1}, cands, max_chars=300, loss_fraction=0.25, limit=2200, goal=1980)
        self.assertEqual(final, ["human fact one", "human fact three", "human fact four", "new fact"])

    def test_budget_defers_weakest_add(self):
        entries = ["x" * 80]
        cands = self._cands("a" * 50, "b" * 50)
        raw = ('{"operations":[{"candidateKey":"k0","action":"added","priorEntries":[],"text":"' + "a" * 50 + '"},'
               '{"candidateKey":"k1","action":"added","priorEntries":[],"text":"' + "b" * 50 + '"}]}')
        plan = parse_plan(raw, "memory", cands)
        final = validate_plan(plan, entries, set(), cands, max_chars=300, loss_fraction=0.25, limit=150, goal=140)
        self.assertLessEqual(size_of(final), 140)
        self.assertEqual(plan.deferred, [("k1", "memory budget")])


def _two_session_db(tmp: Path) -> Path:
    db = tmp / "sessions.db"
    make_sessions_db(db, [
        ("s1", "desktop", "user", "The deploy for project eixo uses pnpm 12, keep that in mind", NOW - 3 * DAY, None),
        ("s1", "desktop", "assistant", "Noted: eixo deploys with pnpm 12.", NOW - 3 * DAY + 5, None),
        ("s2", "tui", "user", "Again: eixo builds need pnpm 12, not npm", NOW - DAY, None),
        ("s2", "tui", "assistant", "Right, pnpm 12 for eixo.", NOW - DAY + 5, None),
    ])
    return db


def _extract(user: str):
    labels = re.findall(r"### Session (\w+)", user)
    return {"observations": [{"ref": None, "text": "Project eixo builds and deploys with pnpm 12, not npm.",
                              "target": "memory", "durability": 0.9, "explicit": False, "stated_by": "user",
                              "sessions": labels}]}


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = _two_session_db(self.tmp)
        self.settings = Settings(min_score=0.5, light_lookback_days=7)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_end_to_end_builtin_add(self):
        mem = FakeMemory(self.tmp, memory=["Host has no Docker."])

        def consolidate(payload):
            c = payload["candidates"][0]
            return {"operations": [{"candidateKey": c["key"], "action": "added", "priorEntries": [],
                                    "text": c["text"]}]}

        llm = ScriptedLLM(extract=_extract, consolidate=consolidate)
        s = run_sweep(self.settings, complete=llm, backend=mem, now=NOW, base_dir=self.tmp / "state",
                      sessions_db=self.db)
        self.assertEqual(len(s.selected), 1, s.as_dict())
        self.assertIn("Project eixo builds and deploys with pnpm 12, not npm.", mem.data["memory"])
        self.assertTrue((self.tmp / "state" / "DREAMS.md").read_text().count("Soft rain"))
        report = next((self.tmp / "state" / "reports").glob("*.md")).read_text()
        for heading in ("## Light Sleep", "## REM Sleep", "## Deep Sleep"):
            self.assertIn(heading, report)
        again = run_sweep(self.settings, complete=llm, backend=mem, now=NOW + 60, base_dir=self.tmp / "state",
                          sessions_db=self.db)
        self.assertEqual(again.light.fragments, 0)
        self.assertEqual(again.selected, [])

    def _merge(self, settings):
        mem = FakeMemory(self.tmp, memory=["eixo uses pnpm."])

        def consolidate(payload):
            c = payload["candidates"][0]
            return {"operations": [{"candidateKey": c["key"], "action": "merged", "priorEntries": [0],
                                    "text": "Project eixo builds and deploys with pnpm 12, not npm."}]}

        s = run_sweep(settings, complete=ScriptedLLM(extract=_extract, consolidate=consolidate), backend=mem,
                      now=NOW, base_dir=self.tmp / "state", sessions_db=self.db)
        return mem, s

    def test_merge_is_applied_by_default_like_openclaw(self):
        mem, s = self._merge(self.settings)
        self.assertEqual(mem.data["memory"], ["Project eixo builds and deploys with pnpm 12, not npm."])
        self.assertEqual(mem.staged, [])
        backups = list((self.tmp / "state" / "runs").glob("*/MEMORY.md"))
        self.assertEqual([b.read_text() for b in backups], ["eixo uses pnpm."])

    def test_merge_is_staged_when_configured(self):
        self.settings.destructive = "stage"
        mem, s = self._merge(self.settings)
        self.assertEqual(mem.data["memory"], ["eixo uses pnpm."])
        self.assertEqual(len(mem.staged), 1)
        self.assertEqual(s.sinks[0].staged_id, "p1")

    def test_dry_run_writes_nothing(self):
        mem = FakeMemory(self.tmp, memory=["Host has no Docker."])
        s = run_sweep(self.settings, complete=ScriptedLLM(extract=_extract), backend=mem, now=NOW, dry_run=True,
                      base_dir=self.tmp / "state", sessions_db=self.db)
        self.assertEqual(len(s.selected), 1)
        self.assertEqual(mem.data["memory"], ["Host has no Docker."])
        self.assertFalse((self.tmp / "state" / "DREAMS.md").exists())
        real = run_sweep(self.settings, complete=ScriptedLLM(extract=_extract), backend=mem, now=NOW,
                         base_dir=self.tmp / "state", sessions_db=self.db, dry_run=True)
        self.assertEqual(real.light.fragments, 4)  # cursor was not advanced by the first dry run

    def test_dry_run_reports_structured_changes(self):
        mem = FakeMemory(self.tmp, memory=["Host has no Docker."])
        s = run_sweep(self.settings, complete=ScriptedLLM(extract=_extract), backend=mem, now=NOW, dry_run=True,
                      base_dir=self.tmp / "state", sessions_db=self.db)
        d = s.as_dict()
        self.assertTrue(d["changes"])
        c = d["changes"][0]
        self.assertEqual(set(c) >= {"action", "target", "text", "replaced", "score", "sessions", "reason"}, True)
        self.assertFalse(c["undoable"])

    def test_diary_language_reaches_the_prompt(self):
        from hermes_somnia.diary import build_diary_prompt
        self.assertIn("Write the entry in pt-BR.", build_diary_prompt(["a"], [], [], "2026-11-11", [], "pt-BR"))
        self.assertIn("language most of the memory fragments", build_diary_prompt(["a"], [], [], "", [], "auto"))

    def test_invalid_plan_falls_back_to_append(self):
        mem = FakeMemory(self.tmp)
        llm = ScriptedLLM(extract=_extract, consolidate=lambda p: {"operations": "nope"})
        s = run_sweep(self.settings, complete=llm, backend=mem, now=NOW, base_dir=self.tmp / "state",
                      sessions_db=self.db)
        self.assertEqual(s.sinks[0].plan_source, "fallback")
        self.assertEqual(len(mem.data["memory"]), 1)

    def test_vault_destination_writes_only_inbox(self):
        vault = self.tmp / "brain"
        (vault / "self").mkdir(parents=True)
        (vault / "self" / "foco.md").write_text("mine")
        mem = FakeMemory(self.tmp)
        settings = Settings(destinations="vault", vault_path=str(vault), protected_paths=["self"], min_score=0.5,
                            light_lookback_days=7)
        s = run_sweep(settings, complete=ScriptedLLM(extract=_extract), backend=mem, now=NOW,
                      base_dir=self.tmp / "state", sessions_db=self.db)
        self.assertEqual(s.errors, [])
        notes = list((vault / "somnia").glob("20*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("- [ ] Project eixo builds", notes[0].read_text())
        self.assertTrue((vault / "somnia" / "DREAMS.md").exists())
        self.assertEqual((vault / "self" / "foco.md").read_text(), "mine")
        self.assertTrue(mem.data["memory"][0].startswith("Somnia inbox:"))

    def test_vault_folder_cannot_overlap_protected(self):
        vault = self.tmp / "brain"
        vault.mkdir()
        settings = Settings(destinations="vault", vault_path=str(vault), vault_folder="self/dreams",
                            protected_paths=["self"], min_score=0.5, light_lookback_days=7)
        s = run_sweep(settings, complete=ScriptedLLM(extract=_extract), backend=FakeMemory(self.tmp), now=NOW,
                      base_dir=self.tmp / "state", sessions_db=self.db)
        self.assertTrue(any("protected" in e for e in s.errors))
        self.assertFalse((vault / "self").exists())

    def test_packed_chunk_attributes_each_session(self):
        llm = ScriptedLLM(extract=_extract)
        s = run_sweep(self.settings, complete=llm, backend=FakeMemory(self.tmp), now=NOW, dry_run=True,
                      base_dir=self.tmp / "state", sessions_db=self.db)
        self.assertEqual(s.light.llm_calls, 1)
        self.assertEqual(len([c for c in llm.calls if c[0] == "extract"]), 1)
        self.assertEqual(len(s.selected), 1)

    def test_unlabelled_observation_counts_once(self):
        def extract(user):
            out = _extract(user)
            out["observations"][0].pop("sessions")
            return out

        s = run_sweep(self.settings, complete=ScriptedLLM(extract=extract), backend=FakeMemory(self.tmp), now=NOW,
                      dry_run=True, base_dir=self.tmp / "state", sessions_db=self.db)
        self.assertEqual(s.selected, [])

    def test_no_llm_uses_explicit_cues_only(self):
        s = run_sweep(Settings(llm=False, light_lookback_days=7), complete=None, backend=FakeMemory(self.tmp),
                      now=NOW, base_dir=self.tmp / "state", sessions_db=self.db)
        self.assertEqual(s.light.observations, 0)  # no "remember/always/sempre" cue in the fixture


class DiaryTests(unittest.TestCase):
    def test_recent_entries_skip_fallback_and_clamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "DREAMS.md"
            from datetime import datetime

            diary.append_entry(path, "first " * 100, datetime(2026, 1, 1, 3))
            diary.append_entry(path, diary.FALLBACK_NARRATIVE, datetime(2026, 1, 2, 3))
            diary.append_entry(path, "newest entry", datetime(2026, 1, 3, 3))
            got = diary.recent_entries(path)
            self.assertEqual(got[0], "newest entry")
            self.assertEqual(len(got), 2)
            self.assertTrue(got[1].endswith("...") and len(got[1]) <= 363)

    def test_managed_block_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.md"
            diary.replace_block(path, "## Deep Sleep", diary.DEEP_START, diary.DEEP_END, ["- one"])
            diary.replace_block(path, "## Deep Sleep", diary.DEEP_START, diary.DEEP_END, ["- two"])
            text = path.read_text()
            self.assertEqual(text.count("## Deep Sleep"), 1)
            self.assertIn("- two", text)
            self.assertNotIn("- one", text)


if __name__ == "__main__":
    time.tzset()
    unittest.main()
