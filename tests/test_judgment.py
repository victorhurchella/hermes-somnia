"""Judgment insights (focus: insights): ingestion filter, store, extraction, synthesis, sinks, sweep."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import json

from helpers import FakeMemory, ScriptedLLM, make_sessions_db

from hermes_somnia import judgment as J
from hermes_somnia import judgment_sinks as S
from hermes_somnia import optimize as opt
from hermes_somnia.sinks import entry_flags, size_of

from hermes_somnia.config import Settings
from hermes_somnia.ingest import Fragment, read_fragments
from hermes_somnia.store import Judgment, Store

DAY = 86_400.0
NOW = 1_800_000_000.0
SOURCES = ["desktop", "tui"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class IngestionFilter(Base):
    ROWS = [
        ("s1", "desktop", "user", "Please keep the change as small as it can be", NOW - 100, None),
        ("sub", "desktop", "user", "Implement the smallest change in the billing module", NOW - 90, None),
        ("cont", "desktop", "user", "Carrying on: the smallest change still applies here", NOW - 80, None),
    ]

    def read(self, **kw):
        db = self.tmp / "sessions.db"
        make_sessions_db(db, self.ROWS, **kw)
        frags, _ = read_fragments(db, after_id=0, since_ts=0, sources=SOURCES, max_messages=100)
        return [f.session_id for f in frags]

    def test_delegated_sessions_are_skipped(self):
        got = self.read(sessions={"sub": {"parent_session_id": "s1", "title": "Subagent: build it"},
                                  "cont": {"parent_session_id": "s1"}})
        self.assertEqual(got, ["s1", "cont"])

    def test_delegated_by_created_source_or_end_reason(self):
        got = self.read(sessions={"sub": {"parent_session_id": "s1", "created_source": "subagent"},
                                  "cont": {"parent_session_id": "s1", "end_reason": "agent_close"}})
        self.assertEqual(got, ["s1"])

    def test_legacy_schema_without_columns(self):
        self.assertEqual(self.read(legacy=True), ["s1", "sub", "cont"])


def obs(store, quote, session="s1", day="2027-01-10", at=NOW, criterion="Ship the smallest change that works."):
    return store.add_observation(criterion=criterion, kind="principle", name=None, application="Asked for a one-line fix.",
                                 context="billing fix", quote=quote, session_id=session, day=day, at=at, created_at=NOW)


def judgment(key="insight:abc", **kw):
    base = dict(key=key, title="Smallest change that works", kind="principle", area="code", name=None,
                criterion="Ship the smallest change that works.", user_version="Counts lines of diff.",
                rule="Offer the smallest diff first.", proposed_at=NOW)
    base.update(kw)
    return Judgment(**base)


class StoreAndSettings(Base):
    def test_round_trip(self):
        store = Store(self.tmp / "state.db")
        a = obs(store, "one line of change for the PR")
        b = obs(store, "do not add an endpoint for this", session="s2", day="2027-01-12", at=NOW + DAY)
        store.save_judgment(judgment())
        store.assign_observations([a], "insight:abc")
        self.assertEqual([o["id"] for o in store.observations(unassigned=True)], [b])
        self.assertEqual([o["quote"] for o in store.observations(key="insight:abc")], ["one line of change for the PR"])
        self.assertEqual(store.judgment("insight:abc").rule, "Offer the smallest diff first.")
        self.assertEqual([j.key for j in store.judgments("proposed")], ["insight:abc"])
        self.assertEqual(store.judgments("approved"), [])

    def test_prune_keeps_assigned(self):
        store = Store(self.tmp / "state.db")
        a = obs(store, "old and assigned", at=NOW - 90 * DAY)
        obs(store, "old and alone", at=NOW - 90 * DAY)
        store.assign_observations([a], "insight:abc")
        self.assertEqual(store.prune_observations(NOW - 60 * DAY), 1)
        self.assertEqual(len(store.observations()), 1)

    def test_old_store_upgrades_in_place(self):
        import sqlite3
        path = self.tmp / "state.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            create table entries (key text primary key, text text not null);
            create table owned (entry_hash text primary key, target text not null, text text not null, key text,
                                score real, created_at real not null);
            insert into owned values('h', 'user', 'old entry', 'claim:1', 0.7, 1.0);
        """)
        conn.commit()
        conn.close()
        store = Store(path)
        self.assertIsNone(store.owned("user")["h"]["class"])
        store.own("p", "user", "pointer", None, None, klass="pointer")
        self.assertEqual(store.owned("user")["p"]["class"], "pointer")
        self.assertEqual(store.judgments(), [])

    def test_focus_setting(self):
        self.assertTrue(Settings().wants_insights and not Settings().wants_facts)
        self.assertTrue(Settings.from_mapping({"focus": "nonsense"}).wants_insights)
        both = Settings.from_mapping({"focus": "both"})
        self.assertTrue(both.wants_insights and both.wants_facts)
        self.assertFalse(Settings.from_mapping({"focus": "facts"}).wants_insights)


