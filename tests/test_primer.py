"""Recent-work primer: bounded session/Git evidence, current-session exclusion, no retention."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper.options import ScanOptions  # noqa: E402
from helper.primer import _request, _ranges, build, render  # noqa: E402
from helper.reader import scan_file  # noqa: E402
from helper.records import new_scope  # noqa: E402


class PrimerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.cache = self.home / "cache"
        self.archive = self.home / "recordings"
        self.archive.mkdir()
        self.empty = self.home / "empty-recordings"
        self.empty.mkdir()
        self.settings = self.home / "settings.json"
        self.settings.write_text(json.dumps({"recordings": {
            "pi": [str(self.archive)], "codex": [str(self.empty)], "claude-code": [str(self.empty)]}}))
        self.write("a.py", "def target():\n    return 2\n")

    def write(self, path, text):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, newline="")
        return dest

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.com", *args], text=True).strip()

    def init(self):
        self.git("init", "-q")
        self.commit("initial commit")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def recording(self, sid="past", request="Fix the importer and verify it",
                  last="Completed the importer fix and verified offline mode with the test suite; "
                       "the old configuration path is unchanged.",
                  root=None, path="a.py", extra=()):
        rows = [
            {"type": "session", "id": sid, "cwd": str(root or self.repo)},
            {"type": "message", "id": sid + "u", "message": {"role": "user", "content": request}},
            {"type": "message", "id": sid + "a", "parentId": sid + "u",
             "message": {"role": "assistant", "content": [{"type": "text", "text": last}]}},
            {"type": "message", "id": sid + "c", "parentId": sid + "a",
             "message": {"role": "assistant", "content": [
                 {"type": "toolCall", "id": sid + "call", "name": "write",
                  "arguments": {"path": path, "content": "def target():\n    return 3\n"}}]}},
            {"type": "message", "id": sid + "r", "parentId": sid + "c",
             "message": {"role": "toolResult", "toolCallId": sid + "call", "toolName": "write",
                         "isError": False, "content": [{"type": "text", "text": "done"}]}},
            *extra,
        ]
        dest = self.archive / (sid + ".jsonl")
        dest.write_text("".join(json.dumps(row) + "\n" for row in rows), newline="")
        return dest

    def env(self, **extra):
        env = {key: value for key, value in os.environ.items()
               if key not in ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID")}
        env["TRACE_SESSIONS_SETTINGS"] = str(self.settings)
        env.update(extra)
        return env

    def cli(self, *args, **env):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/remember.py"), *args,
                                 "--repo", str(self.repo), "--cache", str(self.cache)],
                                capture_output=True, text=True, timeout=40,
                                env=self.env(**env))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def json_map(self, *args, **env):
        return json.loads(self.cli("map", "--format", "json", *args, **env))

    def test_bare_map_shows_recent_sessions_commits_and_tree(self):
        self.init()
        self.write("a.py", "def target():\n    return 3\n")
        self.commit("fix: return three")
        self.recording()
        self.write("a.py", "def target():\n    return 4\n")
        self.write("new.py", "x = 1\n")
        text = self.cli(PI_SESSION_FILE=str(self.archive / "current.jsonl"))
        self.assertTrue(text.startswith("WHOLE REPOSITORY:"))
        self.assertIn("recent work (recorded; current session excluded)", text)
        self.assertIn("[pi] Fix the importer and verify it", text)
        self.assertIn("last: Completed the importer fix", text)
        self.assertIn("edited: a.py", text)
        self.assertIn("fix: return three", text)
        self.assertIn("(1 files +1/-1)", text)
        self.assertIn("tree: 1 changed, 1 untracked", text)

    def test_current_session_excluded_by_file_and_id(self):
        self.recording(sid="current", request="CURRENT PRIVATE request must not appear",
                       last="CURRENT PRIVATE last message must not appear in the primer at all.")
        self.recording(sid="past", request="PAST request about the parser",
                       last="PAST last message about the parser and its tests, long enough to be substantive.")
        for env in ({"PI_SESSION_FILE": str(self.archive / "current.jsonl")}, {"PI_SESSION_ID": "current"}):
            text = self.cli(**env)
            self.assertNotIn("CURRENT PRIVATE", text)
            self.assertIn("PAST request about the parser", text)
            self.assertIn("current session excluded", text)

    def test_json_primer_is_opt_in_and_not_retained(self):
        self.init()
        self.recording(request="UNIQUE_PRIVATE_REQUEST text")
        plain = self.json_map()
        self.assertNotIn("primer", plain)
        full = self.json_map("--primer")
        self.assertEqual(len(full["primer"]["sessions"]), 1)
        self.assertEqual(full["primer"]["sessions"][0]["goal"], "UNIQUE_PRIVATE_REQUEST text")
        report = self.cli("show", full["report"])
        self.assertNotIn("primer", json.loads(report))
        self.assertNotIn("UNIQUE_PRIVATE_REQUEST", report)

    def test_text_map_never_saves_a_report(self):
        self.init()
        self.recording(request="UNIQUE_PRIVATE_REQUEST text")
        self.cli()
        self.assertEqual(list(self.cache.rglob("report-*.json")), [])

    def test_no_sessions_and_no_git(self):
        text = self.cli()
        self.assertNotIn("recent work", text)
        self.assertTrue(text.startswith("WHOLE REPOSITORY:"))

    def test_merge_commits_skipped_by_default(self):
        self.init()
        main = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.git("checkout", "-qb", "side")
        self.write("b.py", "b = 1\n")
        self.commit("side work")
        self.git("checkout", "-q", main)
        self.git("merge", "--no-ff", "-q", "-m", "merge side", "side")
        default = self.json_map("--primer", "--primer-depth", "5")
        subjects = [row["subject"] for row in default["primer"]["commits"]]
        self.assertNotIn("merge side", subjects)
        self.assertGreaterEqual(default["primer"]["coverage"]["commits"]["merges_skipped"], 1)
        merged = self.json_map("--primer", "--primer-depth", "5", "--primer-merges")
        self.assertIn("merge side", [row["subject"] for row in merged["primer"]["commits"]])

    def test_depth_and_no_primer(self):
        self.init()
        for index in range(3):
            self.recording(sid=f"s{index}", request=f"Request number {index} about the parser")
        one = self.json_map("--primer", "--primer-depth", "1")
        self.assertEqual(len(one["primer"]["sessions"]), 1)
        self.assertEqual(len(one["primer"]["commits"]), 1)
        text = self.cli("map", "--no-primer")
        self.assertNotIn("recent work", text)

    def test_snapshot_map_skips_the_primer(self):
        self.init()
        self.recording()
        snapshot = json.loads(self.cli("snapshot"))["results"][0]["snapshot"]
        out = json.loads(self.cli("map", "--format", "json", "--primer", "--snapshot", snapshot))
        self.assertNotIn("primer", out)

    def test_parent_root_fallback_finds_nested_project_recordings(self):
        parent = self.home / "parent"
        nested = parent / "sub" / "repo"
        nested.mkdir(parents=True)
        self.repo = nested
        (nested / "a.py").write_text("def target():\n    return 2\n", newline="")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")
        self.recording(root=parent, request="Request recorded at the parent root should be found")
        text = self.cli()
        self.assertIn("Request recorded at the parent root should be found", text)

    def test_symlinked_root_still_matches_the_resolved_project(self):
        # Recordings carry the cwd the harness saw, which can be a symlinked path (macOS
        # temp dirs are /var/... -> /private/var/...); the lookup must not miss them.
        link = self.home / "link"
        try:
            link.symlink_to(self.repo, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.recording(root=link, request="SYMLINKED request about the importer")
        text = self.cli()
        self.assertIn("SYMLINKED request about the importer", text)

    def test_wrapper_filter_and_generic_continuation(self):
        self.recording(request="<environment_context>cwd=/tmp</environment_context>", extra=(
            {"type": "message", "id": "yes", "message": {"role": "user", "content": "yes"}},
            {"type": "message", "id": "real", "parentId": "yes", "message": {"role": "user",
             "content": "Use the existing cache and avoid a new dependency"}},
        ))
        text = self.cli()
        self.assertIn("Use the existing cache and avoid a new dependency", text)
        self.assertNotIn("environment_context", text)

    def test_range_scan_reads_head_and_tail(self):
        path = self.home / "long.jsonl"
        rows = [
            {"type": "session", "id": "s", "cwd": str(self.repo)},
            {"type": "message", "id": "u", "message": {"role": "user", "content": "First request in the head"}},
            {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "First answer in the head is long enough to be substantive."}]}},
        ]
        rows += [{"type": "message", "id": f"x{i}", "parentId": "a", "message": {"role": "toolResult",
                   "content": [{"type": "text", "text": "filler" * 100}]}} for i in range(20)]
        rows.append({"type": "message", "id": "last", "parentId": "x19", "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Last answer in the tail is the one we need."}]}})
        lines = [json.dumps(row) + "\n" for row in rows]
        path.write_text("".join(lines), newline="")
        head_end = sum(len(line) for line in lines[:3])
        tail_start = sum(len(line) for line in lines[:-1])
        recording = scan_file(path, ScanOptions(harness="pi"), new_scope(),
                              ranges=[(0, head_end), (tail_start, None)])
        self.assertTrue(recording["sampled"])
        texts = [event.get("text", "") for event in recording["events"]]
        self.assertIn("First request in the head", texts)
        self.assertTrue(any("Last answer in the tail" in text for text in texts))
        tail = next(event for event in recording["events"] if "Last answer in the tail" in event.get("text", ""))
        self.assertIsNone(tail["ref"]["line"])
        self.assertIsInstance(tail["ref"]["offset"], int)

    def test_ranges_and_render_budget(self):
        self.assertIsNone(_ranges(9_000_000))
        self.assertEqual(_ranges(10_000_000)[0], (0, 1_000_000))
        data = {"sessions": [{"harness": "pi", "ended_at": "2026-10-26T00:00:00Z",
                              "goal": "Goal " * 60, "last": "Last " * 80, "last_kind": "last",
                              "edited": [{"path": f"src/long/path/item{i}.py", "edits": 1} for i in range(5)]}
                             for _ in range(3)],
                "commits": [{"sha": "a" * 40, "committed_at": "2026-10-25T00:00:00Z",
                             "subject": "Commit " * 30, "files_changed": 4, "additions": 10,
                             "deletions": 2, "paths": [{"path": "src/a.py"}]} for _ in range(3)],
                "tree": None,
                "coverage": {"sessions": {"state": "searched", "current_session_exclusion": "configured"},
                             "commits": {"state": "searched", "complete": True}}}
        for limit in (2048, 900, 512):
            lines = render(data, limit)
            text = "\n".join(lines) + "\n"
            self.assertLessEqual(len(text.encode("utf-8")), limit)
            self.assertTrue(lines[0].startswith("recent work"))
        self.assertIn("last:", "\n".join(render(data, 2048)))
        self.assertNotIn("Goal " * 60, "\n".join(render(data, 512)))

    def test_request_filter(self):
        self.assertFalse(_request("<environment_context>cwd=/tmp</environment_context>"))
        self.assertFalse(_request("/clear"))
        self.assertFalse(_request("yes"))
        self.assertFalse(_request("src/foo.py"))
        self.assertTrue(_request("Fix the importer and verify it"))


if __name__ == "__main__":
    unittest.main()
