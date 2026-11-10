"""Target-scoped signals: leads with refs, never verdicts; nothing retained."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from support import invoke
ENV_DROP = ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def call(mid, parent, name, path, error=None, content="x = 1\n"):
    """Assistant tool call plus its result, as two pi rows."""
    arguments = {"path": path} if name == "read" else {"path": path, "content": content}
    text = error or "ok"
    return [{"type": "message", "id": mid, "parentId": parent, "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": mid + "c", "name": name, "arguments": arguments}]}},
            {"type": "message", "id": mid + "r", "parentId": mid, "message": {
                "role": "toolResult", "toolCallId": mid + "c", "toolName": name, "isError": bool(error),
                "content": [{"type": "text", "text": text}]}}]


def bash(mid, parent, command, error=None):
    """A pi bash call; ``error`` marks a failed result with that output."""
    return [{"type": "message", "id": mid, "parentId": parent, "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": mid + "c", "name": "bash", "arguments": {"command": command}}]}},
            {"type": "message", "id": mid + "r", "parentId": mid, "message": {
                "role": "toolResult", "toolCallId": mid + "c", "toolName": "bash", "isError": bool(error),
                "content": [{"type": "text", "text": error or "ok"}]}}]


def user(mid, parent, text):
    return {"type": "message", "id": mid, "parentId": parent, "message": {"role": "user", "content": text}}


def say(mid, parent, text):
    return {"type": "message", "id": mid, "parentId": parent,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.repo, self.cache, self.archive = home / "repo", home / "cache", home / "recordings"
        empty = home / "empty"
        for d in (self.repo, self.archive, empty):
            d.mkdir()
        (self.repo / "a.py").write_text("class A:\n    def f(self):\n        return 1\n\n\nclass B:\n"
                                        "    def f(self):\n        return 2\n")
        (self.repo / "b.py").write_text("y = 1\n")
        self.settings = home / "settings.json"
        self.settings.write_text(json.dumps({"recordings": {
            "pi": [str(self.archive)], "codex": [str(empty)], "claude-code": [str(empty)]}}))

    def session(self, sid, rows, stamp):
        head = {"type": "session", "id": sid, "cwd": str(self.repo), "timestamp": stamp}
        (self.archive / (sid + ".jsonl")).write_text(
            "".join(json.dumps(row) + "\n" for row in [head, *rows]), newline="")

    def fixture(self):
        # s1: failed then succeeded read in one exchange, a correction, then an unrelated exchange + correction.
        self.session("s1", [
            user("u1", None, "Fix the importer in a.py"),
            *call("a1", "u1", "read", "a.py", error="ENOENT: a.py vanished mid-run"),
            *call("a2", "a1r", "read", "a.py"),
            say("a3", "a2r", "Changed the importer."),
            user("u2", "a3", "No, that's wrong: keep the old parser in a.py."),
            say("a4", "u2", "Understood."),
            user("u3", "a4", "Now update the docs in b.py please"),
            *call("b1", "u3", "write", "b.py"),
            user("u4", "b1r", "No, revert the docs change."),
        ], "2026-01-01T00:00:00Z")
        # s2: checkpoint boundary a1; one target write on the publication branch, one on a side branch.
        self.session("s2", [
            user("u1", None, "Implement the exporter in a.py"),
            say("a1", "u1", "Starting."),
            *call("x1", "a1", "write", "a.py", content="side = 1\n"),
            *call("m1", "a1", "write", "a.py", content="main = 1\n"),
            {"type": "custom", "id": "cp", "parentId": "m1r", "customType": "shadow-compact.checkpoint",
             "data": {"version": 3, "structured": {"objective": "Exporter"},
                      "coverage": {"coverageEndEntryId": "a1"}}},
        ], "2026-01-02T00:00:00Z")
        # s3: a single read; reads are counted per session, not across.
        self.session("s3", [user("u1", None, "Look at a.py importer"), *call("r1", "u1", "read", "a.py")],
                     "2026-01-03T00:00:00Z")

    def env(self):
        return {"TRACE_SESSIONS_SETTINGS": str(self.settings), **{name: None for name in ENV_DROP}}

    def cli(self, *args, ok=True):
        p = invoke(["signals", *args, "--repo", str(self.repo), "--cache", str(self.cache)], env=self.env())
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return p.json() if "--format" not in args else p.stdout
        return p

    def rows(self, out, kind):
        return [row for row in out["results"] if row.get("categories") == [kind]]



class SignalsTests(Fixture):
    def test_each_kind_found_with_refs_and_meaning(self):
        self.fixture()
        out = self.cli("a.py", "--max-bytes", "60000")
        self.assertEqual({k: v["state"] for k, v in out["views"].items()},
                         {**dict.fromkeys(("failed_exchange", "correction_candidate", "checkpoint_gap",
                                           "repeated_read"), "leads_found"),
                          **dict.fromkeys(("failure_after_fix", "verification_gap"), "none_found_in_scanned_scope")})
        failed, = self.rows(out, "failed_exchange")
        self.assertEqual(failed["count"], 1)
        self.assertEqual(failed["last_outcome"], "succeeded")
        self.assertIn("ENOENT", failed["excerpt"])
        self.assertIn("not a confirmed pitfall", failed["meaning"])
        self.assertTrue(failed["ref"] and failed["trace"])
        correction, = self.rows(out, "correction_candidate")
        self.assertIn("keep the old parser", correction["excerpt"])
        self.assertEqual(correction["basis"], "phrasing")
        self.assertTrue(correction["corrects"])
        gap, = self.rows(out, "checkpoint_gap")
        self.assertEqual(gap["ops_after"], 1)  # side branch write excluded
        self.assertEqual(len(gap["refs"]), 1)
        read, = self.rows(out, "repeated_read")
        self.assertEqual((read["reads"], read["extra_reads"]), (2, 1))
        self.assertEqual((read["unchanged_pairs"], read["changed_pairs"]), (0, 0))  # one read failed
        self.assertIn("not waste", read["meaning"])
        for row in out["results"]:
            self.assertNotIn("waste", row["excerpt"])
        for entry in self.cache.rglob("*"):
            if entry.is_file():
                self.assertNotIn("keep the old parser", entry.read_text(errors="ignore"))

    def test_round_robin_order_and_table(self):
        self.fixture()
        out = self.cli("a.py", "--max-bytes", "60000")
        kinds = [row["categories"][0] for row in out["results"]]
        self.assertEqual(kinds[:4], ["failed_exchange", "correction_candidate", "checkpoint_gap", "repeated_read"])
        text = self.cli("a.py", "--format", "table")
        self.assertIn("views: failed_exchange=leads_found", text)
        self.assertIn("section  categories", text)

    def test_unrelated_exchange_correction_not_attributed(self):
        self.fixture()
        out = self.cli("b.py", "--max-bytes", "60000")
        correction, = self.rows(out, "correction_candidate")
        self.assertIn("revert the docs", correction["excerpt"])
        out = self.cli("a.py", "--max-bytes", "60000")
        self.assertNotIn("revert the docs", json.dumps(out["results"]))

    def test_none_found_not_searched_and_plan(self):
        self.session("s9", [user("u1", None, "Read b.py docs"), *call("r1", "u1", "read", "b.py")],
                     "2026-01-01T00:00:00Z")
        out = self.cli("a.py")
        self.assertEqual({v["state"] for k, v in out["views"].items() if k != "checkpoint_gap"},
                         {"none_found_in_scanned_scope"})
        self.assertEqual(out["views"]["checkpoint_gap"]["state"], "none_found_in_scanned_scope")
        self.assertEqual({v["state"] for v in self.cli("a.py", "--no-sessions")["views"].values()},
                         {"not_searched"})
        plan = self.cli("a.py", "--plan")
        self.assertIn("kinds", plan["plan"])

    def test_empty_nonzero_exit_is_not_a_failed_exchange(self):
        from helper import signals
        result = {"file": "r", "offset": 9}
        ops = [{"operation_id": "o1", "recording_id": "r", "lane": "l", "turn": 1, "outcome": "failed",
                "exit_code": 1, "tool": "bash", "result_ref": result, "call_ref": {"file": "r", "offset": 8}}]
        rows, skipped = signals.failed_exchanges(ops, {}, {("r", 9, 0): {"text": ""}})
        self.assertEqual((rows, skipped), ([], 1))

    def test_ambiguous_symbol_returns_candidates(self):
        self.fixture()
        out = self.cli("a.py::f")
        self.assertEqual(out["status"], "partial")
        self.assertIn(out["coverage"]["target"]["reason"], ("ambiguous", "unresolved"))
        self.assertNotIn("views", out)

    def test_paging_without_duplicates_and_tiny_budget_keeps_views(self):
        self.fixture()
        first = self.cli("a.py", "--limit", "2")
        delivery = next(item for item in first["next"] if item["effect"] == "delivery")
        second = invoke(["show", delivery["ref"]], env=self.env()).json()
        ids = [row["id"] for row in first["results"] + second["results"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 4)
        tiny = self.cli("a.py", "--max-bytes", "2048")
        self.assertEqual(set(tiny["views"]), {"failed_exchange", "correction_candidate", "checkpoint_gap",
                                              "repeated_read", "failure_after_fix", "verification_gap"})

    def test_investigate_and_inspect_offer_signals_expansions(self):
        self.fixture()
        def run(*argv):
            p = invoke([*argv, "--repo", str(self.repo), "--cache", str(self.cache)], env=self.env())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return p.json()
        paths = ["a.py", "b.py", "c.py", "d.py"]
        out = run("investigate", "importer", *[x for path in paths for x in ("--path", path)])
        offered = [n for n in out["next"] if n["purpose"].startswith("Recorded signals")]
        self.assertEqual([n["purpose"] for n in offered], [f"Recorded signals for {p}" for p in paths[:3]])
        shown = run("show", offered[0]["ref"], "--max-bytes", "60000")
        self.assertEqual(shown["operation"], "signals")
        self.assertTrue(shown["results"])
        inspected = run("inspect", "a.py")
        self.assertEqual(sum(n["purpose"] == "Recorded signals for a.py" for n in inspected["next"]), 1)


class FixAndVerificationTests(Fixture):
    CRASH = "Traceback (most recent call last):\nValueError: bad token at line 3"

    def chain(self, sid, later_request):
        self.session(sid, [
            user("u1", None, "Fix the crash in a.py when parsing input"),
            *bash("t1", "u1", "python3 -m pytest tests", error=self.CRASH),
            *call("w1", "t1r", "write", "a.py", content="fixed = 1\n"),
            say("s1", "w1r", "Fixed the parser in a.py; should work now."),
            user("u2", "s1", later_request),
            *bash("t2", "u2", "python3 -m pytest tests", error=self.CRASH),
        ], "2026-02-01T00:00:00Z")

    def test_failure_after_fix_chain_with_exposure(self):
        self.chain("s5", "The crash in a.py is back again, look at it")
        out = self.cli("a.py", "--max-bytes", "60000")
        row, = self.rows(out, "failure_after_fix")
        self.assertEqual((row["subtype"], row["exposure"]), ("failure_after_fix", "same_lane_after_fix"))
        self.assertEqual(row["basis"], "error_family_exact")
        self.assertTrue(row["initial"] and row["fixes"] and row["later"])
        self.assertIn("not proof", row["meaning"])
        self.assertEqual(out["coverage"]["signals"]["chains"], 1)

    def test_expected_failure_is_excluded(self):
        self.chain("s5", "The crash in a.py is a negative test now, rerun it")
        out = self.cli("a.py", "--max-bytes", "60000")
        self.assertEqual(self.rows(out, "failure_after_fix"), [])
        self.assertEqual(out["coverage"]["signals"]["chains_excluded"], 1)

    def test_two_runs_give_identical_results(self):
        self.fixture()
        self.chain("s5", "The crash in a.py is back again, look at it")
        for argv in (("a.py", "--max-bytes", "60000"),):
            first, second = self.cli(*argv), self.cli(*argv)
            self.assertEqual(first["results"], second["results"])
            self.assertEqual(first["views"], second["views"])
        env = self.env()
        runs = [invoke([*argv, "--repo", str(self.repo), "--cache", str(self.cache)], env=env).json()
                for argv in (["activity", "a.py"], ["activity", "a.py"],
                             ["investigate", "crash parsing", "--path", "a.py"],
                             ["investigate", "crash parsing", "--path", "a.py"])]
        self.assertTrue(all(run["results"] for run in runs))
        self.assertEqual(runs[0]["results"], runs[1]["results"])
        self.assertEqual(runs[2]["results"], runs[3]["results"])

    def test_verification_gap_subtypes(self):
        self.session("s6", [
            user("u1", None, "Update a.py and push it to origin"),
            *call("w1", "u1", "write", "a.py"),
            say("s1", "w1r", "Done. All tests pass now."),
            user("u2", "s1", "Now tidy a.py further please"),
            *bash("t1", "u2", "python3 -m unittest discover"),
            *call("w2", "t1r", "write", "a.py", content="x = 2\n"),
            user("u3", "w2r", "Check a.py with the tests again"),
            *call("r1", "u3", "read", "a.py"),
            *bash("t2", "r1r", "python3 -m pytest; echo done"),
            *bash("t3", "t2r", "python3 -m pytest", error="FAILED test_a.py::test_x"),
        ], "2026-03-01T00:00:00Z")
        out = self.cli("a.py", "--max-bytes", "60000")
        gaps = {row["subtype"]: row for row in self.rows(out, "verification_gap")}
        self.assertEqual(set(gaps), {"publication_unconfirmed", "claim_without_observed_check",
                                     "check_predates_change", "check_scope_unknown", "check_failed"})
        self.assertEqual(gaps["claim_without_observed_check"]["missing"], {"verified": "not_observed_in_scope"})
        self.assertEqual(gaps["publication_unconfirmed"]["missing"], {"pushed": "not_observed_in_scope"})
        self.assertIn(gaps["check_failed"]["scope"]["reason"], ("complete", "truncated"))
        self.assertEqual(len({row["id"] for row in self.rows(out, "verification_gap")}), 5)


if __name__ == "__main__":
    unittest.main()
