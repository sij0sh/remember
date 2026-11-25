"""Thin activity projection over shared operation facts."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import catalog, runtime
from support import invoke

FIXTURE = ROOT / "tests/fixtures/recordings/pi-slice1.jsonl"


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text("def main():\n    return 2\n")
        self.cache = self.home / "cache"

    def cli(self, *args):
        result = invoke([*map(str, args), "--recordings", str(FIXTURE), "--project-root", "/work/project",
                         "--root-map", f"/work/project={self.repo}", "--repo", str(self.repo),
                         "--cache", str(self.cache)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.json()

    def args(self, *argv):
        full = [*argv, "--recordings", str(FIXTURE), "--project-root", "/work/project",
                "--root-map", f"/work/project={self.repo}", "--repo", str(self.repo),
                "--cache", str(self.cache)]
        return runtime.normalize(runtime.parser().parse_args(full), full)

    def test_path_activity_returns_operations_in_lane_order(self):
        out = self.cli("activity", "src/app.py", "--max-bytes", "12000")
        rows = out["results"]
        self.assertTrue(rows)
        self.assertEqual({row["path"] for row in rows}, {"src/app.py"})
        self.assertTrue(all(row["path_location"] == "repository" for row in rows))
        self.assertIn("write", {row["tool"] for row in rows})
        write = next(row for row in rows if row["tool"] == "write")
        self.assertEqual(write["outcome"], "unknown")
        keys = [(row["lane"], row["order"], row["association"]) for row in rows]
        self.assertEqual(keys, sorted(keys))
        self.assertGreaterEqual(out["coverage"]["activity"]["matched_operations"], 4)
        self.assertEqual(out["scope"]["retention"], "transient")
        self.assertTrue(all(row.get("call_ref") for row in rows))

    def test_symbol_activity_uses_pinned_symbol_events(self):
        out = self.cli("activity", "src/app.py::main", "--max-bytes", "12000")
        self.assertEqual(out["coverage"]["activity"]["symbol_resolution"], "symbol_event_mapping")
        self.assertTrue(out["results"])
        self.assertEqual({row["tool"] for row in out["results"] if "section" not in row}, {"edit"})
        self.assertTrue(all(row["operation_id"] in {r["operation_id"] for r in out["results"] if "section" not in r}
                            for row in out["results"] if row.get("section") == "change"))

    def test_same_relative_path_in_another_project_is_not_activity(self):
        other = self.home / "other.jsonl"
        rows = [
            {"type": "session", "id": "other", "cwd": "/work/elsewhere"},
            {"type": "message", "id": "u", "message": {"role": "user", "content": "Work elsewhere"}},
            {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": "/work/elsewhere/src/app.py"}}]}},
            {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult", "toolCallId": "c",
             "isError": False, "content": [{"type": "text", "text": "other"}]}},
        ]
        other.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result = invoke(["activity", "src/app.py", "--recordings", str(other),
                         "--repo", str(self.repo), "--cache", str(self.cache)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.json()
        self.assertEqual(out["results"], [])
        self.assertTrue(out["coverage"]["activity"]["none_found_in_scanned_scope"])

    def test_returned_node_ref_is_a_valid_activity_target(self):
        inspected = self.cli("inspect", "src/app.py::main", "--no-sessions")
        ref = inspected["target"]["ref"]
        out = self.cli("activity", ref, "--max-bytes", "12000")
        self.assertEqual(out["target"]["path"], "src/app.py")
        self.assertEqual(out["target"]["snapshot"], ref.split("/", 1)[0])

    def test_historical_path_reports_none_found_in_scope(self):
        out = self.cli("activity", "removed.py")
        self.assertEqual(out["results"], [])
        self.assertTrue(out["coverage"]["activity"]["none_found_in_scanned_scope"])
        self.assertEqual(out["coverage"]["activity"]["matched_operations"], 0)

    def test_path_activity_does_not_build_the_source_graph(self):
        args = self.args("activity", "src/app.py")
        with patch("helper.graph_cli.get_graph", side_effect=AssertionError("source graph")):
            document = runtime.Runtime(runtime.Request(args, tuple(["activity", "src/app.py"]), None)).execute().document
        self.assertEqual(document["operation"], "activity")
        self.assertTrue(document["results"])

    def test_operations_and_checkpoints_are_queryable_transient_tables(self):
        out = self.cli("query", 'SELECT operation_id, path, outcome, interpretation FROM operations '
                                'WHERE path=\'src/app.py\' ORDER BY "order", association')
        self.assertTrue(out["results"])
        self.assertEqual(out["scope"]["retention"], "transient")
        checkpoints = self.cli("query", 'SELECT coverage_end_id, boundary_status, boundary_order '
                                       'FROM checkpoints ORDER BY "order"')
        statuses = {row["coverage_end_id"]: row["boundary_status"] for row in checkpoints["results"]}
        self.assertEqual(statuses, {"u1": "resolved", "u2": "off_branch"})
        schema = catalog.describe("operations")[0]
        self.assertEqual(schema["provider"], "operations")
        self.assertEqual(schema["retention"], "transient")
        self.assertIn("operation_id", schema["keys"])

    def test_operations_and_checkpoints_share_one_recording_scan(self):
        args = self.args("query", "SELECT count(*) AS n FROM operations CROSS JOIN checkpoints")
        from helper import providers
        original = providers.Providers.load_recordings
        calls = []
        def counted(instance):
            calls.append(1)
            return original(instance)
        with patch.object(providers.Providers, "load_recordings", counted):
            runtime.Runtime(runtime.Request(args, tuple(["query", "SELECT 1"]), None)).execute()
        self.assertEqual(calls, [1])

    def test_returned_continuation_replays_activity_not_inspect(self):
        out = self.cli("activity", "src/app.py", "--max-bytes", "6000", "--max-events", "1")
        token = next(item["ref"] for item in out["next"] if item.get("effect") == "coverage")
        result = invoke(["show", token, "--repo", str(self.repo), "--cache", str(self.cache)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.json()["operation"], "activity")

    def test_small_page_has_executable_continuation(self):
        # Continuation tokens carry the absolute temp path, which is longer on CI runners.
        out = self.cli("activity", "src/app.py", "--limit", "1", "--max-bytes", "12000")
        self.assertFalse(out["delivery"]["complete"])
        self.assertTrue(out["next"])
        self.assertEqual(out["delivery"]["returned"], 1)


if __name__ == "__main__":
    unittest.main()