Q1 = "just one line of change for the PR, nothing more"
Q2 = "do not add an endpoint when the data is already on the client"
CRIT = "Ship the smallest change that solves the problem."


def frag(mid, session, role, text, day, at=None, origin=None):
    return Fragment(mid, session, role, origin or ("owner" if role == "user" else "agent"), text,
                    at if at is not None else NOW + mid, day)


def two_occasions():
    return [frag(1, "s1", "user", f"Keep it tiny: {Q1}", "2027-01-10"),
            frag(2, "s1", "assistant", "Understood, a one-line change.", "2027-01-10"),
            frag(3, "s2", "user", f"No. {Q2}", "2027-01-12"),
            frag(4, "s2", "assistant", "Right: do not add an endpoint when the data is already there.", "2027-01-12")]


def observation(quote, session="A", criterion=CRIT, **kw):
    return {"criterion": criterion, "kind": "principle", "name": "Occam's razor", "application": "Refused new code.",
            "context": "billing fix", "session": session, "quote": quote, **kw}


def group(ids, **kw):
    base = {"ref": None, "title": "Smallest change that works", "criterion": CRIT, "kind": "principle",
            "area": "code", "name": "Occam's razor", "user_version": "Counts lines of diff.",
            "rule": "Offer the smallest diff first and justify anything larger.", "ids": ids, "relation": "new",
            "delta": None}
    base.update(kw)
    return base


class Extraction(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "state.db")
        self.addCleanup(self.store.close)
        self.settings = Settings()

    def run_extract(self, llm, fragments=None, old=0, new=4):
        res = J.JudgmentResult()
        cursor = J.extract(self.store, fragments or two_occasions(), llm, self.settings, NOW, res,
                           old_cursor=old, new_cursor=new)
        return cursor, res

    def test_grounded_in_user_text_only(self):
        agent_only = "do not add an endpoint when the data is already there"
        llm = ScriptedLLM(judge_extract=lambda u: {"observations": [
            observation(Q1, session="B"), observation(agent_only), observation("words nobody ever typed here")]})
        cursor, res = self.run_extract(llm)
        got = self.store.observations()
        self.assertEqual([(o["quote"], o["session_id"], o["day"]) for o in got], [(Q1, "s1", "2027-01-10")])
        self.assertEqual((res.observations, res.ungrounded, cursor), (1, 2, 4))

    def test_agent_text_is_clipped_in_the_prompt(self):
        seen = []
        frags = [frag(1, "s1", "user", f"Keep it tiny: {Q1}", "2027-01-10"),
                 frag(2, "s1", "assistant", "x" * 700, "2027-01-10")]
        self.run_extract(ScriptedLLM(judge_extract=lambda u: seen.append(u) or {"observations": []}), frags)
        self.assertIn("x" * 220 + "…", seen[0])
        self.assertNotIn("x" * 221, seen[0])

    def test_rejected_insight_blocks_matching_observations(self):
        self.store.save_judgment(judgment(key=J.judgment_key(CRIT), criterion=CRIT, status="rejected"))
        llm = ScriptedLLM(judge_extract=lambda u: {"observations": [observation(Q1)]})
        _, res = self.run_extract(llm)
        self.assertEqual((self.store.observations(), res.rejected_hits), ([], 1))

    def test_untrusted_chunk_is_not_read(self):
        frags = [frag(1, "g1", "user", f"Keep it tiny: {Q1}", "2027-01-10", origin="untrusted")]
        llm = ScriptedLLM(judge_extract=lambda u: {"observations": [observation(Q1)]})
        _, res = self.run_extract(llm, frags, new=1)
        self.assertEqual((llm.calls, self.store.observations()), ([], []))

    def test_same_quote_is_stored_once(self):
        llm = ScriptedLLM(judge_extract=lambda u: {"observations": [observation(Q1), observation(Q1)]})
        self.run_extract(llm)
        self.run_extract(llm)
        self.assertEqual(len(self.store.observations()), 1)

    def test_failed_chunk_holds_the_cursor_then_is_skipped(self):
        bad = ScriptedLLM(judge_extract=lambda u: "not json at all")
        for night in (1, 2):
            cursor, res = self.run_extract(bad)
            self.assertEqual((cursor, res.llm_errors, res.errors), (0, 1, []))
        cursor, res = self.run_extract(bad)
        self.assertEqual(cursor, 4)
        self.assertIn("skipped a chunk after 3 failed nights", res.errors[0])

    def test_without_a_model_nothing_moves(self):
        cursor, res = self.run_extract(None, old=7, new=9)
        self.assertEqual((cursor, res.llm_calls), (7, 0))


