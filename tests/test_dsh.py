"""DeepSeek Harness recordings normalize into the same event model as pi, codex, claude-code."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from helper import cite, edit_hunks, file_observations, operation_facts, zstd  # noqa: E402
from helper.discovery import candidate_files  # noqa: E402
from helper.options import DiscoveryOptions, ScanOptions  # noqa: E402
from helper.profiles import detected, read_view  # noqa: E402
from helper.reader import scan_file  # noqa: E402
from helper.records import RecordCache, new_scope, read_ref, records  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/recordings/dsh-slice1.jsonl"


def scan(path=FIXTURE):
    scope = new_scope()
    recording = scan_file(path, ScanOptions(harness="auto", max_text_bytes=1024 * 1024), scope)
    return recording, scope


class DshTests(unittest.TestCase):
    def test_header_detects_dsh_without_capturing_pi(self):
        header = json.loads(FIXTURE.read_text().splitlines()[0])
        self.assertEqual(detected(header), "dsh")
        pi = json.loads((ROOT / "tests/fixtures/recordings/pi-slice1.jsonl").read_text().splitlines()[0])
        self.assertEqual(detected(pi), "pi")

    def test_events_pair_by_call_id_and_failures_stay_failed(self):
        recording, scope = scan()
        self.assertEqual(recording["harness"], "dsh")
        self.assertEqual(recording["session"], "session-dsh0001")
        self.assertEqual(recording["root"], "/work/project")
        kinds = {event["kind"] for event in recording["events"]}
        self.assertLessEqual({"user_message", "assistant_message", "reasoning", "tool_call", "tool_result",
                              "instruction", "config", "lifecycle", "summary"}, kinds)
        calls = {event.get("tool"): event for event in recording["events"] if event["kind"] == "tool_call"}
        self.assertEqual({tool: call["outcome"] for tool, call in calls.items()},
                         {"bash": "succeeded", "read": "succeeded", "edit": "succeeded", "write": "failed"})
        purposes = {event.get("purpose") for event in recording["events"] if event["kind"] in ("config", "lifecycle")}
        self.assertLessEqual({"compaction_start", "compaction_summary", "compaction_end",
                              "approval_asked", "approval_decided", "job_settled"}, purposes)
        instructions = {event["purpose"] for event in recording["events"] if event["kind"] == "instruction"}
        self.assertEqual(instructions, {"system_prompt", "tool_registry"})
        self.assertEqual([event["native_type"] for event in recording["events"] if event["kind"] == "unknown"],
                         ["plugin/custom"])
        self.assertTrue(all(call.get("result_ref") for call in calls.values()))
        self.assertEqual(scope["ambiguous_call_ids"], 0)
        self.assertEqual(scope["unpaired_results"], 0)
        self.assertEqual(calls["bash"]["command"], "ls")
        self.assertTrue(calls["bash"]["is_shell"])
        self.assertEqual(calls["edit"]["operation"], "file_edit")
        self.assertEqual(calls["read"]["path"], "app.py")

    def test_injected_context_keeps_the_human_request(self):
        recording, _ = scan()
        users = {event["external_message_id"]: event for event in recording["events"]
                 if event["kind"] == "user_message"}
        self.assertEqual([mid for mid, event in users.items() if event["origin"] == "human"], ["u1"])
        self.assertEqual(users["i1"]["origin"], "harness")
        self.assertEqual(users["i1"]["channel"], "injected")
        self.assertEqual(users["i1"]["turn"], "u1")
        answer = next(event for event in recording["events"]
                      if event["kind"] == "assistant_message" and event["text"] == "Done.")
        self.assertEqual(answer["request_basis"], "recorded_ancestry")
        self.assertEqual(answer["turn"], "u1")

    def test_replacement_copies_are_one_summary_not_duplicate_results(self):
        recording, scope = scan()
        summaries = [event for event in recording["events"] if event["kind"] == "summary"]
        self.assertEqual([(event["purpose"], event["representation"]) for event in summaries],
                         [("surface_replacement", "replacement")])
        results = [event for event in recording["events"]
                   if event["kind"] == "tool_result" and event["call_id"] == "call-bash"]
        self.assertEqual(len(results), 1)
        self.assertEqual(scope["ambiguous_call_ids"], 0)
        self.assertGreaterEqual(scope["ignored_items"], 2)

    def test_timestamps_are_iso_utc(self):
        recording, _ = scan()
        header = next(event for event in recording["events"] if event["purpose"] == "session")
        self.assertEqual(header["timestamp"], "2026-01-01T00:00:00Z")
        request = next(event for event in recording["events"] if event["external_message_id"] == "u1")
        self.assertEqual(request["timestamp"], "2026-01-01T00:00:10Z")

    def test_refs_read_back_and_cite_tokens_resolve_the_session(self):
        recording, _ = scan()
        request = next(event for event in recording["events"] if event["external_message_id"] == "u1")
        self.assertEqual(request["ref"].get("compression"), None)
        self.assertEqual(read_ref(request["ref"])["type"], "user/message")
        self.assertTrue(cite.token(request["ref"]).startswith("dsh-slice1@"))
        nested = Path("/archive/--work-project--/session-11111111-2222-3333-4444-555555555555/session.v4.jsonl.zstd")
        self.assertEqual(cite.recording_id(nested), "11111111-2222-3333-4444-555555555555")

    def test_read_adapter_reports_extent_and_completeness(self):
        text = ("<path>/work/project/app.py</path>\n<type>file</type>\n<content>\n"
                "1: def main():\n2:     return 2\n3: \n\n(End of file - total 3 lines)\n</content>")
        view = read_view("dsh", {}, text, {})
        self.assertEqual((view["start_line"], view["line_count"], view["total_lines"], view["completeness"]),
                         (1, 3, 3, "complete"))
        self.assertEqual(view["content"], "def main():\n    return 2\n")
        truncated = text.replace("(End of file - total 3 lines)",
                                 "(Showing lines 1-2 of 9. Use offset=3 to continue.)")
        view = read_view("dsh", {}, truncated, {"limit": 2})
        self.assertEqual((view["total_lines"], view["completeness"]), (9, "truncated"))
        self.assertIsNone(read_view("dsh", {}, "not a read result", {}))

    def test_edit_hunks_keep_only_recorded_changes_without_invented_lines(self):
        arguments = json.dumps({"file_path": "app.py", "old_string": "def main():\n    return 1\n",
                                "new_string": "def main():\n    return 2\n"})
        record = {"data": {"message": {"content": [{"type": "tool-call", "id": "c1", "name": "edit",
                                                    "arguments": arguments}]}}}
        event = {"harness": "dsh", "kind": "tool_call", "tool": "edit", "operation": "file_edit",
                 "payload_pointer": "/data/message/content/0/arguments", "outcome": "succeeded"}
        self.assertIs(edit_hunks._extractor(event), edit_hunks._dsh)
        found, _ = edit_hunks._dsh(event, record)
        change, source, hunks, original, after, truncated = found
        self.assertEqual((change, source, original, after, truncated),
                         ("update", "dsh_edit_arguments", None, None, 0))
        self.assertEqual([(old, new) for _, old, _, new, _ in hunks], [(1, 1)])
        self.assertEqual(hunks[0][4], ["-    return 1", "+    return 2"])
        block, changed, added, removed = edit_hunks.shape(hunks[0][4])
        self.assertEqual((block, changed, added, removed), (["    return 2"], [0], 1, 1))
        self.assertEqual(edit_hunks.anchor(block, 0, ["def main():", "    return 2"]),
                         ("shifted", 1))

        write = {"harness": "dsh", "kind": "tool_call", "tool": "write", "operation": "file_edit",
                 "payload_pointer": "/data/message/content/0/arguments", "outcome": "succeeded"}
        record["data"]["message"]["content"][0]["arguments"] = json.dumps({"file_path": "n.md", "content": "# N\n"})
        found, _ = edit_hunks._dsh(write, record)
        self.assertEqual((found[0], found[1], found[4]), ("write", "dsh_write_content", "# N\n"))

    def test_operations_and_observations_use_the_recorded_read_and_edit(self):
        recording, _ = scan()
        cache = RecordCache()
        recordings = [recording]
        operations, _ = operation_facts.operations(recordings, cache)
        repo_path = lambda op: op.get("path") if op.get("path_location") == "repository" else None
        observations, changes, _ = file_observations.build(recordings, cache, operations, repo_path)
        read = next(row for row in observations if row["kind"] == "read_result" and row["path"] == "app.py")
        self.assertEqual((read["start_line"], read["line_count"], read["total_lines"], read["completeness"]),
                         (1, 3, 3, "complete"))
        edit = next(row for row in observations if row["path"] == "app.py" and row["kind"] == "native_patch")
        self.assertEqual(edit["adapter"], "dsh_edit_arguments")
        self.assertTrue(changes)

    def test_discovery_groups_the_session_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "--work-project--" / "session-dsh0001"
            directory.mkdir(parents=True)
            shutil.copy(FIXTURE, directory / "session.v4.jsonl")
            scope = new_scope()
            groups = candidate_files(DiscoveryOptions(files=(temp,), project=None), scope)
            dsh = next(group for group in groups if group[0] == "dsh")
            self.assertEqual((len(dsh[1]), dsh[2]["files_selected"]), (1, 1))

    def test_missing_decoder_is_counted_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "--work-project--" / "session-dsh0001"
            directory.mkdir(parents=True)
            shutil.copy(FIXTURE, directory / "session.v4.jsonl.zstd")
            with mock.patch.object(zstd, "decoder_kind", return_value=None):
                groups = candidate_files(DiscoveryOptions(files=(temp,), project=None), new_scope())
            unknown = next(group for group in groups if group[0] == "unknown")
            self.assertEqual(unknown[1], [])
            self.assertEqual(unknown[2]["files_compressed_unavailable"], 1)
            with mock.patch.object(zstd, "decoder_kind", return_value=None), \
                    mock.patch("helper.discovery.default_recordings", return_value={"dsh": [Path(temp)]}):
                groups = candidate_files(DiscoveryOptions(project=None), new_scope())
            dsh = next(group for group in groups if group[0] == "dsh")
            self.assertEqual(dsh[1], [])
            self.assertEqual(dsh[2]["files_compressed_unavailable"], 1)

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable is required to build the compressed fixture")
    def test_compressed_artifact_scans_and_reads_back(self):
        with tempfile.TemporaryDirectory() as temp:
            compressed = Path(temp) / "session.v4.jsonl.zstd"
            subprocess.run(["zstd", "-q", "-f", str(FIXTURE), "-o", str(compressed)], check=True)
            with self.assertRaises(ValueError):
                next(records(compressed, new_scope(), 1 << 20, 1 << 28, ((0, 10),)))
            recording, scope = scan(compressed)
            self.assertTrue(scope["source_eof"])
            self.assertTrue(scope["scan_complete"])
            self.assertEqual({event["kind"] for event in recording["events"]},
                             {event["kind"] for event in scan()[0]["events"]})
            request = next(event for event in recording["events"] if event["external_message_id"] == "u1")
            self.assertEqual(request["ref"]["compression"], "zstd")
            self.assertEqual(read_ref(request["ref"])["type"], "user/message")

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable is required to build the compressed fixture")
    def test_record_cache_decompresses_once_and_streams_over_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            compressed = Path(temp) / "session.v4.jsonl.zstd"
            subprocess.run(["zstd", "-q", "-f", str(FIXTURE), "-o", str(compressed)], check=True)
            refs = [event["ref"] for event in scan(compressed)[0]["events"]]
            streamed = [read_ref(ref) for ref in refs]
            with mock.patch.object(zstd, "open_stream", wraps=zstd.open_stream) as opened:
                cache = RecordCache()
                self.assertEqual([cache.get(ref) for ref in refs], streamed)
                self.assertEqual(opened.call_count, 1)
            with mock.patch("helper.records.ZSTD_CACHE_BYTES", 0):
                self.assertEqual([RecordCache().get(ref) for ref in refs], streamed)

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable is required to build the compressed fixture")
    def test_truncated_artifact_reports_a_decompression_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            whole = Path(temp) / "whole.zstd"
            subprocess.run(["zstd", "-q", "-f", str(FIXTURE), "-o", str(whole)], check=True)
            broken = Path(temp) / "session.v4.jsonl.zstd"
            data = whole.read_bytes()
            broken.write_bytes(data[:len(data) // 2])
            _, scope = scan(broken)
            self.assertFalse(scope["scan_complete"])
            self.assertFalse(scope["source_eof"])
            self.assertIn("decompression_failed", scope)


if __name__ == "__main__":
    unittest.main()
