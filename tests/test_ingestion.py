"""Ingestion fidelity: exact requests, branch lanes, outcomes, ordering and coverage."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from helper import edit_hunks
from helper.options import ScanOptions
from helper.projection import human
from helper.reader import scan_file
from helper.records import RecordCache, new_scope, records
from helper import operation_facts

FIXTURE = ROOT / "tests/fixtures/recordings/pi-slice1.jsonl"


class FakeRepository:
    repo_id = "fixture"

    def repo_path(self, event):
        return "src/app.py" if event.get("path") == "src/app.py" else None

    def current_lines(self, path):
        return (["def main():", "    return 2"], None)

    def has_items(self, path):
        return False

    def items_for(self, path):
        return []


class IngestionTests(unittest.TestCase):
    def scan(self):
        scope = new_scope()
        return scan_file(FIXTURE, ScanOptions(harness="pi", max_text_bytes=1024 * 1024), scope), scope

    def test_injected_context_does_not_replace_the_human_request(self):
        recording, _ = self.scan()
        by_id = {e.get("external_message_id"): e for e in recording["events"] if e.get("external_message_id")}
        human_request, injected = by_id["u1"], by_id["inj"]
        self.assertTrue(human(human_request))
        self.assertFalse(human(injected))
        self.assertEqual(injected["request_origin"], "human")
        self.assertEqual(injected["request_ref"], human_request["ref"])
        self.assertEqual(injected["turn"], "u1")
        self.assertEqual(injected["turn_basis"], "recorded_ancestry")
        self.assertEqual(injected["lane"], "u1")

    def test_sibling_branch_has_its_own_lane_and_physical_order(self):
        recording, _ = self.scan()
        by_id = {e.get("external_message_id"): e for e in recording["events"] if e.get("external_message_id")}
        self.assertEqual(by_id["u2"]["lane"], "u2")
        self.assertEqual(by_id["a1"]["lane"], "u1")
        orders = [e["order"] for e in recording["events"]]
        self.assertEqual(orders, sorted(orders))
        positions = [tuple(e["position"]) for e in recording["events"]]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(all(e["byte_offset"] >= 0 and e["item_index"] >= 0 for e in recording["events"]))

    def test_unknown_write_and_failed_edit_never_become_hunks(self):
        recording, _ = self.scan()
        hunks, symbols, coverage = edit_hunks.derive([recording], RecordCache(), FakeRepository())
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0]["tool"], "edit")
        self.assertEqual(coverage["unknown_skipped"], 1)
        self.assertEqual(coverage["failed_skipped"], 1)
        self.assertEqual(coverage["unpaired_edit_calls"], 1)
        self.assertTrue(all(h["tool"] != "write" for h in hunks))

    def test_truncated_unified_patch_is_reported_not_replayed(self):
        hunks, truncated = edit_hunks.parse_unified_checked("@@ -1,3 +1,3 @@\n a\n-b\n")
        self.assertEqual(truncated, 1)
        self.assertFalse(edit_hunks.counts_valid(hunks))
        complete, bad = edit_hunks.parse_unified_checked("@@ -1,2 +1,2 @@\n a\n-b\n+c\n")
        self.assertEqual(bad, 0)
        self.assertTrue(edit_hunks.counts_valid(complete))

    def test_empty_interpreted_content_is_counted_not_silently_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.jsonl"
            rows = [
                {"type": "session", "id": "empty", "cwd": temp},
                {"type": "message", "id": "a", "message": {"role": "assistant", "content": []}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            scope = new_scope()
            recording = scan_file(path, ScanOptions(harness="pi"), scope)
            self.assertEqual(recording["events"][0]["kind"], "config")
            self.assertEqual(scope["records_unrepresented"], 1)
            self.assertEqual(scope["interpretation_unsupported"], 0)

    def test_tail_alignment_bytes_are_charged_to_the_scan_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "long.jsonl"
            first = json.dumps({"type": "session", "id": "s", "cwd": temp, "pad": "x" * 300}) + "\n"
            second = json.dumps({"type": "message", "id": "u", "message": {"role": "user", "content": "tail request"}}) + "\n"
            path.write_text(first + second)
            scope = new_scope()
            list(records(path, scope, 512, 256, ranges=[(120, None)]))
            self.assertGreater(scope["tail_alignment_bytes"], 0)
            self.assertGreater(scope["bytes_scanned"], 120)


    def test_claude_structured_edit_pairs_and_projects_one_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "claude.jsonl"
            target = str(Path(temp) / "a.py")
            rows = [
                {"type": "user", "sessionId": "claude", "cwd": temp, "uuid": "u",
                 "message": {"role": "user", "content": "Change the value"}},
                {"type": "assistant", "uuid": "a", "parentUuid": "u", "cwd": temp,
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "id": "c1", "name": "Edit",
                      "input": {"file_path": target, "old_string": "x", "new_string": "y"}}]}},
                {"type": "user", "uuid": "r", "parentUuid": "a", "cwd": temp,
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "c1", "is_error": False, "content": "ok"}]},
                 "toolUseResult": {"structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1,
                                                       "newLines": 1, "lines": ["-x", "+y"]}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            scope = new_scope()
            recording = scan_file(path, ScanOptions(harness="claude-code"), scope)
            facts, coverage = operation_facts.operations([recording], RecordCache())
            self.assertEqual(coverage["paired"], 1)
            self.assertEqual(facts[0]["outcome"], "succeeded")
            self.assertEqual(facts[0]["action"], "file_edit")

    def test_codex_file_change_is_one_operation_per_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"id": "codex", "cwd": temp}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "UserMessage", "content": [{"type": "text", "text": "Change the value"}]}}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "FileChange", "status": "completed", "changes": {
                        "a.py": {"type": "add", "content": "x=1\n"},
                        "b.py": {"type": "add", "content": "y=1\n"}}}}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            scope = new_scope()
            recording = scan_file(path, ScanOptions(harness="codex"), scope)
            facts, coverage = operation_facts.operations([recording], RecordCache())
            self.assertEqual(coverage["operations"], 1)
            self.assertEqual({row["path"] for row in facts}, {"a.py", "b.py"})
            self.assertEqual({row["outcome"] for row in facts}, {"succeeded"})


    def test_child_before_parent_still_resolves_request_after_full_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reordered.jsonl"
            rows = [
                {"type": "session", "id": "reordered", "cwd": temp},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": "a.py"}}]}},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Keep the parent request"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            call = next(event for event in recording["events"] if event.get("tool") == "read")
            self.assertEqual(call["request_origin"], "human")
            self.assertEqual(call["turn"], "u")

    def test_missing_parent_and_cycle_are_reported_not_repaired(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cycle.jsonl"
            rows = [
                {"type": "session", "id": "cycle", "cwd": temp},
                {"type": "message", "id": "u", "parentId": "a", "message": {"role": "user", "content": "Cycle"}},
                {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": "Reply"}},
                {"type": "message", "id": "missing", "parentId": "absent", "message": {"role": "assistant", "content": "Orphan parent"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            scope = new_scope()
            recording = scan_file(path, ScanOptions(harness="pi"), scope)
            self.assertGreaterEqual(scope["ancestry_cycles"], 1)
            self.assertGreaterEqual(scope["ancestry_unknown"], 1)
            missing = next(event for event in recording["events"] if event.get("external_message_id") == "missing")
            self.assertIsNone(missing["turn"])
            self.assertEqual(missing["turn_basis"], "unresolved_ancestry")

    def test_followup_continues_the_lane_and_descendants_cannot_split_off(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "followup.jsonl"
            rows = [
                {"type": "session", "id": "followup", "cwd": temp},
                {"type": "message", "id": "u1", "message": {"role": "user", "content": "Open the exchange"}},
                {"type": "message", "id": "a1", "parentId": "u1", "message": {"role": "assistant", "content": "First branch"}},
                {"type": "message", "id": "u2", "parentId": "a1", "message": {"role": "user", "content": "Follow up"}},
                {"type": "message", "id": "a2", "parentId": "u2", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": "a.py"}}]}},
                {"type": "message", "id": "side", "parentId": "u1", "message": {"role": "assistant", "content": "Sibling branch"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            by_id = {event.get("external_message_id"): event for event in recording["events"]
                     if event.get("external_message_id")}
            self.assertEqual(by_id["u2"]["lane"], "u1")
            self.assertEqual(by_id["u2"]["lane_basis"], "continued_exchange")
            self.assertEqual(by_id["a2"]["lane"], "u1")
            self.assertEqual(by_id["a2"]["branch"], "a2")
            self.assertEqual(by_id["a1"]["branch"], "a1")
            self.assertEqual(by_id["side"]["branch"], "side")
            self.assertNotEqual(by_id["a1"]["branch"], by_id["side"]["branch"])

    def test_ancestry_status_and_ambiguous_message_ids_are_exposed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ambiguous.jsonl"
            rows = [
                {"type": "session", "id": "ambiguous", "cwd": temp},
                {"type": "message", "id": "dup", "message": {"role": "user", "content": "One"}},
                {"type": "message", "id": "dup", "message": {"role": "user", "content": "Two"}},
                {"type": "message", "id": "a", "parentId": "dup", "message": {"role": "assistant", "content": "Reply"}},
                {"type": "message", "id": "child", "parentId": "absent", "message": {"role": "assistant", "content": "Orphan"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            by_id = {event.get("external_message_id"): event for event in recording["events"]
                     if event.get("external_message_id")}
            self.assertTrue(by_id["dup"]["message_ambiguous"])
            self.assertEqual(by_id["a"]["ancestry_status"], "ambiguous")
            self.assertEqual(by_id["child"]["ancestry_status"], "missing")
            self.assertEqual(by_id["a"]["ancestry_depth"], 0)
            self.assertIn(by_id["a"]["request_basis"], ("ambiguous", "unresolved_ancestry"))

    def test_projection_carries_order_and_branch_fields(self):
        recording, _ = self.scan()
        by_id = {event.get("external_message_id"): event for event in recording["events"]
                 if event.get("external_message_id")}
        for field in ("seq", "branch", "branch_basis", "message_id", "ancestry_status", "ancestry_depth"):
            self.assertIn(field, by_id["u1"])
        self.assertEqual(by_id["a1"]["request_ref"], by_id["u1"]["ref"])

    def test_truncated_jsonl_is_a_gap_without_a_phantom_event(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "truncated.jsonl"
            path.write_text('{"type":"session","id":"t"}\n{"type":"message"\n')
            scope = new_scope()
            recording = scan_file(path, ScanOptions(harness="pi"), scope)
            self.assertEqual(scope["malformed_records"], 1)
            self.assertEqual(scope["interpretation_gaps"], 1)
            self.assertTrue(any(event["kind"] == "gap" for event in recording["events"]))


    def test_orphan_result_remains_a_queryable_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "orphan.jsonl"
            rows = [
                {"type": "session", "id": "orphan", "cwd": temp},
                {"type": "message", "id": "r", "message": {"role": "toolResult", "toolCallId": "absent",
                 "isError": False, "content": [{"type": "text", "text": "orphan output"}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            recording = scan_file(path, ScanOptions(harness="pi"), new_scope())
            facts, coverage = operation_facts.operations([recording], RecordCache())
            self.assertEqual(coverage["result_without_call"], 1)
            self.assertEqual(facts[0]["pairing"], "result_without_call")
            self.assertEqual(facts[0]["outcome"], "succeeded")

    def test_duplicate_call_ids_in_sibling_branches_are_not_paired_by_proximity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dup.jsonl"
            rows = [
                {"type": "session", "id": "dup", "cwd": temp},
                {"type": "message", "id": "u", "message": {"role": "user", "content": "Fix it"}},
                {"type": "message", "id": "a1", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "same", "name": "read", "arguments": {"path": "a.py"}}]}},
                {"type": "message", "id": "a2", "parentId": "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "same", "name": "read", "arguments": {"path": "b.py"}}]}},
                {"type": "message", "id": "r", "parentId": "a2", "message": {"role": "toolResult", "toolCallId": "same",
                 "isError": False, "content": [{"type": "text", "text": "b"}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            scope = new_scope()
            recording = scan_file(path, ScanOptions(harness="pi"), scope)
            calls = [e for e in recording["events"] if e.get("kind") == "tool_call"]
            self.assertTrue(all("result_ref" not in call for call in calls))
            self.assertEqual(scope["ambiguous_call_ids"], 1)
            self.assertGreaterEqual(scope["unpaired_results"], 1)


if __name__ == "__main__":
    unittest.main()
