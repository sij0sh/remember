"""Target bridge fixtures: known histories, independent lanes, no transcript retention."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import remember
from helper.edit_hunks import anchor
from helper.graph_store import Store


class TargetHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.cache = self.home / "cache"
        self.archive = self.home / "recordings"
        self.archive.mkdir()
        self.write("a.py", "def target():\n    return 2\n")

    def write(self, path, text):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        return dest

    def git(self, *args, repo=None):
        return subprocess.check_output(["git", "-C", str(repo or self.repo), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.com", *args], text=True).strip()

    def init(self):
        self.git("init", "-q")
        self.commit("initial")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def recording(self, sid="past", root=None, path="a.py", content=None, request="Reject the eager approach", extra=()):
        content = content or "def target():\n    return 2\n"
        rows = [{"type": "session", "id": sid, "cwd": str(root or self.repo)},
                {"type": "message", "id": "u", "message": {"role": "user", "content": request}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "write", "arguments": {"path": path, "content": content}}]}},
                {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult", "toolCallId": "c",
                 "toolName": "write", "isError": False, "content": [{"type": "text", "text": "done"}]}}, *extra]
        dest = self.archive / (sid + ".jsonl")
        dest.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return dest

    def args(self, *argv):
        return remember.defaults(remember.parser().parse_args([*argv, "--repo", str(self.repo), "--cache", str(self.cache)]))

    def run_command(self, *argv):
        args = self.args(*argv)
        return remember.run(args, Store(args.repo, args.cache))

    def history(self, target="a.py::target", *argv):
        return self.run_command("history", target, "--recordings", str(self.archive), *argv)

    def cli(self, *argv):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/remember.py"), *argv,
                                 "--repo", str(self.repo), "--cache", str(self.cache)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_edit_reaches_original_request_and_paired_result(self):
        self.recording()
        out = self.history()
        lead = next(r for r in out["results"] if r["basis"] == "recorded_definition_at_edit")
        self.assertEqual(lead["symbols"][0]["qualname"], "target")
        command = lead["next"][0]["argv"]
        import shlex
        expanded = subprocess.check_output(shlex.split(command), text=True)
        self.assertIn("Reject the eager approach", expanded)
        self.assertTrue(any("exchange" in n["argv"] for n in lead["next"]))
        self.assertEqual(out["coverage"]["git"][0]["state"], "unavailable")

    def test_renames_exact_paths_and_independent_git_lane(self):
        self.init()
        self.git("mv", "a.py", "renamed.py")
        self.commit("rename")
        self.recording(path="a.py")
        self.recording(sid="backup", path="a.py.bak")
        self.recording(sid="other", path="other/a.py")
        out = self.history("renamed.py")
        activity = [r for r in out["results"] if r["category"] == "recorded_activity"]
        self.assertEqual({r["evidence"]["session"] for r in activity}, {"past"})
        self.assertTrue(any(a["basis"] == "git_rename" for a in out["scope"]["aliases"]))
        only_git = self.history("renamed.py", "--no-sessions")
        self.assertTrue(only_git["results"])
        self.assertEqual(only_git["coverage"]["sessions"]["state"], "not_searched")

    def test_ambiguity_does_not_scan_and_refs_preserve_snapshot(self):
        self.write("other.py", "def target():\n    return 3\n")
        with patch("helper.target_history.candidate_files", side_effect=AssertionError("scan")):
            self.assertEqual(self.history("target")["status"], "ambiguous")
        mapped = self.run_command("map", "a.py::target")
        ref = mapped["history"]["next"]["target"]
        self.write("a.py", "def replacement():\n    return 4\n")
        out = self.history(ref)
        self.assertEqual(out["target"]["qualname"], "target")
        self.assertEqual(out["target"]["snapshot"], ref.split("/")[0])
        self.assertEqual(out["coverage"]["mapping"]["source_basis"], "pinned_source")

    def test_project_roots_and_path_relocation_are_separate(self):
        old = self.home / "old"
        self.recording(root=old)
        self.assertFalse(self.history()["results"])
        self.assertFalse(self.history("a.py", "--project-root", str(old))["results"])
        out = self.history("a.py", "--project-root", str(old), "--root-map", f"{old}={self.repo}")
        self.assertTrue(out["results"])
        self.assertEqual(out["coverage"]["sessions"]["project_also"], [str(old)])

    def test_split_repository_association(self):
        old = self.home / "parent"
        (old / "pkg").mkdir(parents=True)
        (old / "pkg/a.py").write_text("def target():\n    return 1\n")
        self.git("init", "-q", repo=old)
        self.git("add", ".", repo=old)
        self.git("commit", "-qm", "before split", repo=old)
        self.recording(root=old, path="pkg/a.py")
        out = self.history("a.py", "--project-root", str(old), "--history-repo", f"{old}=pkg")
        self.assertTrue(any(r["category"] == "git_change" and r["subject"] == "before split" for r in out["results"]))
        self.assertTrue(any(r["category"] == "recorded_activity" for r in out["results"]))

    def test_mentions_use_paths_relative_to_each_recording_project(self):
        parent = self.home
        self.recording(root=parent, path="unrelated.py", request="a.py target needs a correction")
        out = self.history("a.py::target", "--project-root", str(parent))
        self.assertFalse(out["results"])
        self.recording(sid="scoped", root=parent, path="unrelated.py", request="repo/a.py target needs a correction")
        out = self.history("a.py::target", "--project-root", str(parent))
        self.assertEqual({r["evidence"]["session"] for r in out["results"]}, {"scoped"})
        self.assertEqual(out["results"][0]["category"], "text_mention")

    def test_current_session_and_copied_results_excluded(self):
        self.recording(sid="current")
        echo = {"type": "message", "id": "echo", "message": {"role": "assistant", "content":
                'Copied {"schema_version":1,"target":"a.py::target"}'}}
        self.recording(sid="past", path="unrelated.py", content="x=1\n", extra=[echo])
        out = self.history("a.py", "--exclude-session", "current")
        self.assertFalse(out["results"])
        self.assertEqual(out["coverage"]["sessions"]["recordings_excluded"], 1)

    def test_later_correction_is_retrievable_without_keyword_in_request(self):
        self.recording(sid="first", request="Reject the eager approach")
        self.recording(sid="later", request="The earlier constraint is gone; use eager execution")
        out = self.history()
        self.assertEqual({r["evidence"]["session"] for r in out["results"]}, {"first", "later"})
        self.assertEqual(out["coverage"]["sessions"]["independent_sessions"], 2)

    def test_no_graph_scans_or_broad_find_continuations(self):
        self.write("b.py", "def target():\n    return 3\n")
        with patch("helper.target_history.candidate_files", side_effect=AssertionError("scan")):
            for args in [("map", "a.py"), ("neighbors", "a.py"), ("impact", "a.py")]:
                self.assertIn("history", self.run_command(*args))
            broad = self.run_command("find", "target")
            self.assertNotIn("history", broad)
            self.assertFalse(any("history" in r for r in broad["results"]))
            single = self.run_command("find", "a.py", "--kind", "file")
            self.assertIn("history", single)

    def test_history_delivery_never_saves_transcript(self):
        self.recording(request="UNIQUE_PRIVATE_REQUEST")
        out = self.cli("history", "a.py::target", "--recordings", str(self.archive), "--max-bytes", "40000")
        self.assertTrue(out["results"])
        self.assertNotIn("report", out)
        retained = list(self.cache.rglob("report-*.json"))
        self.assertEqual(retained, [])
        self.assertFalse(any("UNIQUE_PRIVATE_REQUEST" in p.read_text() for p in self.cache.rglob("*.json")))
        small = self.cli("history", "a.py", "--recordings", str(self.archive), "--max-bytes", "2048")
        self.assertEqual(small["status"], "partial")

    def test_exact_file_avoids_full_graph_capture(self):
        with patch("remember.get_graph", side_effect=AssertionError("full graph")):
            self.assertEqual(self.history("a.py")["target"]["path"], "a.py")
            self.assertEqual(self.history("a.py::target")["target"]["qualname"], "target")

    def test_duplicate_anchor_is_ambiguous_even_at_original_position(self):
        self.assertEqual(anchor(["return 2"], 1, ["return 2", "return 2"]), ("ambiguous", None))
        self.assertEqual(anchor(["return 2"], 1, ["return 2"]), ("exact", 0))
        self.assertEqual(anchor(["return 2"], 1, ["pass", "return 2"]), ("shifted", 1))

    def test_commit_witness_preserves_basis_and_expansion(self):
        self.init()
        sha = self.git("rev-parse", "HEAD")
        extra = [{"type": "message", "id": "commit", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "gc", "name": "bash", "arguments": {"command": "git commit -m initial"}}]}},
            {"type": "message", "id": "committed", "parentId": "commit", "message": {"role": "toolResult",
             "toolCallId": "gc", "toolName": "bash", "isError": False,
             "content": [{"type": "text", "text": f"[main {sha[:8]}] initial"}]}}]
        self.recording(extra=extra)
        out = self.history()
        witnesses = [r for r in out["results"] if r["category"] == "commit_witness"]
        self.assertEqual(len(witnesses), 1)
        self.assertEqual(witnesses[0]["evidence"]["sha"], sha)
        self.assertEqual(witnesses[0]["evidence"]["confidence"], "recorded")
        self.assertFalse(witnesses[0]["evidence"]["failed"])
        self.assertTrue(witnesses[0]["next"])

    def test_hunk_matches_pinned_text_not_changed_live_lines(self):
        mapped = self.run_command("map", "a.py::target")
        ref = mapped["history"]["next"]["target"]
        self.write("a.py", "def unrelated():\n    return 9\n")
        edit_call = {"type": "message", "id": "a", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c", "name": "edit", "arguments": {"path": "a.py", "edits": []}}]}}
        edit_result = {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult",
            "toolCallId": "c", "toolName": "edit", "isError": False,
            "details": {"patch": "@@ -1,2 +1,2 @@\n def target():\n-    return 1\n+    return 2\n"},
            "content": [{"type": "text", "text": "done"}]}}
        path = self.archive / "edit.jsonl"
        path.write_text("".join(json.dumps(r)+"\n" for r in [
            {"type": "session", "id": "past", "cwd": str(self.repo)}, edit_call, edit_result]))
        pinned = self.history(ref)
        lead = next(r for r in pinned["results"] if r["basis"] == "recorded_text_matches_target_source")
        self.assertEqual(lead["hunks"][0]["anchor"], "exact")
        live = self.history("a.py::unrelated")
        self.assertFalse(any(r["basis"] == "recorded_text_matches_target_source" for r in live["results"]))
        self.assertTrue(any(r["basis"] == "symbol -> file" for r in live["results"]))

    def test_deleted_file_and_scan_limit_are_honest(self):
        self.init()
        self.git("rm", "a.py")
        self.commit("delete")
        self.recording()
        out = self.history("a.py", "--max-scan-bytes", "100")
        self.assertEqual(out["target"]["resolution"], "historical_path")
        self.assertTrue(any(r["category"] == "git_change" for r in out["results"]))
        self.assertEqual(out["coverage"]["sessions"]["state"], "partial")


if __name__ == "__main__":
    unittest.main()
