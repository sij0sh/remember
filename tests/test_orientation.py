"""Bare-map orientation: the shared investigate packet (no goal), recent commits, tree state; never retained."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from support import invoke
ENV_DROP = ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


class OrientationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.repo, self.cache = self.home / "repo", self.home / "cache"
        self.archive, empty = self.home / "recordings", self.home / "empty"
        for d in (self.repo, self.archive, empty):
            d.mkdir()
        self.settings = self.home / "settings.json"
        self.settings.write_text(json.dumps({"recordings": {
            "pi": [str(self.archive)], "codex": [str(empty)], "claude-code": [str(empty)]}}))
        self.write("a.py", "def target():\n    return 2\n")

    def write(self, path, text):
        (self.repo / path).write_text(text, newline="")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.com", *args], text=True).strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def init(self):
        self.git("init", "-q")
        self.commit("initial commit")

    def recording(self, sid="past", request="Fix the importer in a.py", root=None, rule=None):
        rows = [{"type": "session", "id": sid, "cwd": str(root or self.repo)},
                {"type": "message", "id": sid + "u", "message": {"role": "user", "content": request}},
                {"type": "message", "id": sid + "c", "parentId": sid + "u", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": sid + "call", "name": "write",
                     "arguments": {"path": "a.py", "content": "def target():\n    return 3\n"}}]}},
                {"type": "message", "id": sid + "r", "parentId": sid + "c",
                 "message": {"role": "toolResult", "toolCallId": sid + "call", "toolName": "write",
                             "isError": False, "content": [{"type": "text", "text": "done"}]}}]
        if rule:
            rows.append({"type": "message", "id": sid + "rule", "parentId": sid + "r",
                         "message": {"role": "user", "content": rule}})
        dest = self.archive / (sid + ".jsonl")
        dest.write_text("".join(json.dumps(row) + "\n" for row in rows), newline="")
        return dest

    def cli(self, *args, **extra):
        env = {"TRACE_SESSIONS_SETTINGS": str(self.settings), **{name: None for name in ENV_DROP}, **extra}
        p = invoke([*args, "--repo", str(self.repo), "--cache", str(self.cache)], env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stderr, "")
        return p.stdout

    def json_map(self, *args, **env):
        return json.loads(self.cli("map", "--format", "json", *args, **env))

    def test_bare_map_shows_views_leads_commits_and_tree(self):
        self.init()
        self.write("a.py", "def target():\n    return 3\n")
        self.commit("fix: return three")
        self.recording(rule="Timestamps must stay UTC; billing parses them.")
        self.write("a.py", "def target():\n    return 4\n")
        self.write("new.py", "x = 1\n")
        text = self.cli()
        self.assertTrue(text.startswith("WHOLE REPOSITORY:"))
        self.assertIn("recent work (current may be included): pitfall=none_found_in_scanned_scope(0)", text)
        self.assertIn("invariant: Timestamps must stay UTC", text)
        self.assertIn("fix: return three", text)
        self.assertIn("(1 files +1/-1)", text)
        self.assertIn("tree: 1 changed, 1 untracked", text)

    def test_current_session_excluded_and_leads_match_investigate(self):
        current = self.recording(sid="current", rule="CURRENT PRIVATE text must never appear here.")
        self.recording(sid="past", rule="Exports must escape commas in every CSV.")
        for env in ({"PI_SESSION_FILE": str(current)}, {"PI_SESSION_ID": "current"}):
            text = self.cli(**env)
            self.assertNotIn("CURRENT PRIVATE", text)
            self.assertIn("Exports must escape commas", text)
            self.assertIn("current session excluded", text)
        primer = self.json_map("--primer", PI_SESSION_ID="current")["primer"]
        out = json.loads(self.cli("investigate", "--max-bytes", "60000", PI_SESSION_ID="current"))
        ids = {r["id"] for r in out["results"] if r["section"] == "lead"}
        self.assertTrue(primer["leads"])
        self.assertTrue({lead["id"] for lead in primer["leads"]} <= ids)

    def test_json_primer_is_opt_in_and_not_retained(self):
        self.init()
        self.recording(rule="UNIQUE_PRIVATE rule must hold.")
        plain = self.json_map()
        self.assertTrue(plain["primer"].startswith("omitted:"))
        full = self.json_map("--primer")
        self.assertEqual(set(full["primer"]["views"]), {"pitfall", "invariant", "task", "decision"})
        self.assertIn("UNIQUE_PRIVATE", json.dumps(full["primer"]["leads"]))
        for entry in self.cache.rglob("*.json"):
            self.assertNotIn("UNIQUE_PRIVATE", entry.read_text())
        self.assertEqual(list(self.cache.rglob("report-*.json")), [])

    def test_nothing_to_show_and_no_primer(self):
        text = self.cli()
        self.assertIn("primer omitted: no leads, commits or tree state", text)
        self.assertNotIn("recent work", self.cli("map", "--no-primer"))

    def test_merge_commits_skipped(self):
        self.init()
        main = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.git("checkout", "-qb", "side")
        self.write("b.py", "b = 1\n")
        self.commit("side work")
        self.git("checkout", "-q", main)
        self.git("merge", "--no-ff", "-q", "-m", "merge side", "side")
        subjects = [row["subject"] for row in self.json_map("--primer")["primer"]["commits"]]
        self.assertNotIn("merge side", subjects)
        self.assertIn("side work", subjects)

    def test_removed_primer_flags_fail(self):
        for flag in ("--primer-depth", "--primer-root", "--primer-merges"):
            p = invoke(["map", flag, "1", "--repo", str(self.repo), "--cache", str(self.cache)])
            self.assertEqual(p.returncode, 2, flag)

    def test_snapshot_replay_skips_orientation(self):
        self.init()
        self.recording()
        snapshot = self.json_map()["snapshots"][0]
        out = self.json_map("--primer", "--snapshot", snapshot)
        self.assertIn("--snapshot", out["primer"])

    def test_table_and_byte_cap(self):
        self.init()
        self.recording(rule="TABLE rule must stay visible.")
        self.assertIn("TABLE rule must stay visible", self.cli("map", "--format", "table", "--primer"))
        self.assertIn("primer: omitted: --path", self.cli("map", "--format", "table"))
        capped = self.json_map("--primer", "--max-bytes", "2048")
        self.assertTrue(capped["primer"].startswith("omitted: byte cap"))
        self.assertTrue(capped["results"])

    def test_render_drops_commits_then_leads_and_names_it(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from helper.orientation import render
        data = {"views": {"pitfall": {"state": "leads_found", "eligible": 1}},
                "leads": [{"category": c, "excerpt": c * 60, "ref": "r@1#abc"} for c in ("pitfall", "task")],
                "commits": [{"sha": "f" * 40, "committed_at": None, "subject": "s" * 70, "files_changed": 1,
                             "additions": 1, "deletions": 0} for _ in range(3)],
                "tree": {"changed": 0, "untracked": 0},
                "coverage": {"sessions": {}, "commits": {}}}
        lines = render(data, 512)
        self.assertLessEqual(len("\n".join(lines[:-1]).encode()), 512)
        self.assertTrue(lines[1].startswith("  pitfall:"))
        self.assertEqual(lines[-2], "  tree: clean")
        self.assertIn("primer omitted: byte cap (--primer-bytes 512)", lines[-1])

    def test_parent_root_is_not_searched_implicitly(self):
        parent = self.home / "parent"
        nested = parent / "sub" / "repo"
        nested.mkdir(parents=True)
        self.repo = nested
        self.write("a.py", "x = 1\n")
        self.recording(root=parent, rule="PARENT rule must not leak into the nested repo.")
        self.assertNotIn("PARENT rule", self.cli())

    def test_symlinked_root_matches_the_resolved_project(self):
        link = self.home / "link"
        try:
            link.symlink_to(self.repo, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.recording(root=link, rule="SYMLINKED rule must hold for the importer.")
        self.assertIn("SYMLINKED rule", self.cli())


if __name__ == "__main__":
    unittest.main()
