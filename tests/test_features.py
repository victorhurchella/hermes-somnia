from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import FakeMemory, ScriptedLLM, make_sessions_db

from hermes_somnia import principles as P
from hermes_somnia.config import Settings
from hermes_somnia.scoring import Scored, score_entry
from hermes_somnia.sinks import EntryFlags, parse_plan, validate_plan
from hermes_somnia.store import Entry, Store
from hermes_somnia.sweep import morning_report, run_sweep
from hermes_somnia.textutil import jaccard

DAY = 86_400.0
NOW = 1_800_000_000.0
FACT = "Project eixo builds and deploys with pnpm 12, not npm."


def extract_fact(text=FACT, kind="fact", quote=None, explicit=False):
    def extract(user: str):
        labels = re.findall(r"### Session (\w+)", user)
        q = quote if quote is not None else re.search(r"\] User: ([^\n]{0,60})", user).group(1)
        return {"observations": [{"ref": None, "text": text, "target": "memory", "durability": 0.9,
                                  "explicit": explicit, "stated_by": "user", "sessions": labels, "kind": kind,
                                  "quote": q}]}
    return extract


def two_sessions(tmp: Path, start=NOW, extra=()) -> Path:
    db = tmp / "sessions.db"
    make_sessions_db(db, [
        ("s1", "desktop", "user", "The deploy for project eixo uses pnpm 12, keep that in mind", start - 3 * DAY, None),
        ("s1", "desktop", "assistant", "Noted: eixo deploys with pnpm 12.", start - 3 * DAY + 5, None),
        ("s2", "tui", "user", "Again: eixo builds need pnpm 12, not npm", start - DAY, None),
        ("s2", "tui", "assistant", "Right, pnpm 12 for eixo.", start - DAY + 5, None),
        *extra,
    ])
    return db


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.settings = Settings(min_score=0.5, light_lookback_days=7)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sweep(self, llm, mem=None, db=None, now=NOW, **kw):
        return run_sweep(kw.pop("settings", self.settings), complete=llm, backend=mem or FakeMemory(self.tmp),
                         now=now, base_dir=self.state, sessions_db=db or two_sessions(self.tmp), **kw)


class RejectListTests(Base):
    def test_forgotten_fact_is_never_learned_again(self):
        store = Store(self.state / "state.db")
        store.reject(FACT, None, "test")
        store.commit()
        store.close()
        s = self.sweep(ScriptedLLM(extract=extract_fact()))
        self.assertEqual(s.light.rejected_hits, 1)
        self.assertEqual(s.selected, [])

    def test_near_duplicate_of_a_forgotten_fact_is_blocked(self):
        store = Store(self.state / "state.db")
        store.reject("Project eixo builds and deploys with pnpm 12 not npm", None, "test")
        store.commit()
        store.close()
        s = self.sweep(ScriptedLLM(extract=extract_fact()))
        self.assertEqual(s.light.rejected_hits, 1)

    def test_recurring_forgotten_fact_is_offered_again_after_180_days(self):
        store = Store(self.state / "state.db")
        store.reject(FACT, None, "test", at=NOW - 200 * DAY)
        store.conn.execute("update rejected set recur=2")
        store.commit()
        store.close()
        s = self.sweep(ScriptedLLM(extract=extract_fact()))
        self.assertEqual(s.light.reasked, 1)
        store = Store(self.state / "state.db")
        self.assertTrue(any(e.reask for e in store.entries()))
        self.assertEqual(store.rejected(), [])
        store.close()


class WakeGateTests(Base):
    def test_quiet_night_costs_zero_llm_calls(self):
        llm = ScriptedLLM(extract=extract_fact())
        db = two_sessions(self.tmp)
        first = self.sweep(llm, db=db)
        self.assertGreater(first.llm_calls, 0)
        llm.calls.clear()
        second = self.sweep(llm, db=db, now=NOW + 60)
        self.assertTrue(second.quiet)
        self.assertEqual(llm.calls, [])
        self.assertEqual(second.llm_calls, 0)
        self.assertFalse(second.as_dict()["noop"])


