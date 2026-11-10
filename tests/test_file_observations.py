"""File observations: read adapters, completeness, hashes, change attribution (D142-D147)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper.file_observations import read_pairs, sha
from helper.profiles import read_view
from support import invoke
ENV_DROP = ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def pi_call(mid, parent, name, arguments, text, details=None):
    return [{"type": "message", "id": mid, "parentId": parent, "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": mid + "c", "name": name, "arguments": arguments}]}},
            {"type": "message", "id": mid + "r", "parentId": mid, "message": {
                "role": "toolResult", "toolCallId": mid + "c", "toolName": name, "isError": False,
                "content": [{"type": "text", "text": text}], "details": details}}]


class ReadViewTests(unittest.TestCase):
    def test_claude_complete_needs_positive_totals(self):
        record = {"toolUseResult": {"type": "text", "file": {"content": "a\nb", "startLine": 1,
                                                             "numLines": 2, "totalLines": 2}}}
        view = read_view("claude-code", record, "1\ta\n2\tb", {})
        self.assertEqual((view["completeness"], view["content"]), ("complete", "a\nb"))
        record["toolUseResult"]["file"].update(startLine=5, totalLines=9)
        self.assertEqual(read_view("claude-code", record, "", {})["completeness"], "range")

    def test_claude_cat_n_prefixes_stripped_without_metadata(self):
        view = read_view("claude-code", {}, "     7\tx = 1\n     8\ty = 2\n", {})
        self.assertEqual((view["content"], view["start_line"], view["completeness"]),
                         ("x = 1\ny = 2", 7, "unknown"))
        self.assertIsNone(read_view("claude-code", {}, "not numbered", {}))

    def test_pi_truncation_range_and_unknown(self):
        view = read_view("pi", {}, "l1\nl2\n\n[7 more lines in file. Use offset=3 to continue.]", {})
        self.assertEqual((view["content"], view["line_count"], view["total_lines"], view["completeness"]),
                         ("l1\nl2", 2, 9, "truncated"))
        self.assertEqual(read_view("pi", {}, "x\n", {"offset": 4})["completeness"], "range")
        self.assertEqual(read_view("pi", {}, "x\n", {})["completeness"], "complete")
        self.assertIsNone(read_view("codex", {}, "x\n", {}))

    def test_read_pairs_states(self):
        def obs(i, kind="read_result", digest="h1", request=None):
            return {"observation_id": str(i), "operation_id": str(i), "recording_id": "r", "lane": "l",
                    "path": "a.py", "order": i, "kind": kind, "completeness": "complete", "adapter": "pi:read_text",
                    "request": request, "start_line": None, "content_sha256": digest}
        rows = [obs(1), obs(2), obs(3, digest="h2"), obs(4, "write_payload"), obs(5), obs(6, digest=None)]
        self.assertEqual([s for _, _, s in read_pairs(rows)],
                         ["unchanged", "changed", "edited_between", "incomparable"])


class ObservationCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.repo, self.cache, self.archive = home / "repo", home / "cache", home / "recordings"
        empty = home / "empty"
        for d in (self.repo, self.archive, empty):
            d.mkdir()
        (self.repo / "a.py").write_text("x = 1\n")
        self.settings = home / "settings.json"
        self.settings.write_text(json.dumps({"recordings": {
            "pi": [str(self.archive)], "codex": [str(empty)], "claude-code": [str(empty)]}}))
        rows = [{"type": "message", "id": "u1", "parentId": None,
                 "message": {"role": "user", "content": "Tidy a.py"}},
                *pi_call("r1", "u1", "read", {"path": "a.py"}, "x = 1\n"),
                *pi_call("r2", "r1r", "read", {"path": "a.py"}, "x = 1\n"),
                *pi_call("b1", "r2r", "bash", {"command": "sed -i s/1/2/ a.py"}, ""),
                *pi_call("r3", "b1r", "read", {"path": "a.py"}, "x = 2\n"),
                *pi_call("w1", "r3r", "write", {"path": "a.py", "content": "x = 3\n"}, "ok"),
                *pi_call("r4", "w1r", "read", {"path": "a.py"}, "x = 3\n")]
        head = {"type": "session", "id": "s1", "cwd": str(self.repo), "timestamp": "2026-01-01T00:00:00Z"}
        (self.archive / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [head, *rows]))

    def cli(self, *args):
        env = {"TRACE_SESSIONS_SETTINGS": str(self.settings), **{name: None for name in ENV_DROP}}
        p = invoke([*args, "--repo", str(self.repo), "--cache", str(self.cache)], env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.json()

    def test_activity_rows_hashes_and_attribution(self):
        out = self.cli("activity", "a.py", "--max-bytes", "60000")
        observed = [r for r in out["results"] if r.get("section") == "file_observation"]
        changes = [r for r in out["results"] if r.get("section") == "change"]
        self.assertEqual([r["kind"] for r in observed], ["read_result"] * 3 + ["write_payload", "read_result"])
        self.assertEqual(observed[0]["content_sha256"], sha("x = 1\n"))
        self.assertEqual(observed[0]["hash_basis"], "utf8_text")
        self.assertEqual(observed[0]["completeness"], "complete")  # no offset/limit, no truncation marker
        self.assertEqual(observed[3]["completeness"], "complete")
        self.assertEqual({(c["basis"], c["attribution"]) for c in changes},
                         {("changed_between_reads", "associated"), ("direct_edit", "direct")})
        inferred = next(c for c in changes if c["basis"] == "changed_between_reads")
        self.assertEqual(len(inferred["candidate_ops"]), 1)  # the sed command, a lead only
        self.assertEqual(out["coverage"]["activity"]["by_attribution"], {"associated": 1, "direct": 1})
        self.assertEqual(out["coverage"]["activity"]["file_observations"], 5)

    def test_patch_reconstruction_and_planning_snapshots(self):
        def plan(mid, parent, base, diff):
            text = json.dumps({"base": base, "repositoryRoot": str(self.repo), "changes": [
                {"path": "a.py", "status": "modified", "indexStatus": " ", "worktreeStatus": "M", "diff": diff}]})
            return pi_call(mid, parent, "git_plan_context", {}, text)
        patch = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 4\n"
        rows = [{"type": "message", "id": "u1", "parentId": None, "message": {"role": "user", "content": "Edit a.py"}},
                *plan("p1", "u1", "h1", "d1"),
                *pi_call("r1", "p1r", "read", {"path": "a.py"}, "x = 1\n"),
                *pi_call("e1", "r1r", "edit", {"path": "a.py", "oldText": "x = 1", "newText": "x = 4"}, "ok",
                         {"patch": patch}),
                *plan("p2", "e1r", "h1", "d2"),
                *plan("p3", "p2r", "h2", "d3")]
        head = {"type": "session", "id": "s2", "cwd": str(self.repo), "timestamp": "2026-01-02T00:00:00Z"}
        (self.archive / "s2.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [head, *rows]))
        out = self.cli("query", "SELECT kind, completeness, content_sha256 FROM file_observations "
                       "WHERE session='s2' ORDER BY \"order\"")
        kinds = [(r["kind"], r["completeness"]) for r in out["results"]]
        self.assertIn(("reconstructed", "complete"), kinds)
        rebuilt = next(r for r in out["results"] if r["kind"] == "reconstructed")
        self.assertEqual(rebuilt["content_sha256"], sha("x = 4\n"))
        out = self.cli("query", "SELECT attribution, change_state, uncertainty_reason, operation_id "
                       "FROM change_observations WHERE session='s2' AND basis='planning_diff' ORDER BY \"order\"")
        first, second = out["results"]
        self.assertEqual((first["attribution"], first["change_state"]), ("direct", "changed"))
        self.assertTrue(first["operation_id"])
        self.assertEqual((second["attribution"], second["uncertainty_reason"]), ("unknown", "head_changed"))

    def test_repeated_read_counts_unchanged_pairs(self):
        out = self.cli("signals", "a.py", "--max-bytes", "60000")
        read, = [r for r in out["results"] if r.get("categories") == ["repeated_read"]]
        self.assertEqual((read["reads"], read["unchanged_pairs"], read["changed_pairs"]), (4, 1, 1))
        self.assertNotIn("deferred", out["coverage"]["signals"])


if __name__ == "__main__":
    unittest.main()
