"""Transient operation/checkpoint facts and structured commit witnesses."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from helper import commit_links, operation_facts
from helper.git_resolve import GitResolver
from helper.options import ScanOptions
from helper.reader import scan_file
from helper.records import RecordCache, new_scope

FIXTURE = ROOT / "tests/fixtures/recordings/pi-slice1.jsonl"


def recording():
    return scan_file(FIXTURE, ScanOptions(harness="pi", max_text_bytes=1024 * 1024), new_scope())


class OperationFactTests(unittest.TestCase):
    def setUp(self):
        self.recording = recording()
        self.rows, self.coverage = operation_facts.operations([self.recording], RecordCache())

    def test_multi_path_plan_snapshot_is_one_operation(self):
        plan = [row for row in self.rows if row["action"] == "plan_snapshot"]
        self.assertEqual(len(plan), 2)
        self.assertEqual(len({row["operation_id"] for row in plan}), 1)
        self.assertEqual([row["association"] for row in plan], [0, 1])
        self.assertEqual({row["path"] for row in plan}, {"src/app.py", "src/other.py"})
        self.assertTrue(all(row["basis"] == "planning_snapshot" for row in plan))

    def test_unknown_write_keeps_unknown_outcome_and_no_edit_claim(self):
        write = next(row for row in self.rows if row["tool"] == "write")
        self.assertEqual(write["outcome"], "unknown")
        self.assertEqual(write["pairing"], "call_without_result")
        self.assertEqual(write["operation_id"], write["association_key"].rsplit(":", 1)[0])
        self.assertFalse(self.coverage["complete"])

    def test_structured_tools_are_distinct_from_native_operations(self):
        commit = next(row for row in self.rows if row["action"] == "commit_plan" and row["outcome"] == "succeeded")
        self.assertEqual(commit["interpretation"], "structured")
        self.assertEqual(commit["adapter"], "pi:git_commit_plan")
        unknown = next(row for row in self.rows if row["tool"] == "mystery")
        self.assertEqual(unknown["interpretation"], "generic")
        self.assertEqual(self.coverage["unsupported_tools"], {"mystery": 1})

    def test_checkpoint_boundaries_resolve_by_lane_not_publication_time(self):
        rows, coverage = operation_facts.checkpoints([self.recording], RecordCache())
        ok = next(row for row in rows if row["coverage_end_id"] == "u1")
        stale = next(row for row in rows if row["coverage_end_id"] == "u2")
        self.assertEqual(ok["boundary_status"], "resolved")
        self.assertEqual(ok["boundary_order"], 1)
        self.assertEqual(stale["boundary_status"], "off_branch")
        self.assertLess(stale["boundary_order"], stale["order"])
        self.assertEqual(coverage["resolved"], 1)
        self.assertEqual(coverage["off_branch"], 1)


    def test_missing_commit_result_is_not_inferred_from_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.jsonl"
            rows = [
                {"type": "session", "id": "missing", "cwd": temp},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Commit it"}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "git_commit_plan",
                     "arguments": {"subject": "Plan", "sha": "d" * 40}}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            rows = commit_links.witnesses([recording], GitResolver(), RecordCache(), ROOT)
            self.assertTrue(rows)
            self.assertIsNone(rows[0]["sha"])
            self.assertIsNone(rows[0]["recorded_sha"])
            self.assertEqual(rows[0]["confidence"], "unresolved")

    def test_missing_and_unsupported_checkpoint_boundaries_stay_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.jsonl"
            rows = [
                {"type": "session", "id": "checkpoint", "cwd": temp},
                {"type": "shadow-compact.checkpoint", "id": "cp-missing", "coverageEndEntryId": "absent",
                 "structured": {"version": 1, "objective": "Missing boundary"}},
                {"type": "shadow-compact.checkpoint", "id": "cp-invalid", "structured": "not an object"},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts, coverage = operation_facts.checkpoints([recording], RecordCache())
            self.assertEqual({row["boundary_status"] for row in facts}, {"missing", "invalid"})
            self.assertGreaterEqual(coverage["unsupported"], 1)
            self.assertFalse(coverage["complete"])
    def test_realistic_custom_checkpoint_variants_are_interpreted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoints.jsonl"
            rows = [
                {"type": "session", "id": "variants", "cwd": temp},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Boundary"}},
                {"type": "custom", "id": "cp-v3", "parentId": "u", "customType": "shadow-compact.checkpoint",
                 "data": {"version": 3, "summary": "legacy rendering",
                          "structured": {"objective": "Current shape"},
                          "coverage": {"coverageEndEntryId": "u"}}},
                {"type": "custom", "id": "cp-v1", "parentId": "u", "customType": "shadow-compact.checkpoint.v1",
                 "data": {"version": 1, "summary": "No coverage yet"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts, coverage = operation_facts.checkpoints([recording], RecordCache())
            self.assertEqual(len(facts), 2)
            self.assertEqual([row["coverage_end_id"] for row in facts], ["u", None])
            self.assertEqual(facts[0]["boundary_status"], "resolved")
            self.assertEqual(facts[0]["structured"], {"objective": "Current shape"})
            self.assertEqual(facts[1]["boundary_status"], "invalid")
            self.assertEqual(facts[1]["structured"], "No coverage yet")

    def test_realistic_git_record_details_are_commit_witnesses(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "record.jsonl"
            call = {"type": "message", "id": "a", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "ok", "name": "git_record",
                 "arguments": {"subject": "Record it", "paths": ["src/app.py"]}}]}}
            success = {"type": "message", "id": "r-ok", "parentId": "a", "message": {
                "role": "toolResult", "toolCallId": "ok", "isError": False,
                "content": [{"type": "text", "text": "Created 123456789abc: Record it (1 path)."}],
                "details": {"sha": "d" * 40, "subject": "Record it"}}}
            failure = {"type": "message", "id": "r-fail", "parentId": "fail", "message": {
                "role": "toolResult", "toolCallId": "fail", "isError": True,
                "content": [{"type": "text", "text": "fatal: cannot commit"}], "details": {}}}
            rows = [
                {"type": "session", "id": "record", "cwd": temp},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Record"}},
                {"type": "message", "id": "fail", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "fail", "name": "git_record", "arguments": {"subject": "Fail"}}]}},
                call, success, failure,
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts, coverage = operation_facts.operations([recording], RecordCache())
            self.assertEqual(coverage["commit_records"], 2)
            self.assertTrue(any(row["action"] == "commit_record" for row in facts))
            witnesses = commit_links.witnesses([recording], GitResolver(), RecordCache(), ROOT)
            ok = next(row for row in witnesses if row["recorded_sha"] == "d" * 40)
            bad = next(row for row in witnesses if row["recorded_sha"] is None and row["failed"])
            self.assertTrue(ok["basis"].startswith("structured_commit_record"))
            self.assertFalse(ok["failed"])
            self.assertEqual(ok["subject"], "Record it")
            self.assertIsNone(bad["sha"])

    def test_codex_multi_path_associations_keep_unique_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex-multi.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"id": "codex", "cwd": temp}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "UserMessage", "content": [{"type": "text", "text": "Change both"}]}}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "FileChange", "status": "completed", "changes": {
                        "a.py": {"type": "add", "content": "x=1\n"},
                        "b.py": {"type": "add", "content": "y=1\n"},
                        "c.py": {"type": "add", "content": "z=1\n"}}}}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="codex"), new_scope())
            facts, coverage = operation_facts.operations([recording], RecordCache())
            self.assertEqual(len(facts), 3)
            self.assertEqual(len({row["association_key"] for row in facts}), 3)
            self.assertEqual({row["operation_id"] for row in facts}, {facts[0]["operation_id"]})
            self.assertEqual({row["outcome"] for row in facts}, {"succeeded"})

    def test_checkpoint_boundary_resolves_by_ancestry_across_exchanges(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cross.jsonl"
            rows = [
                {"type": "session", "id": "cross", "cwd": temp},
                {"type": "message", "id": "u1", "message": {"role": "user", "content": "Open"}},
                {"type": "message", "id": "a1", "parentId": "u1", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "a.py"}},
                    {"type": "toolCall", "id": "c2", "name": "read", "arguments": {"path": "b.py"}}]}},
                {"type": "message", "id": "u2", "parentId": "a1", "message": {"role": "user", "content": "Follow up"}},
                {"type": "message", "id": "a2", "parentId": "u2", "message": {"role": "assistant", "content": "Done"}},
                {"type": "shadow-compact.checkpoint", "id": "cp", "parentId": "a2", "coverageEndEntryId": "a1",
                 "structured": {"version": 1, "objective": "Cross"}},
                {"type": "shadow-compact.checkpoint", "id": "cp-self", "parentId": "a2",
                 "coverageEndEntryId": "cp-self", "structured": {"version": 1}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts, coverage = operation_facts.checkpoints([recording], RecordCache())
            cross = next(row for row in facts if row["coverage_end_id"] == "a1")
            self.assertEqual(cross["boundary_status"], "resolved")
            a1_orders = [event["order"] for event in recording["events"]
                         if event.get("external_message_id") == "a1"]
            self.assertEqual(cross["boundary_items"], len(a1_orders))
            self.assertEqual(cross["boundary_order"], max(a1_orders))
            own = next(row for row in facts if row["coverage_end_id"] == "cp-self")
            self.assertEqual(own["boundary_status"], "invalid")
            self.assertEqual(coverage["resolved"], 1)
            self.assertEqual(coverage["invalid"], 1)

    def test_duplicate_boundary_ids_are_ambiguous_not_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dups.jsonl"
            rows = [
                {"type": "session", "id": "dups", "cwd": temp},
                {"type": "message", "id": "dup", "message": {"role": "user", "content": "One"}},
                {"type": "message", "id": "dup", "message": {"role": "user", "content": "Two"}},
                {"type": "message", "id": "a", "parentId": "dup", "message": {"role": "assistant", "content": "A"}},
                {"type": "shadow-compact.checkpoint", "id": "cp", "parentId": "a", "coverageEndEntryId": "dup",
                 "structured": {"version": 1}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts, coverage = operation_facts.checkpoints([recording], RecordCache())
            self.assertEqual(facts[0]["boundary_status"], "ambiguous")
            self.assertIsNone(facts[0]["boundary_ref"])
            self.assertFalse(coverage["complete"])

    def test_missing_shell_result_cannot_infer_a_commit_by_subject(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "A"], check=True)
            (repo / "a.py").write_text("x\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Record it"], check=True)
            path = Path(temp) / "missing-result.jsonl"
            rows = [
                {"type": "session", "id": "missing", "cwd": str(repo)},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Commit it"}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "bash",
                     "arguments": {"command": "git commit -m 'Record it'"}}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts = commit_links.witnesses([recording], GitResolver(), RecordCache(), str(repo))
            self.assertTrue(facts)
            witness = facts[0]
            self.assertIsNone(witness["sha"])
            self.assertEqual(witness["confidence"], "unresolved")
            self.assertEqual(witness["resolution"], "missing_result")
            self.assertEqual(witness["observation"], "missing_result")
            self.assertEqual(witness["order"], 2)
            self.assertEqual(witness["lane"], "u")
            self.assertEqual(witness["branch"], "a")

    def test_structured_base_and_parent_shas_are_not_new_commit_witnesses(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "base.jsonl"
            rows = [
                {"type": "session", "id": "base", "cwd": temp},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Commit"}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "git_commit_plan", "arguments": {"subject": "Plan"}}]}},
                {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult",
                 "toolCallId": "c", "isError": False, "content": [{"type": "text", "text": json.dumps(
                     {"base": "a" * 40, "parent": "c" * 40, "commits": []})}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts = commit_links.witnesses([recording], GitResolver(), RecordCache(), str(Path(temp)))
            self.assertEqual(len(facts), 1)
            self.assertIsNone(facts[0]["recorded_sha"])
            self.assertIsNone(facts[0]["sha"])
            self.assertEqual(facts[0]["observation"], "unsupported_result")

    def test_compound_failure_preserves_a_directly_observed_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "A"], check=True)
            (repo / "a.py").write_text("x\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Record it"], check=True)
            sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                 text=True, check=True).stdout.strip()
            path = Path(temp) / "compound.jsonl"
            rows = [
                {"type": "session", "id": "compound", "cwd": str(repo)},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Commit then fail"}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "bash",
                     "arguments": {"command": "git commit -m 'Record it' && false"}}]}},
                {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult",
                 "toolCallId": "c", "isError": True,
                 "content": [{"type": "text", "text": f"[main {sha}] Record it\n1 file changed"}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts = commit_links.witnesses([recording], GitResolver(), RecordCache(), str(repo))
            witnessed = next(row for row in facts if row["sha"] == sha)
            self.assertTrue(witnessed["failed"])
            self.assertEqual(witnessed["confidence"], "recorded")
            self.assertEqual(witnessed["observation"], "direct_result_in_failed_command")
            self.assertEqual(witnessed["resolution"], "failed")
            self.assertFalse(any(row["observation"] == "direct_result" and row["failed"] for row in facts))

    def test_real_pi_commit_details_commits_are_witnesses(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "details-commits.jsonl"
            rows = [
                {"type": "session", "id": "details", "cwd": temp},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Commit both"}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "git_commit_plan", "arguments": {"subject": "Plan"}}]}},
                {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult",
                 "toolCallId": "c", "isError": False,
                 "content": [{"type": "text", "text": "Recorded 2 commits: abcdef123456 (one), 123456abcdef (two)."}],
                 "details": {"base": "9" * 40, "order": ["one", "two"], "commits": [
                     {"id": "one", "sha": "a" * 40, "subject": "One", "paths": ["a.py"]},
                     {"id": "two", "sha": "b" * 40, "subject": "Two", "paths": ["b.py"]}],
                     "stillDirty": False}}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts = commit_links.witnesses([recording], GitResolver(), RecordCache(), str(Path(temp)))
            shas = {row["sha"] for row in facts}
            self.assertEqual(shas, {"a" * 40, "b" * 40})
            self.assertNotIn("9" * 40, shas)
            self.assertEqual({row["subject"] for row in facts}, {"One", "Two"})
            self.assertTrue(all(row["confidence"] == "recorded" for row in facts))
            self.assertTrue(all(row["observation"] == "direct_result" for row in facts))

    def test_structured_commit_result_is_a_witness_but_failure_is_not(self):
        rows = commit_links.witnesses([self.recording], GitResolver(), RecordCache(), ROOT)
        success = next(row for row in rows if row["recorded_sha"] == "b" * 40)
        failed = next(row for row in rows if row["recorded_sha"] == "c" * 40)
        self.assertEqual(success["sha"], "b" * 40)
        self.assertIn(success["basis"], ("structured_commit_result", "structured_commit_result+repo"))
        self.assertFalse(success["failed"])
        self.assertIsNone(failed["sha"])
        self.assertTrue(failed["failed"])
        self.assertEqual(success["relation"], "commit")
        self.assertIsNotNone(success["witness_id"])


if __name__ == "__main__":
    unittest.main()