class GroundingTests(Base):
    def test_quote_is_stored_as_evidence(self):
        self.sweep(ScriptedLLM(extract=extract_fact()))
        store = Store(self.state / "state.db")
        e = store.entries()[0]
        self.assertTrue(e.evidence and "pnpm 12" in e.evidence[0]["quote"])
        store.close()

    def test_invented_quote_is_penalized(self):
        s = self.sweep(ScriptedLLM(extract=extract_fact(quote="the user loves yarn berry forever")),
                       dry_run=True)
        self.assertEqual(s.light.ungrounded, 1)


class SecretMaskingTests(Base):
    def test_secrets_never_reach_the_model(self):
        extra = [("s3", "desktop", "user", "my key is sk-abcdefghijklmnopqrstuvwx123 please use it", NOW - DAY, None)]
        llm = ScriptedLLM(extract=lambda u: {"observations": []})
        self.sweep(llm, db=two_sessions(self.tmp, extra=extra), dry_run=True)
        prompt = next(u for k, u in llm.calls if k == "extract")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx123", prompt)
        self.assertNotIn("abcdefghijklmnopqrstuvwx", prompt)


class PlacementTests(Base):
    def test_procedure_becomes_a_skill_idea_not_memory(self):
        mem = FakeMemory(self.tmp)
        s = self.sweep(ScriptedLLM(extract=extract_fact("To release eixo: bump version, build, upload, tag.",
                                                        kind="procedure")), mem=mem)
        self.assertEqual(s.selected, [])
        self.assertEqual(len(s.skills), 1)
        self.assertEqual(mem.data["memory"], [])


def cand(key, text, score=0.8, **kw):
    return Scored(Entry(key, text, **kw), score, score)


class PrincipleTests(unittest.TestCase):
    def test_inversion_bonus_for_corrections(self):
        base = dict(origin="owner", signal_count=2, total_score=1.6, sessions=["a", "b"], days=["2026-01-01"],
                    last_seen_at=NOW, first_seen_at=NOW)
        plain = score_entry(Entry("k", "x", **base), NOW, principles=set(P.ALL)).raw
        corr = score_entry(Entry("k", "x", kind="correction", **base), NOW, principles=set(P.ALL))
        self.assertAlmostEqual(corr.raw - plain, P.INVERSION_BONUS)
        off = score_entry(Entry("k", "x", kind="correction", **base), NOW, principles=set())
        self.assertAlmostEqual(off.raw, plain)

    def test_lindy_bonus_needs_age_and_confirmations(self):
        young = score_entry(Entry("k", "x", signal_count=5, total_score=4, first_seen_at=NOW - 5 * DAY,
                                  last_seen_at=NOW), NOW)
        old = score_entry(Entry("k", "x", signal_count=5, total_score=4, first_seen_at=NOW - 90 * DAY,
                                last_seen_at=NOW), NOW)
        self.assertNotIn("lindy", young.components)
        self.assertGreater(old.components["lindy"], 0)

    def _validate(self, raw, entries, cands, flags=None, active=None):
        plan = parse_plan(raw, "memory", cands)
        final = validate_plan(plan, entries, flags or EntryFlags(), cands, max_chars=300, loss_fraction=0.5,
                              limit=2200, goal=1980, active=active)
        return plan, final

    def test_occam_defers_a_padded_entry(self):
        cands = [cand("k0", "Uses pnpm 12.")]
        raw = json.dumps({"operations": [{"candidateKey": "k0", "action": "added", "priorEntries": [],
                                          "text": "Uses pnpm 12, which is the package manager that it uses for everything."}]})
        plan, final = self._validate(raw, [], cands)
        self.assertEqual(final, [])
        self.assertTrue(plan.deferred[0][1].startswith("occam"))
        plan, final = self._validate(raw, [], cands, active=set())
        self.assertEqual(len(final), 1)

    def test_feynman_defers_a_dangling_reference(self):
        cands = [cand("k0", "That project deploys with pnpm 12.")]
        raw = json.dumps({"operations": [{"candidateKey": "k0", "action": "added", "priorEntries": [],
                                          "text": "That project deploys with pnpm 12."}]})
        plan, final = self._validate(raw, [], cands)
        self.assertTrue(plan.deferred[0][1].startswith("feynman"))

    def test_lindy_protects_long_confirmed_entries_from_supersession(self):
        entries = ["Deploys run on Fly.io."]
        cands = [cand("k0", "Deploys run on Railway.")]
        raw = json.dumps({"operations": [{"candidateKey": "k0", "action": "superseded", "priorEntries": [0],
                                          "text": "Deploys run on Railway."}]})
        flags = EntryFlags(evictable={0}, lindy={0})
        plan, final = self._validate(raw, entries, cands, flags=flags)
        self.assertEqual(final, entries)
        self.assertTrue(plan.deferred[0][1].startswith("lindy"))
        cands[0].entry.explicit = True
        plan, final = self._validate(raw, entries, cands, flags=flags)
        self.assertEqual(final, ["Deploys run on Railway."])

    def test_pareto_tail_is_the_only_evictable_part_under_pressure(self):
        info = {0: {"confirmations": 20}, 1: {"confirmations": 15}, 2: {"confirmations": 1}, 3: {"confirmations": 1}}
        split = P.pareto_split(["a", "b", "c", "d"], info, used=2000, limit=2200, active={"pareto"})
        self.assertTrue(split.active)
        self.assertEqual(split.tail, {2, 3})
        relaxed = P.pareto_split(["a", "b", "c", "d"], info, used=500, limit=2200, active={"pareto"})
        self.assertFalse(relaxed.active)
        protected = P.pareto_split(["a", "b", "c", "d"], {**info, 3: {"confirmations": 1, "protected": True}},
                                   used=2000, limit=2200, active={"pareto"})
        self.assertNotIn(3, protected.tail)

    def test_lens_lines_follow_the_switches(self):
        self.assertIn("corrections", P.lens("extract", {"inversion"}))
        self.assertEqual(P.lens("extract", set()), "")