class Synthesis(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "state.db")
        self.addCleanup(self.store.close)
        self.settings = Settings()

    def two(self, **second):
        a = obs(self.store, Q1, criterion=CRIT)
        kw = dict(session="s2", day="2027-01-12", at=NOW + 2 * DAY)
        kw.update(second)
        b = obs(self.store, kw.pop("quote", Q2), criterion=CRIT, **kw)
        return a, b

    def synth(self, groups, new_observations=1, known="", llm=None):
        res = J.JudgmentResult(observations=new_observations)
        self.llm = llm or ScriptedLLM(judge_synth=lambda u: {"insights": groups})
        out = J.synthesize(self.store, self.llm, self.settings, known, NOW + 3 * DAY, res)
        return out, res

    def test_two_occasions_make_a_proposal(self):
        a, b = self.two()
        out, res = self.synth([group([a, b])])
        self.assertEqual([j.status for j in out], ["proposed"])
        key = J.judgment_key(CRIT)
        self.assertEqual((res.proposed, [o["id"] for o in self.store.observations(key=key)]), ([key], [a, b]))
        self.assertEqual([o["quote"] for o in J.occasions(self.store, key)], [Q2, Q1])

    def test_gates(self):
        cases = {
            "same day": dict(day="2027-01-10"),
            "same session": dict(session="s1"),
            "same sentence": dict(quote=Q1.upper() + "!"),
        }
        for name, second in cases.items():
            with self.subTest(name):
                store_ids = self.two(**second)
                out, _ = self.synth([group(list(store_ids))])
                self.assertEqual(out, [])
                self.assertEqual(len(self.store.observations(unassigned=True)), 2)
                self.store.drop_observations(store_ids)
        a, b = self.two()
        for name, bad in {"one observation": group([a]), "bad area": group([a, b], area="garden"),
                          "bad kind": group([a, b], kind="mood"), "no rule": group([a, b], rule=" "),
                          "dangling": group([a, b], criterion="This is how the user likes small changes.")}.items():
            with self.subTest(name):
                self.assertEqual(self.synth([bad])[0], [])
        self.assertEqual(self.store.judgments(), [])

    def test_ref_attaches_drops_or_ignores(self):
        self.store.save_judgment(judgment(key="insight:ok", status="approved"))
        self.store.save_judgment(judgment(key="insight:no", status="rejected", criterion="Never use tabs."))
        a, b = self.two()
        c = obs(self.store, "tabs are awful, never tabs", session="s3", day="2027-01-13", criterion="Never use tabs.")
        d = obs(self.store, "already in my notes anyway", session="s4", day="2027-01-14", criterion="Written down.")
        out, res = self.synth([{"ref": "insight:ok", "ids": [a, b]}, {"ref": "insight:no", "ids": [c]},
                               group([d, a], relation="known")])
        self.assertEqual((out, res.attached, res.touched), ([], 2, ["insight:ok"]))
        self.assertEqual(len(self.store.observations(key="insight:ok")), 2)
        self.assertEqual(self.store.observations(unassigned=True), [])

    def test_an_id_counts_once_and_cap_keeps_the_rest_waiting(self):
        self.settings.max_insights = 1
        a, b = self.two()
        c = obs(self.store, "explain it so a newcomer gets it", session="s3", day="2027-01-13", criterion="Explain simply.")
        d = obs(self.store, "if I cannot explain it I did not get it", session="s4", day="2027-01-14",
                criterion="Explain simply.")
        e = obs(self.store, "say it in plain words please", session="s5", day="2027-01-15", criterion="Explain simply.")
        simple = group([c, d, e, a], title="Explain it simply", criterion="Explain things so a newcomer follows.",
                       area="communication")
        out, res = self.synth([group([a, b]), simple])
        self.assertEqual([j.title for j in out], ["Explain it simply"])
        self.assertEqual(len(self.store.observations(unassigned=True)), 2)
        self.assertTrue(self.store.get("insight_synth_retry"))
        out, _ = self.synth([group([a, b])], new_observations=0)  # the retry flag triggers the call
        self.assertEqual([j.title for j in out], ["Smallest change that works"])
        self.assertFalse(self.store.get("insight_synth_retry"))

    def test_no_trigger_no_call_and_failure_sets_retry(self):
        self.two()
        out, res = self.synth([], new_observations=0)
        self.assertEqual((out, self.llm.calls), ([], []))
        out, res = self.synth([], llm=ScriptedLLM(judge_synth=lambda u: RuntimeError("boom")))
        self.assertEqual(res.llm_errors, 1)
        self.assertIn("synthesis failed", res.errors[0])
        self.assertTrue(self.store.get("insight_synth_retry"))

    def test_payload_and_window(self):
        self.two()
        obs(self.store, "far too old to matter now", session="s9", day="2026-09-01", at=NOW - 80 * DAY)
        self.store.save_judgment(judgment(key="insight:old", status="rejected"))
        self.synth([], known="- Occam: the simplest solution wins.")
        payload = self.llm.calls[0][1]
        self.assertIn("ALREADY RECORDED:\n- Occam: the simplest solution wins.", payload)
        self.assertIn('"key": "insight:old", "status": "rejected"', payload)
        self.assertIn(Q1, payload)
        self.assertNotIn("far too old", payload)
        self.assertEqual(len(self.store.observations()), 2)


