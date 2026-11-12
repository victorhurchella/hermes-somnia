from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import FakeMemory

from hermes_somnia import optimize as opt
from hermes_somnia.store import Store
from hermes_somnia.textutil import sha1_12

MEM = [
    "The laptop has no Docker. Local infra for ledgerly: ~/code/ledgerly/infra/up.sh starts Postgres 16 (127.0.0.1:5544, "
    "trust auth) and MinIO (:9100); there is no psql, so please always use node + the postgres pkg instead.",
    "The user's git workflow rule is that work always happens on a new branch and they never push directly to main.",
    "Every push requires the user's explicit approval first, before anything is pushed.",
    "web_extract fails here: use `defuddle parse <url> --md` or the browser.",
]


def scripted(plan, verdicts=None):
    calls = []

    def complete(system, user, _max):
        calls.append(system)
        if system is opt.OPTIMIZE_SYSTEM:
            return json.dumps({"entries": plan})
        ids = [p["id"] for p in json.loads(user)]
        return json.dumps({"checks": [{"id": i, "ok": (verdicts or {}).get(i, True), "lost": "x"} for i in ids]})

    complete.calls = calls
    return complete


class OptimizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_specifics_cover_paths_ports_code_and_identifiers(self):
        s = opt.specifics(MEM[0] + " " + MEM[3])
        for tok in ("~/code/ledgerly/infra/up.sh", "16", "127.0.0.1:5544", ":9100", "defuddle parse <url> --md", "web_extract"):
            self.assertTrue(any(tok.lower() in x or x in tok.lower() for x in s), tok)

    def test_guards_keep_entries_that_lose_or_invent_specifics(self):
        plan = [
            {"from": [0], "text": "No Docker. Infra: ~/code/ledgerly/infra/up.sh → Postgres 16 (127.0.0.1:5544, trust auth), "
                                  "MinIO (:9100). No psql: use node + postgres pkg."},
            {"from": [1, 2], "text": "Git: always a new branch; never push to main; every push needs explicit approval."},
            {"from": [3], "text": "web_extract fails: use defuddle or the browser."},  # drops the exact command
        ]
        prop = opt.propose("memory", MEM, 2200, scripted(plan))
        d = prop.as_dict()
        by = {tuple(i["from"]): i for i in d["items"]}
        self.assertTrue(by[(0,)]["ok"], by[(0,)]["reason"])
        self.assertTrue(by[(1, 2)]["ok"])
        self.assertFalse(by[(3,)]["ok"])
        self.assertIn("would drop", by[(3,)]["reason"])
        self.assertGreater(d["saved"], 50)
        self.assertEqual(d["after_chars"], d["before_chars"] - d["saved"])

    def test_verifier_veto_and_not_shorter_are_kept(self):
        plan = [{"from": [1], "text": MEM[1] + " Really."}, {"from": [2], "text": "Pushes need approval."}]
        prop = opt.propose("memory", MEM, 2200, scripted(plan, verdicts={1: False}))
        items = prop.as_dict()["items"]
        self.assertEqual(items[0]["reason"], "not shorter than the original")
        self.assertTrue(items[1]["reason"].startswith("would change behaviour"))
        self.assertEqual(prop.as_dict()["saved"], 0)

    def test_unknown_or_duplicate_indexes_leave_originals_alone(self):
        plan = [{"from": [0, 9], "text": "x"}, {"from": [1], "text": "Git: new branch always; never push to main."},
                {"from": [1, 2], "text": "dup"}]
        d = opt.propose("memory", MEM, 2200, scripted(plan)).as_dict()
        self.assertEqual([i["from"] for i in d["items"]], [[1]])

    def test_apply_replaces_in_place_merges_and_writes_an_undo_manifest(self):
        mem = FakeMemory(self.tmp, memory=list(MEM))
        store = Store(self.tmp / "s.db")
        store.own(sha1_12(MEM[2]), "memory", MEM[2], "claim:abc", 0.8)
        store.commit()
        plan = [{"from": [1, 2], "text": "Git: always a new branch; never push to main; every push needs explicit approval."}]
        d = opt.propose("memory", MEM, 2200, scripted(plan)).as_dict()
        run_dir = self.tmp / "runs" / "x-optimize"
        out = opt.apply(mem, "memory", d, [0], run_dir, store)
        self.assertEqual(out["applied"], 1)
        self.assertEqual(mem.data["memory"][1], plan[0]["text"])
        self.assertEqual(len(mem.data["memory"]), 3)
        self.assertIn(sha1_12(plan[0]["text"]), store.owned("memory"))
        self.assertTrue((run_dir / "ops-memory.json").exists() and (run_dir / "MEMORY.md").exists())
        with self.assertRaises(opt.StaleProposal):
            opt.apply(mem, "memory", d, [0], run_dir, store)
        store.close()


if __name__ == "__main__":
    unittest.main()
