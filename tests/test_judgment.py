"""Judgment insights (focus: insights): ingestion filter, store, extraction, synthesis, sinks, sweep."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import json

from helpers import FakeMemory, ScriptedLLM, make_sessions_db

from hermes_somnia import judgment as J

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


if __name__ == "__main__":
    unittest.main()