class PromoteNowTests(Base):
    def test_promote_now_ignores_recurrence_gates(self):
        db = self.tmp / "one.db"
        make_sessions_db(db, [("s1", "desktop", "user", "eixo uses pnpm 12 for builds and deploys", NOW - DAY, None)])
        mem = FakeMemory(self.tmp)
        llm = ScriptedLLM(extract=extract_fact())
        first = self.sweep(llm, mem=mem, db=db)
        self.assertEqual(first.selected, [])
        key = Store(self.state / "state.db").entries()[0].key
        s = run_sweep(self.settings, complete=llm, backend=mem, now=NOW + 10, base_dir=self.state, sessions_db=db,
                      only_keys=[key])
        self.assertEqual(len(s.selected), 1)
        self.assertIn(FACT, mem.data["memory"])


class ChangeLogTests(Base):
    def test_changes_are_logged_per_run(self):
        mem = FakeMemory(self.tmp)
        s = self.sweep(ScriptedLLM(extract=extract_fact(), consolidate=lambda p: {"operations": [
            {"candidateKey": p["candidates"][0]["key"], "action": "added", "priorEntries": [], "text": FACT}]}), mem=mem)
        store = Store(self.state / "state.db")
        run_id = store.latest_change_run()
        changes = store.changes(run_id)
        self.assertEqual([c["action"] for c in changes], ["added"])
        self.assertIn("frequency", changes[0]["components"])
        store.close()
        report = morning_report(s)
        self.assertIn("remembered 1 thing", report)
        self.assertIn(FACT, report)


class TextTests(unittest.TestCase):
    def test_accents_and_plurals_fold(self):
        self.assertGreater(jaccard("sessões de configuração", "sessao de configuracao"), 0.9)