class Sinks(Base):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "self").mkdir(parents=True)
        self.base = self.tmp / "state"
        self.store = Store(self.base / "state.db")
        self.addCleanup(self.store.close)
        self.settings = Settings(destinations="vault", vault_path=str(self.vault), protected_paths=["self"])
        self.mem = FakeMemory(self.tmp, user=["Likes short answers."])

    def proposal(self, crit=CRIT, **kw):
        key = J.judgment_key(crit)
        a = obs(self.store, Q1 if crit == CRIT else f"first quote about {crit}", criterion=crit)
        b = obs(self.store, Q2 if crit == CRIT else f"second quote about {crit}", session="s2", day="2027-01-12",
                at=NOW + 2 * DAY, criterion=crit)
        j = judgment(key=key, criterion=crit, **kw)
        self.store.save_judgment(j)
        self.store.assign_observations([a, b], key)
        return j

    def approve(self, key):
        return S.approve(self.store, self.settings, self.mem, key, NOW + 3 * DAY, self.base)

    def test_proposal_block_and_tick_with_edited_text(self):
        j = self.proposal()
        note, lines = S.write_proposals(self.settings, self.store, [j], "2027-01-13", dry_run=False, stamp="03:00")
        text = note.read_text()
        self.assertIn("> Ticking the checkbox of an insight approves it", text)
        self.assertIn(f"- [ ] **Smallest change that works** — {CRIT}", text)
        self.assertIn(f"  - occasions: 2 on 2 days · area: code · new · key: `{j.key}`", text)
        self.assertIn(f'  - 2027-01-10 · billing fix: "{Q1}"', text)
        self.assertEqual(S.read_ticks(self.settings, self.store), [])
        note.write_text(text.replace("- [ ] **Smallest change that works**", "- [x] my own words for it")
                        + "- [x] stray\n  - key: `insight:0000beef`\n")
        warnings = []
        self.assertEqual(S.read_ticks(self.settings, self.store, warnings), [j.key])
        self.assertIn("insight:0000beef", warnings[0])
        self.approve(j.key)
        self.assertIn(f"### Smallest change that works\n{CRIT}", S.living_note_path(self.settings, self.base).read_text())
        self.assertEqual(S.read_ticks(self.settings, self.store), [])  # approved: the note is no longer read

    def test_dry_run_writes_nothing(self):
        j = self.proposal()
        note, lines = S.write_proposals(self.settings, self.store, [j], "2027-01-13", dry_run=True)
        self.assertTrue(lines and not note.exists() and self.store.judgment(j.key).note is None)

    def test_living_note_layout_and_stability(self):
        a = self.proposal(area="design")
        b = self.proposal("Explain things so a newcomer follows.", title="Explain it simply", area="communication",
                          rule="Define every term on first use.")
        self.proposal("Prefer boring tools.", title="Not approved")
        self.approve(b.key)
        self.approve(a.key)
        path = S.living_note_path(self.settings, self.base)
        self.assertEqual(path, self.vault / "somnia" / "insights.md")
        text = path.read_text()
        head = text.split("---")[1]
        self.assertIn("somnia: insights", head)
        self.assertIn("type: inbox", head)
        self.assertNotIn("status:", head)
        self.assertLess(text.index("## Design"), text.index("## Communication"))
        self.assertIn("**Rule for the agent:** Define every term on first use.", text)
        self.assertIn(f"2 occasions, 2027-01-10 to 2027-01-12 · key: `{a.key}`", text)
        self.assertNotIn("Not approved", text)
        S.write_living_note(self.settings, self.store, self.base)
        self.assertEqual(path.read_text(), text)

    def test_refuses_a_file_it_did_not_create(self):
        j = self.proposal()
        self.settings.insights_note = "self/judgment.md"
        mine = self.vault / "self" / "judgment.md"
        mine.write_text("---\ntitle: my own notes\n---\nhands off\n")
        with self.assertRaises(RuntimeError):
            self.approve(j.key)
        self.assertEqual((self.store.judgment(j.key).status, mine.read_text().endswith("hands off\n")), ("proposed", True))
        self.assertEqual(self.mem.entries("user"), ["Likes short answers."])
        mine.unlink()  # a protected folder is fine when the user names the file and Somnia creates it
        self.assertEqual(self.approve(j.key)["note"], str(mine))
        self.settings.insights_note = "../outside.md"
        with self.assertRaises(RuntimeError):
            S.living_note_path(self.settings, self.base)

    def test_pointer_added_once_and_follows_the_note(self):
        a, b = self.proposal(), self.proposal("Prefer boring tools.", title="Boring tools")
        self.assertEqual(self.approve(a.key)["warnings"], [])
        self.approve(b.key)
        pointers = [e for e in self.mem.entries("user") if e.startswith("Judgment criteria of the user:")]
        self.assertEqual(len(pointers), 1)
        self.assertIn(str(self.vault / "somnia" / "insights.md"), pointers[0])
        self.assertEqual([r["class"] for r in self.store.owned("user").values()], ["pointer"])
        self.settings.insights_note = "self/judgment.md"
        S.ensure_pointer(self.store, self.settings, self.mem, S.living_note_path(self.settings, self.base))
        pointers = [e for e in self.mem.entries("user") if e.startswith("Judgment criteria of the user:")]
        self.assertEqual((len(pointers), "self/judgment.md" in pointers[0]), (1, True))

    def test_full_user_md_or_excluded_target_only_warns(self):
        j = self.proposal()
        self.mem = FakeMemory(self.tmp, user=["x" * 1300], limits=(2200, 1375))
        out = self.approve(j.key)
        self.assertTrue(out["ok"] and "no room" in out["warnings"][0])
        self.assertEqual(self.mem.entries("user"), ["x" * 1300])
        self.assertEqual(self.store.judgment(j.key).status, "approved")
        self.settings.targets = ["memory"]
        k = self.proposal("Prefer boring tools.", title="Boring tools")
        self.assertIn("targets excludes user", self.approve(k.key)["warnings"][0])

    def test_inline_rules_first_come_and_slot_freed_by_forget(self):
        self.settings.insights_pointer = False
        made = [self.proposal(f"Answer rule number {n} about depth.", title=f"Rule {n}", area="responses",
                              rule=f"Lead with the answer, variant {n}.") for n in range(4)]
        long = self.proposal("Answers must be long.", title="Long", area="responses", rule="x" * 161)
        code = self.proposal()
        got = [self.approve(j.key)["inlined"] for j in made + [long, code]]
        self.assertEqual(got, [True, True, True, False, False, False])
        self.assertEqual(self.mem.entries("user")[1:], [f"Lead with the answer, variant {n}." for n in range(3)])
        S.forget(self.store, self.settings, self.mem, made[0].key, NOW, self.base)
        self.assertNotIn("Lead with the answer, variant 0.", self.mem.entries("user"))
        fourth = self.store.judgment(made[3].key)
        self.assertEqual(S.sync_inline(self.store, self.settings, self.mem, fourth), (True, None))

    def test_forget_and_unforget(self):
        j = self.proposal()
        note, _ = S.write_proposals(self.settings, self.store, [j], "2027-01-13", dry_run=False)
        self.approve(j.key)
        out = S.forget(self.store, self.settings, self.mem, j.key, NOW, self.base)
        self.assertTrue(out["was_approved"] and out["block"].startswith("- [ ] **Smallest change"))
        self.assertNotIn(j.key, note.read_text())
        self.assertNotIn(j.key, S.living_note_path(self.settings, self.base).read_text())
        self.assertEqual(self.store.judgment(j.key).status, "rejected")
        self.assertTrue(S.unforget(self.store, j.key))
        self.assertEqual(self.store.judgment(j.key).status, "proposed")
        self.assertFalse(S.unforget(self.store, j.key))

    def test_fact_pipeline_and_optimize_leave_insight_entries_alone(self):
        j = self.proposal()
        self.approve(j.key)
        entries = self.mem.entries("user")
        flags = entry_flags(entries, self.store.owned("user"), NOW, size_of(entries), 1375, set())
        self.assertEqual((flags.evictable, flags.locked), (set(), {1}))
        llm = lambda system, user, n: json.dumps({"entries": [{"from": [1], "text": "Read the note."}]})  # noqa: E731
        prop = opt.propose("user", entries, 1375, llm, frozenset(r["text"] for r in self.store.owned("user").values()))
        self.assertEqual(prop.items, [])

    def test_builtin_mode_keeps_the_note_in_the_state_dir(self):
        self.settings = Settings(destinations="builtin")
        j = self.proposal()
        self.assertEqual(S.write_proposals(self.settings, self.store, [j], "2027-01-13", dry_run=False), (None, []))
        self.assertEqual(self.approve(j.key)["note"], str(self.base / "insights.md"))

    def test_known_notes_are_read_only_inside_the_vault(self):
        (self.vault / "self" / "heuristics.md").write_text("- Occam: the simplest solution wins.")
        self.settings.insights_known_notes = ["self/heuristics.md", "../secret.md", "missing.md"]
        (self.tmp / "secret.md").write_text("do not read")
        text = S.known_text(self.settings)
        self.assertIn("Occam", text)
        self.assertNotIn("do not read", text)


if __name__ == "__main__":
    unittest.main()
