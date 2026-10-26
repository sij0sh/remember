import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / "scripts/sessions.py"
ROWS = ROOT / "scripts/rows.py"
try:
    import tree_sitter, tree_sitter_bash, tree_sitter_go, tree_sitter_rust, tree_sitter_typescript  # noqa: F401
    TREE_SITTER = True
except ImportError:
    TREE_SITTER = False
GIT = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]


def pi(role, body, mid, parent=None):
    return {"type": "message", "id": mid, "parentId": parent,
            "message": {"role": role, "content": body}}


def claude(kind, content, uuid, parent=None, cwd=None):
    record = {"type": kind, "uuid": uuid, "parentUuid": parent, "sessionId": "claude-session",
              "message": {"content": content}}
    if cwd:
        record["cwd"] = cwd
    return record


def pi_commit(root, command, output, sid="s1", call="c1"):
    return [{"type": "session", "id": sid, "cwd": root},
            pi("assistant", [{"type": "toolCall", "id": call, "name": "bash",
                              "arguments": {"command": command}}], "a" + call),
            {"type": "message", "id": "r" + call, "parentId": "a" + call, "message": {
                "role": "toolResult", "toolCallId": call, "isError": False,
                "content": [{"type": "text", "text": output}]}}]


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.root = str(self.directory.resolve())

    def tearDown(self):
        self.temp.cleanup()

    def write(self, records, name="session.jsonl"):
        path = self.directory / name
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *GIT, *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    def repo(self, name="repo"):
        repo = self.directory / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        return repo

    def commit(self, repo, message, **files):
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message)
        return self.git(repo, "rev-parse", "HEAD")

    def select(self, *args, max_bytes=60000, env=None):
        result = subprocess.run([sys.executable, str(SESSIONS), "select", *map(str, args),
                                 "--project", "", "--max-bytes", str(max_bytes)],
                                capture_output=True, text=True, timeout=60, cwd=self.root, env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(result.stderr, result.stderr)
        return json.loads(result.stdout)

    def sql(self, path, query, *args, env=None):
        return self.select(path, *args, "--sql", query, env=env)

    def links(self, path, *args):
        return self.sql(path, "SELECT * FROM commit_links ORDER BY command_offset", "--include", "git", *args)["results"]

    def run_select(self, *args):
        result = subprocess.run([sys.executable, str(SESSIONS), "select", *map(str, args), "--max-bytes", "60000"],
                                capture_output=True, text=True, timeout=60, cwd=self.root)
        return result.returncode, json.loads(result.stdout)

    def test_include_reads_the_project_repository_not_the_invocation_directory(self):
        repo = self.repo()
        self.commit(repo, "feat: one", **{"a.txt": "1\n"})
        code, value = self.run_select("--project", repo, "--include", "git", "--sql", "SELECT count(*) AS n FROM commits")
        self.assertEqual(code, 0, value)
        self.assertEqual(value["scope"]["repository"]["repo"], str(repo.resolve()))
        self.assertEqual(value["results"], [{"n": 1}])
        self.assertTrue(value["delivery"]["complete"])
        other = self.repo("other")
        code, value = self.run_select("--project", repo, "--repo", other, "--include", "git",
                                      "--sql", "SELECT count(*) AS n FROM commits")
        self.assertEqual(code, 2)
        self.assertIn("different directories", value["results"][0]["error"])

    def test_unknown_view_names_the_likely_view(self):
        repo = self.repo()
        self.commit(repo, "feat: one", **{"a.txt": "1\n"})
        code, value = self.run_select("--project", repo, "--include", "edits", "--sql", "SELECT count(*) FROM edits")
        self.assertEqual(code, 2)
        self.assertIn("did you mean edit_hunks?", value["results"][0]["error"])

    # Witnesses: one row per event-to-commit pairing.

    def test_pi_commit_witness_from_output(self):
        path = self.write(pi_commit(self.root, "git commit -m 'feat: first'", "[main abc1234] feat: first\n 1 file"))
        link, = self.links(path)
        self.assertEqual((link["recorded_sha"], link["harness"], link["session"], link["subject"]),
                         ("abc1234", "pi", "s1", "feat: first"))
        self.assertIsNone(link["sha"])  # a short SHA without a repository is not verified
        self.assertEqual((link["basis"], link["confidence"]), ("commit_output", "recorded"))
        self.assertIn("feat: first", link["output_excerpt"])

    def test_full_sha_is_kept_without_repository(self):
        long_sha = "a" * 40
        path = self.write(pi_commit(self.root, "git commit -m 'feat: witness'", f"[main {long_sha}] feat: witness"))
        link, = self.links(path)
        self.assertEqual(link["sha"], long_sha)
        self.assertEqual(link["ref"]["file"], str(path))

    def test_quiet_commit_inferred_from_repo_subject(self):
        repo = self.repo()
        head = self.commit(repo, "fix: quiet", **{"file.txt": "one\n"})
        path = self.write([
            claude("user", "commit it", "u", cwd=str(repo)),
            claude("assistant", [{"type": "tool_use", "id": "c", "name": "Bash",
                                  "input": {"command": "git commit -q -F - <<'EOF'\nfix: quiet\nEOF"}}], "a", "u", cwd=str(repo)),
            claude("user", [{"type": "tool_result", "tool_use_id": "c", "content": "", "is_error": False}], "r", "a", cwd=str(repo))])
        link, = self.links(path, "--repo", repo)
        self.assertEqual((link["sha"], link["basis"], link["confidence"]), (head, "repo_subject", "inferred"))

    def test_ambiguous_subject_stays_unresolved(self):
        repo = self.repo()
        self.commit(repo, "chore: same", **{"a.txt": "1\n"})
        self.commit(repo, "chore: same", **{"a.txt": "2\n"})
        path = self.write([
            claude("assistant", [{"type": "tool_use", "id": "c", "name": "Bash",
                                  "input": {"command": "git commit -q -m 'chore: same'"}}], "a", cwd=str(repo)),
            claude("user", [{"type": "tool_result", "tool_use_id": "c", "content": "", "is_error": False}], "r", "a", cwd=str(repo))])
        link, = self.links(path, "--repo", repo)
        self.assertIsNone(link["sha"])
        self.assertEqual((link["basis"], link["confidence"]), ("ambiguous_subject", "unresolved"))

    def test_failed_quiet_commit_is_kept_unresolved(self):
        repo = self.repo()
        self.commit(repo, "fix: denied", **{"file.txt": "one\n"})
        path = self.write([
            claude("assistant", [{"type": "tool_use", "id": "c", "name": "Bash",
                                  "input": {"command": "git commit -q -m 'fix: denied'"}}], "a", cwd=str(repo)),
            claude("user", [{"type": "tool_result", "tool_use_id": "c", "content": "denied", "is_error": True}], "r", "a", cwd=str(repo))])
        value = self.sql(path, "SELECT * FROM commit_links", "--include", "git", "--repo", repo)
        link, = value["results"]
        self.assertEqual((link["sha"], link["confidence"], link["failed"]), (None, "unresolved", True))
        self.assertEqual(value["scope"]["repository"]["git"]["unresolved_witnesses"], 1)

    def test_codex_command_output(self):
        path = self.write([
            {"type": "session_meta", "payload": {"id": "codex-session", "cwd": self.root}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "command": ["bash", "-lc", "git commit -m 'feat: codex'"],
                "status": "completed", "exit_code": 0,
                "aggregated_output": "[main def5678] feat: codex\n 2 files changed"}}}])
        link, = self.links(path)
        self.assertEqual((link["recorded_sha"], link["harness"], link["session"]), ("def5678", "codex", "codex-session"))

    def test_multiple_witnesses_become_one_event_array(self):
        path = self.write([
            claude("user", "make two commits", "u"),
            claude("assistant", [{"type": "tool_use", "id": "c", "name": "Bash", "input": {
                "command": "git commit -q -m 'feat: one' && git commit -q -m 'fix: two' && git log --oneline -3"}}], "a", "u"),
            claude("user", [{"type": "tool_result", "tool_use_id": "c", "is_error": False, "content":
                             "aaaa111 fix: two\nbbbb222 feat: one\ncccc333 old"}], "r", "a")])
        self.assertEqual({(row["recorded_sha"], row["subject"]) for row in self.links(path)},
                         {("aaaa111", "fix: two"), ("bbbb222", "feat: one")})
        value = self.select(path, "--include", "git", "--kind", "tool_call", "--fields", "kind")
        row, = value["harnesses"][0]["results"]
        self.assertEqual(sorted(item["recorded_sha"] for item in row["git"]), ["aaaa111", "bbbb222"])
        counted = self.sql(path, "SELECT count(*) AS events, count(DISTINCT event_id) AS distinct_events "
                                 "FROM events WHERE kind = 'tool_call'", "--include", "git")
        self.assertEqual(counted["results"], [{"events": 1, "distinct_events": 1}])

    def test_same_short_sha_in_separate_recordings_stays_separate(self):
        first = self.write(pi_commit(self.root, "git commit -m 'feat: one'", "[main abc1234] feat: one"), "one.jsonl")
        second = self.write(pi_commit(self.root, "git commit -m 'feat: two'", "[main abc1234] feat: two", "s2", "c2"), "two.jsonl")
        rows = self.sql(first, "SELECT recording_id, session FROM commit_links ORDER BY session",
                        second, "--include", "git")["results"]
        self.assertEqual([row["session"] for row in rows], ["s1", "s2"])
        self.assertEqual(len({row["recording_id"] for row in rows}), 2)

    def test_witnesses_are_derived_before_event_filters(self):
        path = self.write(pi_commit(self.root, "git commit -m 'feat: kept'", "[main abc4321] feat: kept"))
        value = self.sql(path, "SELECT (SELECT count(*) FROM events) AS events, "
                               "(SELECT count(*) FROM commit_links) AS links",
                         "--include", "git", "--role", "user")
        self.assertEqual(value["results"], [{"events": 0, "links": 1}])

    # Left joins: unmatched rows survive, empty tables still bind.

    def test_unmatched_events_and_empty_tables_left_join(self):
        path = self.write(pi_commit(self.root, "echo hi", "hi"))
        value = self.sql(path, "SELECT e.kind, l.sha, c.subject, f.path FROM events e "
                               "LEFT JOIN commit_links l USING (recording_id, event_id) "
                               "LEFT JOIN commits c USING (repo_id, sha) "
                               "LEFT JOIN changes d USING (repo_id, sha) "
                               "LEFT JOIN files f ON f.repo_id = d.repo_id AND f.path = d.new_path ORDER BY e.kind",
                         "--include", "git,files,symbols")
        self.assertEqual([row["kind"] for row in value["results"]], ["config", "tool_call", "tool_result"])
        self.assertTrue(all(row["sha"] is None and row["path"] is None for row in value["results"]))
        self.assertEqual(value["status"], "partial")  # no repository at the invocation directory
        self.assertIn("files", value["omissions"]["incomplete_inputs"])
        rows = self.select(path, "--include", "git,files", "--kind", "tool_call")["harnesses"][0]["results"]
        self.assertEqual((rows[0]["git"], rows[0]["file"]), ([], None))

    def test_spec_query_joins_events_commits_changes_files(self):
        repo = self.repo()
        self.commit(repo, "feat: base", **{"src/app.py": "print(1)\n"})
        output = self.git(repo, "commit", "--allow-empty", "-m", "feat: linked")  # recorded commit output
        path = self.write(pi_commit(str(repo), "git commit --allow-empty -m 'feat: linked'", output))
        sha = self.git(repo, "rev-parse", "HEAD")
        value = self.sql(path, "SELECT e.session, c.sha, c.subject, f.path, f.lines FROM events e "
                               "LEFT JOIN commit_links l USING (recording_id, event_id) "
                               "LEFT JOIN commits c USING (repo_id, sha) "
                               "LEFT JOIN changes d USING (repo_id, sha) "
                               "LEFT JOIN files f ON f.repo_id = d.repo_id AND f.path = d.new_path "
                               "WHERE c.sha IS NOT NULL",
                         "--include", "git,files", "--repo", repo)
        self.assertEqual(value["results"], [{"session": "s1", "sha": sha, "subject": "feat: linked",
                                             "path": None, "lines": None}])
        self.assertNotIn("status", value)

    # Normalized history: commits and changes against a stated parent.

    def test_changes_renames_deletions_and_merge_parent(self):
        repo = self.repo()
        root = self.commit(repo, "feat: add", **{"a.txt": "one\ntwo\n", "gone.txt": "x\n"})
        self.git(repo, "mv", "a.txt", "b.txt")
        (repo / "gone.txt").unlink()
        renamed = self.commit(repo, "refactor: rename")
        self.git(repo, "checkout", "-q", "-b", "side")
        side = self.commit(repo, "feat: side", **{"side.txt": "s\n"})
        self.git(repo, "checkout", "-q", "main")
        self.commit(repo, "feat: main", **{"main.txt": "m\n"})
        self.git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side")
        merge = self.git(repo, "rev-parse", "HEAD")
        path = self.write(pi_commit(self.root, "echo", "ok"))
        changes = self.sql(path, "SELECT sha, parent_sha, status, old_path, new_path, path, additions "
                                 "FROM changes ORDER BY sha, path", "--include", "git", "--repo", repo)["results"]
        by = {(row["sha"], row["path"]): row for row in changes}
        self.assertIsNone(by[(root, "a.txt")]["parent_sha"])
        self.assertEqual((by[(root, "a.txt")]["status"], by[(root, "a.txt")]["additions"]), ("added", 2))
        self.assertEqual({k: by[(renamed, "b.txt")][k] for k in ("status", "old_path", "new_path")},
                         {"status": "renamed", "old_path": "a.txt", "new_path": "b.txt"})
        self.assertEqual({k: by[(renamed, "gone.txt")][k] for k in ("status", "new_path")},
                         {"status": "deleted", "new_path": None})
        merged = [row for row in changes if row["sha"] == merge]
        self.assertEqual([row["path"] for row in merged], ["side.txt"])  # first-parent diff
        commits = self.sql(path, "SELECT sha, parent_count, is_merge, parents FROM commits WHERE is_merge",
                           "--include", "git", "--repo", repo)["results"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["parents"].count(side), 1)

    def test_follow_limits_history_to_one_path(self):
        repo = self.repo()
        self.commit(repo, "feat: add", **{"a.txt": "one\n", "other.txt": "o\n"})
        self.git(repo, "mv", "a.txt", "b.txt")
        self.commit(repo, "refactor: rename")
        self.commit(repo, "chore: other", **{"other.txt": "p\n"})
        path = self.write(pi_commit(self.root, "echo", "ok"))
        subjects = self.sql(path, "SELECT subject FROM commits ORDER BY committed_at, subject",
                            "--include", "git", "--repo", repo, "--follow", "b.txt")["results"]
        self.assertEqual({row["subject"] for row in subjects}, {"feat: add", "refactor: rename"})

    def test_commit_limit_marks_partial(self):
        repo = self.repo()
        for n in range(3):
            self.commit(repo, f"feat: {n}", **{"a.txt": f"{n}\n"})
        path = self.write(pi_commit(self.root, "echo", "ok"))
        value = self.sql(path, "SELECT count(*) AS n FROM commits", "--include", "git", "--repo", repo,
                         "--max-commits", "2")
        self.assertEqual(value["results"], [{"n": 2}])
        self.assertEqual(value["status"], "partial")
        self.assertFalse(value["scope"]["repository"]["git"]["complete"])

    # Current files: dirty state, limits, and opt-in structure.

    def test_files_dirty_untracked_deleted_and_event_file_object(self):
        repo = self.repo()
        self.commit(repo, "feat: files", **{"src/app.py": "import os\n\nclass A:\n    def run(self):\n        pass\n",
                                            "gone.txt": "x\n", ".gitignore": "ignored.log\n"})
        (repo / "src/app.py").write_text("import os\nimport sys\n")
        (repo / "gone.txt").unlink()
        (repo / "new.md").write_text("# new\n")
        (repo / "ignored.log").write_text("noise\n")
        path = self.write([
            claude("assistant", [{"type": "tool_use", "id": "c", "name": "Read",
                                  "input": {"file_path": str(repo / "src/app.py")}}], "a", cwd=str(repo)),
            claude("user", [{"type": "tool_result", "tool_use_id": "c", "content": "x", "is_error": False}], "r", "a", cwd=str(repo))])
        files = {row["path"]: row for row in self.sql(
            path, "SELECT * FROM files", "--include", "files", "--repo", repo)["results"]}
        self.assertNotIn("ignored.log", files)
        self.assertEqual((files["src/app.py"]["modified"], files["src/app.py"]["lines"],
                          files["src/app.py"]["language"]), (True, 2, "python"))
        self.assertEqual((files["gone.txt"]["exists"], files["gone.txt"]["worktree_status"]), (False, "D"))
        self.assertEqual((files["new.md"]["tracked"], files["new.md"]["index_status"]), (False, "?"))
        self.assertEqual(files["src/app.py"]["blob_sha"],
                         self.git(repo, "hash-object", "src/app.py"))
        row, = self.select(path, "--include", "files", "--kind", "tool_call", "--repo", repo)["harnesses"][0]["results"]
        self.assertEqual((row["repo_path"], row["file"]["path"], row["file"]["lines"]), ("src/app.py", "src/app.py", 2))

    def test_file_limits_and_symbols(self):
        repo = self.repo()
        self.commit(repo, "feat: code", **{"a.py": "from . import b\n\nasync def go():\n    pass\n\nclass K:\n    def m(self):\n        pass\n",
                                           "b.rb": "def x; end\n", "c.py": "def broken(:\n",
                                           "big.txt": "x" * 2000})
        path = self.write(pi_commit(self.root, "echo", "ok"))
        items = self.sql(path, "SELECT kind, qualname, module, line FROM code_items ORDER BY line, qualname",
                         "--include", "symbols", "--repo", repo)["results"]
        self.assertEqual([(i["kind"], i["qualname"]) for i in items],
                         [("import", ".b"), ("async_function", "go"), ("class", "K"), ("method", "K.m")])
        # b.rb is not a Python module, so the relative import keeps its literal module and no target.
        targets = self.sql(path, "SELECT module, target_path, target_state, target_rule FROM code_items "
                           "WHERE kind = 'import'", "--include", "symbols", "--repo", repo)
        self.assertEqual(targets["results"], [{"module": ".", "target_path": None, "target_state": "unresolved",
                                               "target_rule": "python_relative"}])
        self.assertEqual(targets["scope"]["repository"]["files"]["import_targets"], {"unresolved": 1})
        states = {row["path"]: (row["parse_state"], row["read_state"]) for row in self.sql(
            path, "SELECT path, parse_state, read_state FROM files", "--include", "symbols", "--repo", repo,
            "--max-file-bytes", "1000")["results"]}
        self.assertEqual(states, {"a.py": ("parsed", "read"), "b.rb": ("unsupported", "read"),
                                  "c.py": ("syntax_error", "read"), "big.txt": ("not_requested", "too_large")})
        limited = self.sql(path, "SELECT count(*) AS n FROM files", "--include", "files", "--repo", repo,
                           "--max-repo-files", "2")
        self.assertEqual((limited["results"], limited["status"]), ([{"n": 2}], "partial"))

    @unittest.skipUnless(TREE_SITTER, "tree-sitter grammars not installed")
    def test_tree_sitter_symbols(self):
        repo = self.repo()
        self.commit(repo, "feat: polyglot", **{
            "lib.rs": "use std::{io::{self, Read}, fs as f};\nmod m {\n    pub struct S;\n"
                      "    impl Tr for S {\n        fn go(&self) {}\n    }\n}\n",
            "app.ts": "import d, { a as b } from './x';\nconst r = require('q');\n"
                      "class C {\n    m() {}\n}\nconst o = { n() {} };\nexport const f = async () => 1;\n",
            "main.go": 'package p\nimport f "fmt"\nfunc (s *S) M() {}\ntype S struct{}\n',
            "run.sh": 'source ./lib.sh\n. "$HOME/x"\nmain() { :; }\n',
            "bad.js": "function (\n"})
        path = self.write(pi_commit(self.root, "echo", "ok"))
        items = self.sql(path, "SELECT path, kind, name, qualname, parent, module, language FROM code_items "
                         "ORDER BY path, line, qualname", "--include", "symbols", "--repo", repo)["results"]
        got = [(i["path"], i["kind"], i["name"], i["qualname"], i["parent"], i["module"]) for i in items]
        self.assertEqual(got, [
            ("app.ts", "import", "b", "./x#a", None, "./x"),
            ("app.ts", "import", "d", "./x#default", None, "./x"),
            ("app.ts", "import", "r", "q", None, "q"),
            ("app.ts", "class", "C", "C", None, None),
            ("app.ts", "method", "m", "C.m", "C", None),
            ("app.ts", "async_function", "f", "f", None, None),
            ("lib.rs", "import", "f", "std::fs", None, "std"),
            ("lib.rs", "import", "io", "std::io", None, "std"),
            ("lib.rs", "import", "Read", "std::io::Read", None, "std::io"),
            ("lib.rs", "module", "m", "m", None, None),
            ("lib.rs", "struct", "S", "m::S", "m", None),
            ("lib.rs", "impl", "S", "m::<S as Tr>", "m", None),
            ("lib.rs", "method", "go", "m::<S as Tr>::go", "m::<S as Tr>", None),
            ("main.go", "import", "f", "fmt", None, "fmt"),
            ("main.go", "method", "M", "S.M", "S", None),
            ("main.go", "struct", "S", "S", None, None),
            ("run.sh", "import", "./lib.sh", "./lib.sh", None, "./lib.sh"),
            ("run.sh", "function", "main", "main", None, None)])
        states = {row["path"]: row["parse_state"] for row in self.sql(
            path, "SELECT path, parse_state FROM files", "--include", "symbols", "--repo", repo)["results"]}
        self.assertEqual(states, {"lib.rs": "parsed", "app.ts": "parsed", "main.go": "parsed",
                                  "run.sh": "parsed", "bad.js": "parsed_with_errors"})

    def test_missing_grammar_is_parser_unavailable(self):
        repo = self.repo()
        self.commit(repo, "feat: mixed", **{"a.py": "def f():\n    pass\n", "b.rs": "fn g() {}\n"})
        # A shadowing module on PYTHONPATH makes the Rust grammar fail to import.
        stubs = self.directory / "stubs"
        stubs.mkdir()
        (stubs / "tree_sitter_rust.py").write_text("raise ImportError('grammar removed for test')\n")
        env = {**os.environ, "PYTHONPATH": str(stubs)}
        path = self.write(pi_commit(self.root, "echo", "ok"))
        result = self.sql(path, "SELECT f.path, f.parse_state, count(i.kind) AS items FROM files f "
                          "LEFT JOIN code_items i USING (repo_id, path) GROUP BY ALL ORDER BY f.path",
                          "--include", "symbols", "--repo", repo, env=env)
        self.assertEqual([(r["path"], r["parse_state"], r["items"]) for r in result["results"]],
                         [("a.py", "parsed", 1), ("b.rs", "parser_unavailable", 0)])
        self.assertEqual(result["scope"]["repository"]["files"]["parsers_unavailable"], ["rust"])

    @unittest.skipUnless(TREE_SITTER, "tree-sitter grammars not installed")
    def test_shebang_sets_language_for_extensionless_scripts(self):
        repo = self.repo()
        self.commit(repo, "feat: scripts", **{"bin/deploy": "#!/usr/bin/env bash\nmain() { :; }\n",
                                              "bin/tool": "#!/usr/bin/python3\ndef run():\n    pass\n",
                                              "bin/data": "plain text\n", "notes.txt": "#!/bin/sh\n"})
        path = self.write(pi_commit(self.root, "echo", "ok"))
        files = {r["path"]: (r["language"], r["parse_state"]) for r in self.sql(
            path, "SELECT path, language, parse_state FROM files", "--include", "symbols", "--repo", repo)["results"]}
        self.assertEqual(files, {"bin/deploy": ("shell", "parsed"), "bin/tool": ("python", "parsed"),
                                 "bin/data": (None, "unsupported"), "notes.txt": ("text", "unsupported")})
        items = self.sql(path, "SELECT path, qualname FROM code_items ORDER BY path", "--include", "symbols",
                         "--repo", repo)["results"]
        self.assertEqual([(i["path"], i["qualname"]) for i in items], [("bin/deploy", "main"), ("bin/tool", "run")])

    # Edit hunks: recorded line ranges and the definitions they changed.

    def hunks(self, path, repo, query="SELECT * FROM edit_hunks ORDER BY harness, path, hunk"):
        return self.sql(path, query, "--include", "edits", "--repo", repo)

    def test_claude_edit_names_symbols_at_edit(self):
        repo = self.repo()
        before = "def keep():\n    return 1\n\n\ndef target():\n    return 1\n"
        after = before[:-2] + "2\n"
        self.commit(repo, "feat: code", **{"a.py": after})
        result = {"filePath": f"{repo}/a.py", "oldString": "    return 1\n", "newString": "    return 2\n",
                  "originalFile": before, "structuredPatch": [
                      {"oldStart": 5, "oldLines": 2, "newStart": 5, "newLines": 2,
                       "lines": [" def target():", "-    return 1", "+    return 2"]}]}
        record = claude("user", [{"type": "tool_result", "tool_use_id": "c", "content": "ok", "is_error": False}],
                        "r", "a", cwd=str(repo))
        record["toolUseResult"] = result
        path = self.write([
            claude("assistant", [{"type": "tool_use", "id": "c", "name": "Edit",
                                  "input": {"file_path": f"{repo}/a.py", "old_string": "x", "new_string": "y"}}],
                   "a", cwd=str(repo)), record])
        out = self.hunks(path, repo)
        hunk, = out["results"]
        self.assertEqual({k: hunk[k] for k in ("harness", "path", "change", "source", "added", "removed",
                                               "changed_start", "changed_end", "anchor", "current_start",
                                               "symbol_basis")},
                         {"harness": "claude-code", "path": "a.py", "change": "update",
                          "source": "claude_structured_patch", "added": 1, "removed": 1, "changed_start": 6,
                          "changed_end": 6, "anchor": "exact", "current_start": 6, "symbol_basis": "at_edit"})
        symbols = self.hunks(path, repo, "SELECT qualname, basis, changed_lines, innermost FROM edit_symbols")["results"]
        self.assertEqual(symbols, [{"qualname": "target", "basis": "at_edit", "changed_lines": 1, "innermost": True}])
        self.assertEqual(out["scope"]["repository"]["edits"]["symbol_basis"], {"at_edit": 1})

    def test_codex_hunks_anchor_to_current_file(self):
        repo = self.repo()
        self.commit(repo, "feat: rust", **{"lib.rs": "// head\nfn a() {}\nfn b() {}\n"})
        changes = {f"{repo}/lib.rs": {"type": "update", "move_path": None, "unified_diff":
                                     "@@ -1,1 +1,2 @@\n fn a() {}\n+fn b() {}\n@@ -9,1 +10,1 @@\n-x\n+gone\n"},
                   f"{repo}/gone.rs": {"type": "add", "content": "fn g() {}\n"}}
        path = self.write([
            {"type": "session_meta", "payload": {"id": "codex-session", "cwd": str(repo)}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "id": "p", "type": "FileChange", "changes": changes, "status": "completed"}}}])
        rows = {(r["path"], r["hunk"]): (r["anchor"], r["current_start"], r["current_end"], r["symbol_basis"])
                for r in self.hunks(path, repo)["results"]}
        self.assertEqual(rows, {("lib.rs", 0): ("shifted", 3, 3, "current"),
                                ("lib.rs", 1): ("not_found", None, None, "none"),
                                ("gone.rs", 0): ("file_missing", None, None, "at_edit")})
        symbols = self.hunks(path, repo, "SELECT path, qualname, basis, line FROM edit_symbols ORDER BY path")["results"]
        # An added file's recorded content is the post-edit file, so it names symbols at_edit.
        self.assertEqual(symbols, [{"path": "gone.rs", "qualname": "g", "basis": "at_edit", "line": 1},
                                   {"path": "lib.rs", "qualname": "b", "basis": "current", "line": 3}])

    def test_hunks_outside_the_repository_are_labelled(self):
        repo = self.repo()
        self.commit(repo, "feat: rust", **{"lib.rs": "fn a() {}\n"})
        changes = {f"{repo}/lib.rs": {"type": "update", "move_path": None,
                                     "unified_diff": "@@ -1,1 +1,1 @@\n-fn x() {}\n+fn a() {}\n"},
                   "/tmp/scratch.py": {"type": "add", "content": "print(1)\n"}}
        path = self.write([
            {"type": "session_meta", "payload": {"id": "codex-session", "cwd": str(repo)}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "id": "p", "type": "FileChange", "changes": changes, "status": "completed"}}}])
        out = self.hunks(path, repo, "SELECT path, location, recorded_path FROM edit_hunks ORDER BY location")
        self.assertEqual(out["results"], [
            {"path": None, "location": "outside_repo", "recorded_path": "/tmp/scratch.py"},
            {"path": "lib.rs", "location": "repository", "recorded_path": f"{repo}/lib.rs"}])
        self.assertEqual(out["scope"]["repository"]["edits"]["locations"], {"outside_repo": 1, "repository": 1})

    def test_pi_edit_patch_and_failed_edit(self):
        repo = self.repo()
        self.commit(repo, "feat: sh", **{"run.sh": "main() {\n  echo hi\n}\n"})

        def edit(call, patch, failed=False):
            return [pi("assistant", [{"type": "toolCall", "id": call, "name": "edit",
                                      "arguments": {"path": f"{repo}/run.sh", "edits": []}}], "a" + call),
                    {"type": "message", "id": "r" + call, "parentId": "a" + call, "message": {
                        "role": "toolResult", "toolCallId": call, "toolName": "edit", "isError": failed,
                        "content": [{"type": "text", "text": "done"}],
                        "details": None if failed else {"patch": patch}}}]
        path = self.write([{"type": "session", "id": "s1", "cwd": str(repo)},
                           *edit("c1", "--- run.sh\n+++ run.sh\n@@ -2,1 +2,1 @@\n-  echo old\n+  echo hi\n"),
                           *edit("c2", None, failed=True)])
        out = self.hunks(path, repo)
        hunk, = out["results"]
        self.assertEqual((hunk["source"], hunk["anchor"], hunk["current_start"], hunk["symbol_basis"]),
                         ("pi_patch", "exact", 2, "current"))
        self.assertEqual(out["scope"]["repository"]["edits"]["failed_skipped"], 1)
        symbols = self.hunks(path, repo, "SELECT qualname FROM edit_symbols")["results"]
        self.assertEqual(symbols, [{"qualname": "main"}])

    def test_content_is_opt_in(self):
        repo = self.repo()
        self.commit(repo, "feat: text", **{"a.txt": "hello\n"})
        path = self.write(pi_commit(self.root, "echo", "ok"))
        plain = self.sql(path, "SELECT content FROM files", "--include", "files", "--repo", repo)["results"]
        full = self.sql(path, "SELECT content FROM files", "--include", "content", "--repo", repo)["results"]
        self.assertEqual((plain, full), ([{"content": None}], [{"content": "hello\n"}]))

    def test_unknown_include_is_an_error(self):
        path = self.write(pi_commit(self.root, "echo", "ok"))
        result = subprocess.run([sys.executable, str(SESSIONS), "select", str(path), "--project", "",
                                 "--include", "patches"], capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown --include", json.loads(result.stdout)["results"][0]["error"])

    def test_enriched_rows_pipe_into_rows_py(self):
        path = self.write(pi_commit(self.root, "git commit -m 'feat: pipe'", "[main abc9999] feat: pipe"))
        envelope = subprocess.run([sys.executable, str(SESSIONS), "select", str(path), "--project", "",
                                   "--include", "git", "--kind", "tool_call", "--fields", "kind"],
                                  capture_output=True, text=True, check=True, cwd=self.root).stdout
        rows = subprocess.run([sys.executable, str(ROWS),
                               "SELECT git->>'$[0].recorded_sha' AS sha, git->>'$[0].subject' AS subject FROM rows"],
                              input=envelope, capture_output=True, text=True, check=True, cwd=self.root)
        self.assertEqual(json.loads(rows.stdout)["results"], [{"sha": "abc9999", "subject": "feat: pipe"}])


    def test_duplicates_merges_aligned_windows_without_reading_recordings(self):
        body = "def score(items):\n    total = 0\n    for item in items:\n    total = total + item.weight * 0\n    total = total + item.weight * 1\n    total = total + item.weight * 2\n    total = total + item.weight * 3\n    total = total + item.weight * 4\n    total = total + item.weight * 5\n    total = total + item.weight * 6\n    total = total + item.weight * 7\n    return total\n"
        repo = self.repo()
        self.commit(repo, "copies", **{"a.py": body, "b.py": "import os\n\n" + body, "c.py": "x = 1\n"})
        result = subprocess.run([sys.executable, str(SESSIONS), "duplicates", "--repo", str(repo)],
                                capture_output=True, text=True, timeout=60, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        value = json.loads(result.stdout)
        self.assertFalse(value["scope"]["recordings_read"])
        self.assertEqual(value["query"], "duplicates span=7 min_chars=200")
        # One merged region per pair, not one row per overlapping window; short leading windows fall under --min-chars.
        self.assertEqual(value["results"], [{"first": "a.py:3-12", "second": "b.py:5-14", "code_lines": 10}])
        refused = subprocess.run([sys.executable, str(SESSIONS), "duplicates", "--repo", str(repo), "--sql", "SELECT 1"],
                                 capture_output=True, text=True, timeout=60, cwd=self.root)
        self.assertEqual(refused.returncode, 2)

    def test_parameterized_repository_query_reads_no_recordings(self):
        repo = self.repo()
        self.commit(repo, "one", **{"a.py": "x = 1\n"})
        value = self.select("--repo", repo, "--include", "files", "--param", "p=a.py", "--sql",
                            "WITH f AS (SELECT path FROM files WHERE path = $p) "
                            "SELECT path, count(*) OVER (ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING) AS n FROM f")
        self.assertFalse(value["scope"]["recordings_read"])
        self.assertEqual(value["results"], [{"path": "a.py", "n": 1}])

    def test_cite_checks_full_commits_against_the_repository(self):
        repo = self.repo()
        sha = self.commit(repo, "one", **{"a.py": "x = 1\n"})
        report = self.directory / "report.md"
        report.write_text(f"Landed in {sha}; not {'f' * 40} or {sha[:7]}.\n")
        result = subprocess.run([sys.executable, str(SESSIONS), "cite", str(report), "--repo", str(repo)],
                                capture_output=True, text=True, timeout=60, cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stderr)
        states = {r["citation"]: r["state"] for r in json.loads(result.stdout)["results"]}
        self.assertEqual(states, {sha: "ok", "f" * 40: "not_in_repo", sha[:7]: "ambiguous"})


if __name__ == "__main__":
    unittest.main()
