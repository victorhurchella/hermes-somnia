"""Judgment insights (focus: insights): ingestion filter, store, extraction, synthesis, sinks, sweep."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import make_sessions_db

from hermes_somnia.ingest import read_fragments

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


if __name__ == "__main__":
    unittest.main()