class RpcTests(Base):
    def setUp(self):
        super().setUp()
        self.env = {k: os.environ.get(k) for k in ("HERMES_HOME", "HERMES_SOMNIA_STATE_DIR", "HERMES_SOMNIA_SESSIONS_DB")}
        os.environ["HERMES_HOME"] = str(self.tmp / "home")
        os.environ["HERMES_SOMNIA_STATE_DIR"] = str(self.state)
        os.environ["HERMES_SOMNIA_SESSIONS_DB"] = str(two_sessions(self.tmp))
        (self.tmp / "home" / "memories").mkdir(parents=True)
        (self.tmp / "home" / "memories" / "MEMORY.md").write_text("Host has no Docker.")

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_read_and_write_handlers(self):
        from hermes_somnia import hermes_api, rpc

        backend = hermes_api.HermesMemory()
        llm = ScriptedLLM(extract=extract_fact(), consolidate=lambda p: {"operations": [
            {"candidateKey": p["candidates"][0]["key"], "action": "added", "priorEntries": [], "text": FACT}]})
        run_sweep(Settings(min_score=0.5, light_lookback_days=7), complete=llm, backend=backend, now=NOW,
                  base_dir=self.state, sessions_db=Path(os.environ["HERMES_SOMNIA_SESSIONS_DB"]))
        self.assertIn(FACT, (self.tmp / "home" / "memories" / "MEMORY.md").read_text())

        ov = rpc.overview({})
        json.dumps(ov, allow_nan=False)
        memory = next(b for b in ov["budgets"] if b["target"] == "memory")
        owners = {e["text"]: e["owner"] for e in memory["entries"]}
        self.assertEqual(owners, {"Host has no Docker.": "human", FACT: "somnia"})
        self.assertEqual(ov["principles"]["active"], sorted(P.DEFAULT))
        self.assertEqual(len(ov["nights"]), 1)

        ch = rpc.changes({})
        self.assertEqual(ch["changes"][0]["action"], "added")
        self.assertTrue(ch["changes"][0]["undoable"])

        promoted = rpc.candidates({"filter": "promoted"})["items"]
        self.assertEqual(promoted[0]["status"], "promoted")
        key = promoted[0]["key"]
        self.assertEqual(rpc.candidate({"key": key})["text"], FACT)

        out = rpc.forget({"key": key})
        self.assertTrue(out["removed_entry"])
        self.assertNotIn(FACT, (self.tmp / "home" / "memories" / "MEMORY.md").read_text())
        self.assertEqual(rpc.candidates({"filter": "rejected"})["items"][0]["status"], "rejected")
        self.assertTrue(rpc.unforget({"key": key})["ok"])

        with self.assertRaises(rpc.RpcError):
            rpc.settings_set({"key": "vault_path", "value": "/tmp"})
        with self.assertRaises(rpc.RpcError):
            rpc.entry_edit({"target": "memory", "index": 0, "old": "not there", "text": "x"})
        rpc.entry_edit({"target": "memory", "index": 0, "old": "Host has no Docker.", "text": "The host has no Docker."})
        self.assertIn("The host has no Docker.", (self.tmp / "home" / "memories" / "MEMORY.md").read_text())

        for name in ("themes", "stats", "sessions.fed", "diary"):
            json.dumps(rpc.HANDLERS[name]({}), allow_nan=False)


if __name__ == "__main__":
    unittest.main()


class BudgetAlarmTests(Base):
    def test_full_memory_alarms_only_when_somnia_writes_there(self):
        from hermes_somnia.insights import alarm
        full = [{"target": "user", "used": 1370, "limit": 1375, "entries": [{"owner": "human"}]}]
        store = Store(self.tmp / "alarm.db")
        try:
            self.assertEqual(alarm(store, Settings(destinations="builtin"), full, None, NOW)["kind"], "budget_full")
            vault = self.tmp / "vault"
            vault.mkdir()
            self.assertIsNone(alarm(store, Settings(destinations="vault", vault_path=str(vault)), full, None, NOW))
        finally:
            store.close()


class ForgetInVaultTests(unittest.TestCase):
    NOTE = """---
title: Somnia 2026-11-12
---
# Somnia 2026-11-12

## Night 03:03

### Promoted
- [ ] Keep me: the team uses pnpm.
  - target: memory | kind: fact | score: 0.72 | key: `claim:aaa`
  - evidence: "we use pnpm"
- [ ] Forget me: something private.
  - target: user | kind: fact | score: 0.54 | key: `claim:bbb`
  - evidence: "private"

### Themes
- `pnpm` across 3 sessions
"""

    def test_remove_and_restore_a_block(self):
        from hermes_somnia.sinks import remove_from_vault, restore_to_vault

        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / "2026-11-12.md"
            note.write_text(self.NOTE, encoding="utf-8")
            block = remove_from_vault(note, "claim:bbb")
            text = note.read_text(encoding="utf-8")
            self.assertTrue(block.startswith("- [ ] Forget me"))
            self.assertNotIn("private", text)
            self.assertIn("Keep me", text)
            self.assertIn("### Themes", text)
            self.assertIsNone(remove_from_vault(note, "claim:zzz"))
            remove_from_vault(note, "claim:aaa")
            self.assertNotIn("### Promoted", note.read_text(encoding="utf-8"))
            restore_to_vault(note, block)
            self.assertIn("Forget me: something private.", note.read_text(encoding="utf-8"))
