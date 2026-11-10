"""Branch-aware trace episodes, checkpoint reconciliation and linked Git evidence."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from helper import continuations, operations, reconciliation
from helper.options import ScanOptions
from helper.reader import scan_file
from helper.records import new_scope
from support import invoke

FIXTURE = ROOT / "tests/fixtures/recordings/pi-trace.jsonl"


def scan(path=FIXTURE):
    return scan_file(Path(path), ScanOptions(harness="pi", max_text_bytes=1024 * 1024), new_scope())


def refs(path=FIXTURE):
    recording = scan(path)
    return {event.get("external_message_id"): json.dumps(event["ref"], separators=(",", ":"))
            for event in recording["events"] if event.get("external_message_id")}


def event_id(name, path=FIXTURE):
    recording = scan(path)
    return next(event["event_id"] for event in recording["events"]
                if event.get("external_message_id") == name)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class TraceFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text("def main():\n    return 2\n")
        self.cache = self.home / "cache"
        self.refs = refs()

    def cli(self, *args, ok=True, timeout=60):
        result = invoke([*map(str, args), "--repo", str(self.repo), "--cache", str(self.cache)])
        self.assertEqual(result.returncode, 0 if ok else 2, result.stdout + result.stderr)
        return result.json()

    def trace(self, name, *extra, ok=True):
        return self.cli("trace", self.refs[name], "--recordings", str(FIXTURE), *extra, ok=ok)

    def test_trace_is_a_registered_common_operation(self):
        self.assertIn("trace", operations.COMMON)
        self.assertIn("trace", continuations.ALLOWED)
        self.assertEqual(operations.OPERATIONS["trace"].route, "runtime")

    def test_event_trace_separates_branches_and_keeps_request(self):
        out = self.trace("r-commit", "--max-bytes", "40000")
        self.assertEqual(out["anchor"]["kind"], "event")
        request = next(row for row in out["results"] if row["section"] == "request")
        self.assertTrue(request["text"].startswith("Requirement: keep lock ordering"))
        self.assertEqual(request["origin"], "human")
        lanes = {lane["lane"]: lane["lane_role"] for lane in out["lanes"]}
        self.assertEqual(lanes.get("u1"), "request")
        self.assertEqual(lanes.get("a1"), "agent")
        self.assertEqual(lanes.get("a-side"), "fork")
        self.assertNotIn("a2", lanes)
        operations_rows = [row for row in out["results"] if row["section"] == "operation"]
        edit = next(row for row in operations_rows if row["action"] == "file_edit"
                    and row["paths"] == ["src/app.py"] and row["outcome"] == "succeeded")
        self.assertEqual(edit["basis"], "recorded_tool")
        side = next(row for row in operations_rows if row["paths"] == ["src/config.py"])
        self.assertEqual(side["outcome"], "failed")
        dims = out["dimensions"]
        self.assertEqual(dims["requested"]["evidence_presence"], "observed")
        self.assertEqual(dims["edited"]["evidence_presence"], "observed")
        self.assertEqual(dims["verified"]["evidence_presence"], "observed")
        self.assertEqual(dims["committed"]["evidence_presence"], "observed")
        self.assertEqual(dims["pushed"]["evidence_presence"], "unknown")
        self.assertEqual(dims["live_confirmed"]["evidence_presence"], "unknown")

    def test_table_and_fields_render_the_episode(self):
        result = invoke(["trace", self.refs["r-commit"], "--recordings", str(FIXTURE), "--format", "table",
                         "--max-bytes", "40000", "--repo", str(self.repo), "--cache", str(self.cache)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("section", result.stdout)
        self.assertIn("operation", result.stdout)
        out = self.trace("r-commit", "--fields", "section,outcome", "--max-bytes", "40000")
        self.assertTrue(out["results"])
        self.assertTrue(all(set(row) <= {"section", "outcome"} for row in out["results"]))

    def test_every_operation_expands_through_show(self):
        out = self.trace("r-commit", "--max-bytes", "40000")
        operation = next(row for row in out["results"] if row["section"] == "operation")
        shown = self.cli("show", json.dumps(operation["call_ref"]))
        self.assertTrue(any(row.get("selected") for row in shown["results"]))
        self.assertTrue(any(row.get("kind") == "user_message" for row in shown["results"]))

    def verified(self):
        return mock.patch("helper.git_resolve.GitResolver.verify", return_value=("true", "true"))

    def test_stale_checkpoint_is_a_later_observation_not_current_state(self):
        with self.verified():
            out = self.trace("cp-late", "--max-bytes", "40000")
        reconciliation = out["reconciliation"]
        comparison = next(row for row in reconciliation["comparisons"]
                          if row["checkpoint_id"].endswith(event_id("cp-late"))
                          and row["dimension"] == "committed")
        self.assertEqual(comparison["relation"], "later_observation")
        self.assertEqual(comparison["reason"], "observation_after_coverage_boundary")
        self.assertEqual(comparison["observation_value"], "b" * 40)
        self.assertEqual(out["dimensions"]["committed"]["evidence_presence"], "observed")
        checkpoint = next(row for row in reconciliation["checkpoints"]
                          if row["checkpoint_id"].endswith(event_id("cp-late")))
        self.assertEqual(checkpoint["boundary_status"], "resolved")
        self.assertGreaterEqual(checkpoint["coverage_gap"]["operations"], 2)
        self.assertIn("b" * 40, checkpoint["coverage_gap"]["commits"])
        self.assertNotIn("completed", out["dimensions"])
        self.assertTrue(all(value["evidence_presence"] != "completed" for key, value in out["dimensions"].items()
                            if key != "scope"))

    def test_unverified_recorded_sha_never_contradicts_a_claim(self):
        out = self.trace("cp-contra", "--max-bytes", "40000")
        self.assertFalse([row for row in out["reconciliation"]["comparisons"]
                          if row["checkpoint_id"].endswith(event_id("cp-contra"))])
        committed = [row for row in out["results"] if row.get("section") == "observation"
                     and row.get("dimension") == "committed" and row.get("value") == "b" * 40]
        self.assertTrue(committed)
        self.assertEqual({row["evidence_kind"] for row in committed}, {"tool_observation"})
        self.assertEqual({row["object_resolved"] for row in committed}, {"unknown"})

    def test_covered_commit_contradicts_an_uncommitted_claim(self):
        with self.verified():
            out = self.trace("cp-contra", "--max-bytes", "40000")
        comparison = next(row for row in out["reconciliation"]["comparisons"]
                          if row["checkpoint_id"].endswith(event_id("cp-contra")))
        self.assertEqual(comparison["relation"], "contradicts")
        self.assertEqual(comparison["reason"], "observation_inside_covered_range")

    def test_checkpoint_anchor_crosses_exchanges_without_inventing_contradiction(self):
        out = self.trace("cp-cross", "--max-bytes", "40000")
        request = next(row for row in out["results"] if row["section"] == "request")
        self.assertTrue(request["text"].startswith("No, revert that"))
        checkpoint = next(row for row in out["reconciliation"]["checkpoints"]
                          if row["checkpoint_id"].endswith(event_id("cp-cross")))
        self.assertEqual(checkpoint["coverage_gap"]["status"], "observed")
        self.assertIn("b" * 40, checkpoint["coverage_gap"]["commits"])
        self.assertFalse([row for row in out["reconciliation"]["comparisons"]
                          if row["checkpoint_id"].endswith(event_id("cp-cross"))])

    def test_followup_is_a_new_exchange_in_the_same_conversation_lane(self):
        out = self.trace("r-test2", "--max-bytes", "40000")
        request = next(row for row in out["results"] if row["section"] == "request")
        self.assertTrue(request["text"].startswith("No, revert that"))
        self.assertEqual({lane["lane"] for lane in out["lanes"]}, {"u2", "a2"})
        self.assertEqual(out["dimensions"]["verified"]["evidence_presence"], "observed")

    def test_focused_scan_does_not_read_the_archive_or_build_the_graph(self):
        out = self.trace("r-commit", "--max-bytes", "40000")
        recordings = out["coverage"]["recordings"]
        self.assertEqual(recordings["files_scanned"], 1)
        self.assertEqual(recordings["scope"], "selected recording")
        self.assertNotIn("graph", out["coverage"])
        self.assertEqual(out["scope"]["retention"], "transient")

    def test_orphan_result_anchor_stays_unpaired_and_visible(self):
        path = self.home / "orphan-result.jsonl"
        rows = [
            {"type": "session", "id": "orphan-result", "cwd": str(self.repo)},
            {"type": "message", "id": "u", "message": {"role": "user", "content": "Check the output"}},
            {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": "src/app.py"}}]}},
            {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult",
             "toolCallId": "missing", "isError": True,
             "content": [{"type": "text", "text": "No matching call"}]}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        out = self.cli("trace", refs(path)["r"], "--recordings", str(path), "--max-bytes", "40000")
        operation = next(row for row in out["results"] if row["section"] == "operation"
                         and row["pairing"] == "result_without_call")
        self.assertEqual(operation["outcome"], "failed")
        self.assertFalse(out["coverage"]["operations"]["complete"])
        self.assertEqual(out["status"], "partial")

    def test_missing_ancestry_is_an_unassigned_fragment(self):
        path = self.home / "orphan.jsonl"
        rows = [
            {"type": "session", "id": "orphan", "cwd": str(self.repo)},
            {"type": "message", "id": "u", "message": {"role": "user", "content": "Unrelated request"}},
            {"type": "message", "id": "a", "parentId": "ghost", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": str(self.repo / "src/app.py")}}]}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        anchor = refs(path)["a"]
        out = self.cli("trace", anchor, "--recordings", str(path), "--max-bytes", "40000")
        self.assertFalse([row for row in out["results"] if row["section"] == "request"])
        self.assertEqual(out["coverage"]["request"]["basis"], "unresolved")
        self.assertTrue([row for row in out["results"] if row["section"] == "gap"
                         and row["area"] == "request"])

    def test_bare_prefix_is_not_a_ref(self):
        out = self.cli("trace", "bbbbbbbb", "--recordings", str(FIXTURE), ok=False)
        self.assertIn("JSON ref or a cite token", out["error"])

    def test_tiny_budget_keeps_dimensions_and_executable_continuations(self):
        out = self.trace("r-commit", "--max-bytes", "12000")
        self.assertEqual(set(out["dimensions"]) - {"scope"}, set(reconciliation.DIMENSIONS))
        self.assertTrue(out["delivery"]["complete"] is False)
        self.assertTrue(out["next"])
        tokens = [item for item in out["next"] if item.get("ref", "").startswith("continue:")]
        self.assertTrue(any(item.get("effect") == "delivery" for item in tokens))
        page = self.cli("show", next(item["ref"] for item in tokens if item.get("effect") == "delivery"))
        self.assertGreater(page["delivery"]["offset"], 0)

    def test_expansion_tokens_execute_without_reconstructing_flags(self):
        out = self.trace("r-commit", "--max-bytes", "40000")
        expansions = [item for item in out["next"] if item.get("effect") == "expansion"]
        self.assertTrue(expansions)
        widen = next(item for item in expansions if continuations.decode(item["ref"])["argv"][0] == "trace")
        nested = self.cli("show", widen["ref"], "--max-bytes", "40000")
        self.assertEqual(nested["operation"], "trace")
        self.assertTrue(nested["results"])
        deepen = next(item for item in expansions if continuations.decode(item["ref"])["argv"][0] == "show")
        shown = self.cli("show", deepen["ref"])
        self.assertTrue(shown["results"])

    def test_malformed_ref_is_rejected(self):
        bad = json.dumps({"file": str(FIXTURE), "offset": 0, "length": 10, "sha256": "x", "item": "zero"})
        out = self.cli("trace", bad, "--recordings", str(FIXTURE), ok=False)
        self.assertIn("nonnegative integer item", out["error"])

    def test_trace_writes_no_cache(self):
        self.trace("r-commit", "--max-bytes", "12000")
        self.assertFalse(self.cache.exists())

    def test_changed_recording_rejects_a_signed_page(self):
        copy = self.home / "copy.jsonl"
        shutil.copyfile(FIXTURE, copy)
        out = self.cli("trace", refs(copy)["r-commit"], "--recordings", str(copy), "--max-bytes", "12000")
        token = next(item["ref"] for item in out["next"] if item.get("effect") == "delivery")
        with copy.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "message", "id": "new", "parentId": "u1",
                                     "message": {"role": "assistant", "content": [
                                         {"type": "text", "text": "later evidence"}]}}) + "\n")
        rejected = self.cli("show", token, ok=False)
        self.assertIn("Continuation source changed", rejected["error"])


class CommitTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text("v1\n")
        (self.repo / "src/other.py").write_text("x\n")
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "a@b.c")
        git(self.repo, "config", "user.name", "A")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "initial")
        (self.repo / "src/app.py").write_text("v2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Keep lock ordering")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.recs = self.home / "recs"
        self.recs.mkdir()
        self.cache = self.home / "cache"
        self.write_recordings()

    def session(self, name, rows):
        (self.recs / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def write_recordings(self):
        repo = str(self.repo)
        self.session("A", [
            {"type": "session", "id": "A", "cwd": repo},
            {"type": "message", "id": "a-u", "message": {"role": "user", "content": "Requirement: keep lock ordering in src/app.py."}},
            {"type": "message", "id": "a-a", "parentId": "a-u", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "a-read", "name": "read", "arguments": {"path": f"{repo}/src/app.py"}}]}},
            {"type": "message", "id": "a-r", "parentId": "a-a", "message": {"role": "toolResult",
             "toolCallId": "a-read", "isError": False, "content": [{"type": "text", "text": "v1"}]}},
        ])
        self.session("B", [
            {"type": "session", "id": "B", "cwd": repo},
            {"type": "message", "id": "b-u", "message": {"role": "user", "content": "Implement it."}},
            {"type": "message", "id": "b-a", "parentId": "b-u", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "b-edit", "name": "edit", "arguments": {
                    "path": f"{repo}/src/app.py", "oldText": "v1", "newText": "v2"}},
                {"type": "toolCall", "id": "b-commit", "name": "git_commit_plan",
                 "arguments": {"subject": "Keep lock ordering"}}]}},
            {"type": "message", "id": "b-r1", "parentId": "b-a", "message": {"role": "toolResult",
             "toolCallId": "b-edit", "isError": False, "content": [{"type": "text", "text": "edited"}]}},
            {"type": "message", "id": "b-r2", "parentId": "b-r1", "message": {"role": "toolResult",
             "toolCallId": "b-commit", "isError": False, "content": [{"type": "text", "text": json.dumps(
                 {"commits": [{"sha": self.sha, "subject": "Keep lock ordering"}]})}]}},
        ])
        self.session("C", [
            {"type": "session", "id": "C", "cwd": repo},
            {"type": "message", "id": "c-u", "message": {"role": "user", "content": "Nearby but unrelated."}},
            {"type": "message", "id": "c-a", "parentId": "c-u", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "c-edit", "name": "edit", "arguments": {
                    "path": f"{repo}/src/third.py", "oldText": "", "newText": "n"}}]}},
            {"type": "message", "id": "c-r", "parentId": "c-a", "message": {"role": "toolResult",
             "toolCallId": "c-edit", "isError": False, "content": [{"type": "text", "text": "edited"}]}},
        ])
        self.session("D", [
            {"type": "session", "id": "D", "cwd": repo},
            {"type": "message", "id": "d-u", "message": {"role": "user", "content": "Unrelated change."}},
            {"type": "message", "id": "d-a", "parentId": "d-u", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "d-edit", "name": "edit", "arguments": {
                    "path": f"{repo}/src/other.py", "oldText": "x", "newText": "y"}}]}},
            {"type": "message", "id": "d-r", "parentId": "d-a", "message": {"role": "toolResult",
             "toolCallId": "d-edit", "isError": False, "content": [{"type": "text", "text": "edited"}]}},
        ])

    def cli(self, *args, ok=True, recordings=None, context=True):
        extra = (["--recordings", str(recordings or self.recs), "--project-root", str(self.repo)]
                 if context else [])
        result = invoke([*map(str, args), "--repo", str(self.repo), "--cache", str(self.cache), *extra,
                         "--max-bytes", "60000"])
        self.assertEqual(result.returncode, 0 if ok else 2, result.stdout + result.stderr)
        return result.json()

    def test_commit_trace_keeps_witness_lanes_and_path_context(self):
        out = self.cli("trace", self.sha)
        self.assertEqual(out["anchor"]["kind"], "commit")
        self.assertEqual(out["anchor"]["subject"], "Keep lock ordering")
        self.assertEqual(out["dimensions"]["committed"]["evidence_presence"], "observed")
        self.assertEqual({lane["lane_role"] for lane in out["lanes"]}, {"commit_witness"})
        related = [row for row in out["results"] if row["section"] == "related"]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["lane"], "a-u")
        self.assertEqual(related[0]["basis"], "path_overlap")
        self.assertIn("not proof", related[0]["meaning"])
        self.assertFalse([row for row in related if row["recording_id"].endswith(("C.jsonl", "D.jsonl"))])
        self.assertEqual(out["coverage"]["episode"]["witness_recordings"], 1)
        widen = next(item for item in out["next"]
                     if continuations.decode(item["ref"])["argv"][0] == "trace")
        nested = self.cli("show", widen["ref"], "--max-bytes", "60000", context=False)
        self.assertEqual(nested["operation"], "trace")
        self.assertEqual(nested["anchor"]["kind"], "event")

    def test_two_commit_witnesses_in_one_exchange_do_not_replay_rows(self):
        (self.repo / "src/app.py").write_text("v3\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Second change")
        second = git(self.repo, "rev-parse", "HEAD")
        self.session("E", [
            {"type": "session", "id": "E", "cwd": str(self.repo)},
            {"type": "message", "id": "e-u", "message": {"role": "user", "content": "Commit both changes."}},
            {"type": "message", "id": "e-a", "parentId": "e-u", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "e-one", "name": "git_commit_plan",
                 "arguments": {"subject": "Keep lock ordering"}},
                {"type": "toolCall", "id": "e-two", "name": "git_commit_plan",
                 "arguments": {"subject": "Second change"}}]}},
            {"type": "message", "id": "e-r1", "parentId": "e-a", "message": {"role": "toolResult",
             "toolCallId": "e-one", "isError": False, "content": [{"type": "text", "text": json.dumps(
                 {"commits": [{"sha": self.sha, "subject": "Keep lock ordering"}]})}]}},
            {"type": "message", "id": "e-r2", "parentId": "e-r1", "message": {"role": "toolResult",
             "toolCallId": "e-two", "isError": False, "content": [{"type": "text", "text": json.dumps(
                 {"commits": [{"sha": second, "subject": "Second change"}]})}]}},
        ])
        out = self.cli("trace", second)
        self.assertEqual(len([row for row in out["results"] if row["section"] == "request"]), 1)
        commits = [row for row in out["results"] if row["section"] == "commit"]
        self.assertEqual(len(commits), 2)
        self.assertEqual(out["coverage"]["episode"]["witness_recordings"], 1)

    def test_commit_without_a_witness_is_qualified(self):
        empty = self.home / "empty"
        empty.mkdir()
        out = self.cli("trace", self.sha, recordings=empty)
        self.assertEqual(out["status"], "partial")
        self.assertTrue([row for row in out["results"] if row["section"] == "gap"
                         and row["reason"] == "no_recording_witness_in_scanned_scope"])
        self.assertTrue(out["coverage"]["episode"]["none_found_in_scanned_scope"])

    def test_archive_horizon_is_bounded_and_reported(self):
        for index in range(120):
            self.session(f"noise{index}", [
                {"type": "session", "id": f"noise{index}", "cwd": str(self.repo)},
                {"type": "message", "id": f"n{index}-u",
                 "message": {"role": "user", "content": f"Unrelated work {index}"}},
                {"type": "message", "id": f"n{index}-a", "parentId": f"n{index}-u",
                 "message": {"role": "assistant", "content": [
                     {"type": "toolCall", "id": f"n{index}-c", "name": "read",
                      "arguments": {"path": f"{self.repo}/src/other.py"}}]}},
            ])
        out = self.cli("trace", self.sha, "--max-recordings", "25")
        archive = out["coverage"]["archive"]["recordings"]
        self.assertEqual(archive["files_scanned"], 25)
        self.assertTrue(archive["incomplete_reasons"])
        self.assertGreaterEqual(out["coverage"]["targeted_search"]["files_matched"], 1)
        self.assertFalse(out["coverage"]["episode"]["none_found_in_scanned_scope"])
        self.assertEqual(out["status"], "partial")

    def test_older_commit_outside_the_git_window_is_qualified(self):
        (self.repo / "src/other.py").write_text("later\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "later work")
        older = git(self.repo, "rev-parse", "HEAD~1")
        out = self.cli("trace", older, "--max-commits", "1")
        self.assertEqual(out["coverage"]["episode"]["commit_changes"], "outside_git_window")
        self.assertEqual(out["status"], "partial")

    def test_unknown_commit_is_an_error(self):
        out = self.cli("trace", "0" * 40, ok=False)
        self.assertIn("Commit unavailable", out["error"])


if __name__ == "__main__":
    unittest.main()
