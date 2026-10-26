import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper.envelope import LEVELS  # noqa: E402
from helper.options import command_line  # noqa: E402
SESSIONS = ROOT / "scripts/sessions.py"
ROWS = ROOT / "scripts/rows.py"


def split_line(line):
    """Inverse of command_line: POSIX shell words, or the Windows argv parse."""
    if os.name != "nt":
        return shlex.split(line)
    import ctypes
    from ctypes import wintypes
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    argv = parse(line, ctypes.byref(count))
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def pi(role, body, mid, parent=None):
    return {"type": "message", "id": mid, "parentId": parent,
            "message": {"role": role, "content": body}}


def only(value):
    """The single harness group of a grouped envelope."""
    groups = value["harnesses"]
    assert len(groups) == 1, [g["harness"] for g in groups]
    return groups[0]


def claude(kind, content, uuid, parent=None, **extra):
    return {"type": kind, "uuid": uuid, "parentUuid": parent, "sessionId": "claude-session",
            "message": {"content": content}, **extra}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.root = str(self.directory.resolve())

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, records):
        path = self.directory / name
        path.write_text("".join(json.dumps(v) + "\n" for v in records))
        return path

    def run_cli(self, *args, code=0, script=SESSIONS, stdin=None, cwd=None, env=None):
        result = subprocess.run([sys.executable, str(script), *map(str, args)], input=stdin,
                                capture_output=True, timeout=20, cwd=cwd or self.root,
                                env={**os.environ, **(env or {})})
        self.assertEqual(result.returncode, code, result.stderr.decode() + result.stdout.decode())
        self.assertFalse(result.stderr, result.stderr.decode())
        value = json.loads(result.stdout)
        return value, result.stdout

    def fixture(self):
        return self.write("pi.jsonl", [
            {"type": "session", "id": "s1", "cwd": self.root},
            pi("user", "Fix importer and verify it", "u1"),
            pi("assistant", [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "pytest importer"}}], "a1", "u1"),
            {"type": "message", "id": "r1", "parentId": "a1", "message": {"role": "toolResult", "toolCallId": "c1", "isError": True, "details": {"exitCode": 1}, "content": [{"type": "text", "text": "FAILED: missing configuration"}]}},
            pi("user", "Please preserve offline operation", "u2", "a1"),
            pi("assistant", "I will preserve it", "a2", "u2")])

    def test_pi_overview_and_exact_exchange(self):
        path = self.fixture()
        value, raw = self.run_cli("overview", path)
        self.assertLessEqual(len(raw), 8192)
        self.assertEqual(only(value)["harness"], "pi")
        self.assertEqual(only(value)["activity"]["operation_outcomes"]["failed"], 1)
        requests = only(value)["requests"]
        self.assertEqual({e["text"] for e in requests}, {"Fix importer and verify it", "Please preserve offline operation"})
        search, _ = self.run_cli("search", path, "--kind", "tool_call")
        call = only(search)["results"][0]
        self.assertEqual(call["exit_code"], 1)
        exchange, _ = self.run_cli("context", "--ref", json.dumps(call["ref"]), "--mode", "exchange")
        self.assertEqual([e["kind"] for e in exchange["results"]], ["tool_call", "tool_result"])

    def test_claude_tools_and_summary_not_user(self):
        path = self.write("claude.jsonl", [
            {"type": "user", "uuid": "u", "sessionId": "s", "cwd": self.root, "message": {"content": "Update reader"}},
            {"type": "assistant", "uuid": "a", "parentUuid": "u", "message": {"content": [{"type": "tool_use", "id": "c", "name": "Read", "input": {"file_path": f"{self.root}/reader.py"}}]}},
            {"type": "user", "uuid": "r", "parentUuid": "a", "message": {"content": [{"type": "tool_result", "tool_use_id": "c", "content": "source text"}]}},
            {"type": "user", "isCompactSummary": True, "message": {"content": "User always requires random thing"}}])
        value, _ = self.run_cli("overview", path)
        self.assertEqual(only(value)["activity"]["request_count"], 1)
        calls, _ = self.run_cli("search", path, "--path", "reader.py", "--kind", "tool_call")
        self.assertEqual(only(calls)["results"][0]["outcome"], "succeeded")
        self.assertEqual(only(calls)["results"][0]["session"], "s")

    def test_codex_completed_and_fallback(self):
        path = self.write("codex.jsonl", [
            {"type": "session_meta", "payload": {"id": "s", "cwd": self.root}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "echoed request"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t", "item": {"id": "u", "type": "UserMessage", "content": [{"type": "text", "text": "actual request"}]}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": ["bash", "-lc", "cargo test"], "status": "completed", "exit_code": 0}}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done claim"}]}}])
        value, _ = self.run_cli("overview", path)
        group = only(value)
        self.assertEqual(group["activity"]["request_count"], 2)
        self.assertIn("actual request", {e["text"] for e in group["requests"]})
        self.assertIn("echoed request", {e["text"] for e in group["requests"]})
        self.assertEqual(group["activity"]["events"]["assistant_message"], 1)
        self.assertEqual(group["activity"]["operation_outcomes"]["succeeded"], 1)
        self.assertEqual(group["scope"]["duplicates_skipped"], 0)
        self.assertEqual(value["scope"]["duplicates_skipped"], 0)

    def test_codex_response_and_completed_copies_are_one_request(self):
        def response(text):
            return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                         "content": [{"type": "input_text", "text": text}]}}

        def completed(mid, text):
            return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": f"t-{mid}", "item": {
                "id": mid, "type": "UserMessage", "content": [{"type": "text", "text": text}]}}}

        def answer(text):
            return {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                         "content": [{"type": "output_text", "text": text}]}}
        # Both orders occur; a request sent twice is two requests.
        path = self.write("codex.jsonl", [
            {"type": "session_meta", "payload": {"id": "s", "cwd": self.root}},
            response("commit and push"), completed("u1", "commit and push"), answer("pushed"),
            completed("u2", "commit and push"), response("commit and push"), answer("pushed again")])
        value, _ = self.run_cli("select", path, "--kind", "user_message", "--fields", "text,ref")
        group = only(value)
        self.assertEqual(len(group["results"]), 2)
        self.assertEqual(group["scope"]["duplicates_skipped"], 2)
        self.assertEqual({r["ref"]["line"] for r in group["results"]}, {3, 5})

    def test_pi_parallel_results_chain_to_their_calls(self):
        calls = [{"type": "toolCall", "id": f"c{i}", "name": "bash", "arguments": {"command": f"step {i}"}}
                 for i in range(3)]
        path = self.write("parallel.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            pi("user", "run three steps", "u"),
            pi("assistant", calls, "a", "u"),
            {"type": "message", "id": "r0", "parentId": "a", "message": {"role": "toolResult", "toolCallId": "c0", "content": []}},
            {"type": "message", "id": "r1", "parentId": "r0", "message": {"role": "toolResult", "toolCallId": "c1", "content": []}},
            {"type": "message", "id": "r2", "parentId": "r1", "message": {"role": "toolResult", "toolCallId": "c2", "isError": True, "content": []}},
            # A result whose recorded parent chain never reaches the call's message stays unpaired.
            pi("assistant", [{"type": "toolCall", "id": "c3", "name": "bash", "arguments": {"command": "late"}}], "a2", "r2"),
            {"type": "message", "id": "r3", "parentId": "u", "message": {"role": "toolResult", "toolCallId": "c3", "content": []}}])
        value, _ = self.run_cli("select", path, "--kind", "tool_call", "--fields", "command,outcome,result_ref")
        group = only(value)
        paired = {r["command"]: r.get("result_ref") is not None for r in group["results"]}
        self.assertEqual(paired, {"step 0": True, "step 1": True, "step 2": True, "late": False})
        self.assertEqual(group["results"][2]["outcome"], "failed")
        self.assertEqual(group["scope"]["unpaired_results"], 1)
        self.assertEqual(group["scope"]["calls_without_result"], 1)

    def test_branch_turn_not_record_proximity(self):
        path = self.write("branches.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            pi("user", "branch one", "u1"), pi("assistant", "first", "a1", "u1"),
            pi("user", "branch two", "u2", "a1"), pi("assistant", "second", "a2", "u2"),
            pi("assistant", "continued branch one", "a3", "a1")])
        value, _ = self.run_cli("search", path, "--text", "continued")
        context, _ = self.run_cli("context", "--ref", json.dumps(only(value)["results"][0]["ref"]))
        texts = [e["text"] for e in context["results"]]
        self.assertIn("branch one", texts)
        self.assertNotIn("branch two", texts)

    def test_large_payload_search_and_bounded_expansion(self):
        text = "x" * 100000 + " ERROR_MIDDLE " + "y" * 100000 + " final result"
        path = self.write("large.jsonl", [{"type": "session", "id": "s", "cwd": self.root},
            {"type": "message", "id": "r", "message": {"role": "toolResult", "toolCallId": "missing", "content": [{"type": "text", "text": text}], "isError": True}}])
        value, raw = self.run_cli("search", path, "--text", "ERROR_MIDDLE")
        self.assertLessEqual(len(raw), 8192)
        e = only(value)["results"][0]
        self.assertIn("ERROR_MIDDLE", e["text"])
        self.assertGreater(e["text_omitted"], 100000)
        value, _ = self.run_cli("payload", "--ref", json.dumps(e["ref"]), "--pointer", e["payload_pointer"], "--tail", "--chars", 40)
        self.assertIn("final result", value["results"][0]["text"])

    def test_oversized_and_malformed_records_are_visible(self):
        path = self.write("oversized.jsonl", [{"type": "session", "id": "s", "cwd": self.root}, {"blob": "a" * 5000 + "NEEDLE"}])
        with path.open("a") as f:
            f.write('{"broken":')
        value, _ = self.run_cli("search", path, "--kind", "gap", "--max-record-bytes", 1000)
        self.assertEqual(value["scope"]["oversized_records"], 1)
        self.assertEqual(value["scope"]["malformed_records"], 1)
        ref = only(value)["results"][0]["ref"]
        raw, _ = self.run_cli("raw", "--ref", json.dumps(ref), "--find", "NEEDLE", "--chars", 40)
        self.assertIn("NEEDLE", raw["results"][0]["text"])

    def test_append_allowed_changed_record_rejected(self):
        path = self.fixture()
        value, _ = self.run_cli("search", path, "--role", "user")
        e = only(value)["results"][0]
        with path.open("a") as f:
            f.write(json.dumps(pi("assistant", "later", "a3", "a2")) + "\n")
        self.run_cli("payload", "--ref", json.dumps(e["ref"]), "--pointer", e["payload_pointer"])
        path.write_text(path.read_text().replace("Fix importer", "Fix exporter"))
        value, _ = self.run_cli("payload", "--ref", json.dumps(e["ref"]), code=2)
        self.assertEqual(value["status"], "error")

    def test_limits_valid_json_and_control_escaping(self):
        path = self.write("budget.jsonl", [{"type": "session", "id": "s", "cwd": self.root}] +
            [pi("user", '\x1b[31m } , "scope": {"fake":true} ' + "z" * 1000, f"u{i}") for i in range(20)])
        value, raw = self.run_cli("search", path, "--role", "user", "--limit", 20, "--max-bytes", 2048)
        self.assertLessEqual(len(raw), 2048)
        self.assertNotIn(b"\x1b", raw)
        self.assertEqual(value["matched"], 20)
        self.assertNotIn("fake", value["scope"])
        limited, _ = self.run_cli("overview", path, "--max-scan-bytes", 200)
        self.assertFalse(limited["scope"]["scan_complete"])

    def test_project_selection_and_duplicate_input(self):
        path = self.fixture()
        value, _ = self.run_cli("overview", path, path, "--project", self.root)
        self.assertEqual(value["scope"]["files_scanned"], 1)
        self.assertEqual(value["scope"]["duplicates_skipped"], 1)
        value, _ = self.run_cli("overview", path, "--project", "/elsewhere")
        self.assertEqual(value["scope"]["files_scanned"], 0)

    def test_project_filter_scans_past_the_unfiltered_file_cap(self):
        for i in range(10):
            self.write(f"p{i}.jsonl", [{"type": "session", "id": f"s{i}", "cwd": self.root},
                                       pi("user", f"request {i}", "u")])
        value, _ = self.run_cli("overview", self.directory)
        self.assertEqual(value["scope"]["files_scanned"], 10)
        self.assertTrue(value["scope"]["scan_complete"])
        value, _ = self.run_cli("overview", self.directory, "--project", "")
        self.assertEqual(value["scope"]["files_scanned"], 8)
        self.assertFalse(value["scope"]["scan_complete"])
        # The rerun that completes coverage survives a tight response budget.
        value, _ = self.run_cli("overview", self.directory, "--project", "", "--max-bytes", 1500)
        self.assertTrue(any("--max-files 10" in step["argv"] for step in value.get("next", [])), value)

    def test_turn_context_centres_on_the_selected_event(self):
        records = [{"type": "session", "id": "s", "cwd": self.root}, pi("user", "long turn", "u")]
        parent = "u"
        for i in range(20):
            records.append(pi("assistant", f"step {i}", f"a{i}", parent))
            parent = f"a{i}"
        path = self.write("long.jsonl", records)
        found, _ = self.run_cli("search", path, "--text", "step 15")
        ref = json.dumps(only(found)["results"][0]["ref"])
        value, _ = self.run_cli("context", "--ref", ref, "--limit", 5)
        self.assertTrue(value["selected"]["on_page"])
        self.assertEqual(value["selected"]["position"], 16)
        self.assertEqual(value["page"], {"start": 14, "end": 19})
        self.assertIn("step 15", [e["text"] for e in value["results"]])
        self.assertIn("turn_request_ref", value)
        value, _ = self.run_cli("context", "--ref", ref, "--limit", 5, "--offset", 0)
        self.assertFalse(value["selected"]["on_page"])
        self.assertIn("--offset 14", value["next"][0]["argv"])

    def test_project_defaults_to_invocation_directory(self):
        path = self.fixture()
        value, _ = self.run_cli("overview", path)
        self.assertEqual(value["scope"]["files_scanned"], 1)
        self.assertEqual(value["scope"]["project_filter"], self.root)
        other = self.write("other.jsonl", [{"type": "session", "id": "s2", "cwd": "/elsewhere"},
                                           pi("user", "elsewhere work", "u1")])
        value, _ = self.run_cli("overview", other)
        self.assertEqual(value["scope"]["files_scanned"], 0)
        value, _ = self.run_cli("overview", other, "--project", "")
        self.assertEqual(value["scope"]["files_scanned"], 1)
        self.assertEqual(value["scope"]["project_filter"], "all")

    def test_projects_lists_recorded_roots_from_headers_only(self):
        self.fixture()
        self.write("old.jsonl", [{"type": "session", "id": "s2", "cwd": "/old/checkout"},
                                 pi("user", "old work", "u1")])
        self.write("old2.jsonl", [{"type": "session", "id": "s3", "cwd": "/old/checkout/"}])
        self.write("rootless.jsonl", [pi("user", "no root", "u1")])
        value, _ = self.run_cli("projects", self.directory, "--harness", "pi")
        roots = {r["root"]: r["recordings"] for r in value["results"]}
        self.assertEqual(roots, {self.root: 1, "/old/checkout": 2, None: 1})
        self.assertEqual(value["scope"]["project_unknown"], 1)
        self.assertNotIn("files_scanned", value["scope"])
        self.assertIn("--project", value["next"][0]["argv"])
        value, _ = self.run_cli("projects", self.directory, "--harness", "pi", "--limit", "1")
        # The invocation directory's root stays listed beyond --limit.
        self.assertEqual((len(value["results"]), value["omissions"]["selection"]), (2, 1))
        self.assertEqual([r["root"] for r in value["results"] if r.get("listed_because")], [self.root])

    def test_overview_points_to_projects_when_roots_are_excluded(self):
        self.fixture()
        self.write("old.jsonl", [{"type": "session", "id": "s2", "cwd": "/old/checkout"},
                                 pi("user", "old work", "u1")])
        value, _ = self.run_cli("overview", self.directory, "--max-bytes", "20000")
        self.assertTrue(any(n["argv"].endswith(" projects") for n in value["next"]))
        value, _ = self.run_cli("overview", self.directory, "--project", "", "--max-bytes", "20000")
        self.assertFalse(any(n["argv"].endswith(" projects") for n in value["next"]))

    def test_context_ignores_default_project(self):
        path = self.write("elsewhere.jsonl", [{"type": "session", "id": "s", "cwd": "/elsewhere"},
                                              pi("user", "remote request", "u1")])
        found, _ = self.run_cli("search", path, "--text", "remote", "--project", "")
        context, _ = self.run_cli("context", "--ref", json.dumps(only(found)["results"][0]["ref"]))
        self.assertEqual([e["text"] for e in context["results"]], ["remote request"])

    def test_bare_invocation_uses_local_recordings(self):
        home = self.directory / "home"
        recordings = home / ".pi" / "agent" / "sessions" / "--repo--"
        recordings.mkdir(parents=True)
        (recordings / "pi.jsonl").write_text(json.dumps({"type": "session", "id": "s", "cwd": self.root}) + "\n" +
            json.dumps(pi("user", "default recordings request", "u1")) + "\n")
        value, raw = self.run_cli(env={"HOME": str(home)})
        pi_group = next(g for g in value["harnesses"] if g["harness"] == "pi")
        self.assertEqual(pi_group["requests"][0]["text"], "default recordings request")
        self.assertEqual(value["scope"]["project_filter"], self.root)
        empty = self.directory / "empty-home"
        empty.mkdir()
        value, _ = self.run_cli(env={"HOME": str(empty)}, code=2)
        self.assertIn("sessions", value["results"][0]["error"])
        explicit, _ = self.run_cli("overview", path := recordings / "pi.jsonl", env={"HOME": str(empty)})
        self.assertEqual(only(explicit)["requests"][0]["text"], "default recordings request")

    def home_with_every_harness(self):
        home = self.directory / "home"
        layouts = {
            home / ".pi/agent/sessions/--repo--/pi.jsonl": [
                {"type": "session", "id": "pi-session", "cwd": self.root}, pi("user", "pi request", "u1")],
            home / ".codex/sessions/2026/10/24/rollout-1.jsonl": [
                {"type": "session_meta", "payload": {"id": "codex-session", "cwd": self.root, "cli_version": "1.2"}},
                {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t", "item": {"id": "u", "type": "UserMessage", "content": [{"type": "text", "text": "codex request"}]}}}],
            home / ".claude/projects/-repo/c.jsonl": [
                {"type": "file-history-snapshot", "messageId": "m"},
                claude("user", "claude request", "u", cwd=self.root, version="2.0")],
        }
        for path, records in layouts.items():
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(v) + "\n" for v in records))
        return home

    def test_bare_invocation_groups_every_harness_with_one_schema(self):
        home = self.home_with_every_harness()
        value, raw = self.run_cli(env={"HOME": str(home)})
        self.assertLessEqual(len(raw), 8192)
        groups = {g["harness"]: g for g in value["harnesses"]}
        self.assertEqual(list(groups), ["claude-code", "codex", "pi"])
        self.assertEqual({h: g["requests"][0]["text"] for h, g in groups.items()},
                         {"claude-code": "claude request", "codex": "codex request", "pi": "pi request"})
        self.assertEqual({h: g["requests"][0]["session"] for h, g in groups.items()},
                         {"claude-code": "claude-session", "codex": "codex-session", "pi": "pi-session"})
        # Identical group shape and identical row keys for the same kind of event.
        self.assertEqual(len({tuple(g) for g in groups.values()}), 1)
        self.assertEqual(len({tuple(g["scope"]) for g in groups.values()}), 1)
        core = {"session", "harness", "kind", "role", "turn", "turn_basis", "outcome", "text", "ref"}
        for g in groups.values():
            self.assertLessEqual(core, set(g["requests"][0]))
        self.assertEqual(groups["codex"]["recordings"][0]["harness_version"], "1.2")
        self.assertEqual(value["scope"]["files_scanned"], 3)
        selected, raw = self.run_cli("select", "--role", "user", env={"HOME": str(home)})
        self.assertEqual(selected["matched"], 3)
        rows, _ = self.run_cli("--sql", "SELECT harness, count(*) AS n FROM rows GROUP BY harness ORDER BY harness", script=ROWS, stdin=raw)
        self.assertEqual([r["harness"] for r in rows["results"]], ["claude-code", "codex", "pi"])
        only_codex, _ = self.run_cli("overview", "--harness", "codex", env={"HOME": str(home)})
        self.assertEqual(only(only_codex)["harness"], "codex")

    def test_settings_override_default_locations(self):
        home = self.home_with_every_harness()
        settings = self.directory / "settings.json"
        settings.write_text(json.dumps({"recordings": {"pi": [], "codex": "$TRACE_TEST_CODEX", "claude-code": []}}))
        env = {"HOME": str(home), "TRACE_SESSIONS_SETTINGS": str(settings), "TRACE_TEST_CODEX": str(home / ".codex/sessions")}
        value, _ = self.run_cli(env=env)
        self.assertEqual(only(value)["requests"][0]["text"], "codex request")
        described, _ = self.run_cli("describe", env=env)
        self.assertEqual(described["results"][0]["defaults"]["recordings"]["pi"], [])
        settings.write_text(json.dumps({"recordings": {"nope": ["/x"]}}))
        error, _ = self.run_cli(env=env, code=2)
        self.assertIn("known harness", error["results"][0]["error"])

    def test_duplicate_call_ids_stay_unpaired(self):
        path = self.write("duplicates.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root}, pi("user", "work", "u"),
            pi("assistant", [{"type": "toolCall", "id": "same", "name": "bash", "arguments": {"command": "false"}}], "a", "u"),
            pi("assistant", [{"type": "toolCall", "id": "same", "name": "bash", "arguments": {"command": "true"}}], "b", "u"),
            {"type": "message", "id": "r", "parentId": "b", "message": {"role": "toolResult", "toolCallId": "same", "isError": False, "content": []}}])
        value, _ = self.run_cli("search", path, "--kind", "tool_call")
        self.assertEqual(value["scope"]["ambiguous_call_ids"], 1)
        self.assertTrue(all("result_ref" not in e for e in only(value)["results"]))

    def test_rows_aggregation_coverage_and_readonly(self):
        selected, raw = self.run_cli("select", self.fixture(), "--kind", "tool_call")
        value, _ = self.run_cli("--sql", "SELECT outcome, count(*) AS n FROM rows GROUP BY outcome", script=ROWS, stdin=raw)
        self.assertEqual(value["results"], [{"outcome": "failed", "n": 1}])
        self.assertEqual(value["scope"]["inputs"][0]["upstream"]["scope"]["files_scanned"], 1)
        not_created = self.directory / "must-not-create.db"
        for statement in (f"ATTACH DATABASE '{not_created}' AS bad",
                          f"COPY (SELECT * FROM rows) TO '{not_created}.csv'",
                          f"CREATE TABLE bad AS SELECT * FROM rows",
                          "SELECT 1; SELECT 2"):
            self.run_cli("--sql", statement, script=ROWS, stdin=raw, code=2)
        self.assertFalse(not_created.exists())
        self.assertFalse((self.directory / "must-not-create.db.csv").exists())

    def test_rows_exact_join_and_dynamic_fields(self):
        path = self.fixture()
        calls, _ = self.run_cli("select", path, "--kind", "tool_call", "--fields", "tool,result_ref,ref", "--field", "alias=/message/role")
        results, _ = self.run_cli("select", path, "--kind", "tool_result", "--fields", "text,ref")
        self.assertEqual(only(calls)["results"][0]["alias"], "assistant")
        left, right = self.directory / "calls.json", self.directory / "results.json"
        left.write_text(json.dumps(calls)); right.write_text(json.dumps(results))
        value, _ = self.run_cli("--input", left, "--input", right, "--sql", "SELECT t0.tool,t1.text FROM t0 JOIN t1 ON json_extract(t0.result_ref,'$.offset')=json_extract(t1.ref,'$.offset')", script=ROWS)
        self.assertEqual(len(value["results"]), 1)
        self.assertIn("FAILED", value["results"][0]["text"])

    def test_scan_cut_is_not_malformed_and_gaps_expand(self):
        path = self.fixture()
        value, _ = self.run_cli("overview", path, "--max-scan-bytes", 100)
        self.assertFalse(value["scope"]["scan_complete"])
        self.assertEqual(value["scope"]["malformed_records"], 0)
        unknown = self.write("unknown.jsonl", [{"unexpected": "shape"}])
        value, _ = self.run_cli("search", unknown, "--text", "absent", "--project", "")
        self.assertEqual(value["matched"], 0)
        self.assertEqual(only(value)["harness"], "unknown")
        self.assertEqual(only(value)["leads"][0]["kind"], "unknown")

    def test_select_sql_counts_every_match_and_pipes_flag_partial(self):
        path = self.write("many.jsonl", [{"type": "session", "id": "s", "cwd": self.root}] +
            [pi("user", f"request {i} " + "z" * 500, f"u{i}") for i in range(60)])
        value, _ = self.run_cli("select", path, "--role", "user", "--sql", "SELECT harness, count(*) AS n FROM rows GROUP BY harness")
        self.assertEqual(value["results"], [{"harness": "pi", "n": 60}])
        self.assertEqual(value["matched"], 60)
        self.run_cli("select", path, "--sql", "SELECT 1", "--offset", 1, code=2)
        _, raw = self.run_cli("select", path, "--role", "user")
        piped, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=raw)
        self.assertEqual(piped["status"], "partial")
        self.assertLess(piped["results"][0]["n"], 60)
        self.assertEqual(piped["results"][0]["n"] + piped["omissions"]["upstream_rows"], 60)

    def test_rows_positional_inputs_describe_and_empty_stdin(self):
        saved = self.directory / "rows.json"
        saved.write_text(json.dumps([{"x": 1}, {"x": 2}]))
        value, _ = self.run_cli("SELECT sum(x) AS total FROM rows", saved, script=ROWS)
        self.assertEqual(value["results"], [{"total": 3}])
        self.assertNotIn("status", value)
        value, _ = self.run_cli(saved, script=ROWS)
        self.assertEqual(len(value["results"]), 2)
        described, _ = self.run_cli("--describe", script=ROWS, stdin=b"")
        self.assertIn("usage", described["results"][0])
        error, _ = self.run_cli(script=ROWS, stdin=b"", code=2)
        self.assertIn("Empty input", error["results"][0]["error"])

    def test_rows_views_join_without_saved_files(self):
        path = self.fixture()
        select = [sys.executable, str(SESSIONS), "select", str(path)]
        calls = command_line(select + ["--kind", "tool_call", "--fields", "tool,result_ref,ref"])
        results = command_line(select + ["--kind", "tool_result", "--fields", "text,ref"])
        query = ("SELECT t0.tool, t1.text FROM t0 JOIN t1 "
                 "ON t0.result_ref->>'$.offset' = t1.ref->>'$.offset'")
        value, _ = self.run_cli(query, "--view", "calls=" + calls, "--view", "results=" + results, script=ROWS)
        self.assertEqual(len(value["results"]), 1)
        self.assertEqual(value["results"][0]["tool"], "bash")
        self.assertIn("FAILED", value["results"][0]["text"])
        self.assertEqual([(i["view"], i["alias"]) for i in value["scope"]["inputs"]],
                         [("calls", "t0"), ("results", "t1")])

    @unittest.skipIf(os.name == "nt", "process substitution is POSIX-only")
    def test_rows_accept_process_substitution(self):
        # Process substitution supplies named views too; stdin can be added with '-'.
        command = (f"{shlex.quote(sys.executable)} {shlex.quote(str(ROWS))} "
                   f"'SELECT t0.x, t1.y FROM t0, t1' - <(printf '[{{\"y\":2}}]')")
        result = subprocess.run(["/bin/bash", "-c", command], input=b'[{"x":1}]',
                                capture_output=True, timeout=30, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)["results"], [{"x": 1, "y": 2}])
        self.assertFalse(any(p.suffix in (".json", ".db", ".tmp") for p in self.directory.glob("*rows*")))

    def test_rows_leave_no_artifacts_and_reject_unknown_envelopes(self):
        empty = self.directory / "empty"
        empty.mkdir()
        value, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=b"[]", cwd=empty)
        self.assertEqual(value["results"], [{"n": 0}])
        self.assertEqual(value["scope"]["inputs"][0]["columns"], [])
        self.run_cli("SELECT 1 AS one", script=ROWS, stdin=b'[{"x":1}]', cwd=empty)
        self.assertEqual(os.listdir(empty), [])
        unknown, _ = self.run_cli("--sql", "SELECT 1", script=ROWS, stdin=b'{"scope":{}}', code=2)
        self.assertIn("results[] or rows[]", unknown["results"][0]["error"])
        missing, _ = self.run_cli("SELECT 1 AS one", "--memory-limit", "512Mb", script=ROWS, stdin=b'[{"x":1}]')
        self.assertEqual(missing["results"], [{"one": 1}])

    def test_rows_blob_and_query_limits(self):
        value, _ = self.run_cli("--sql", "SELECT x'6162' AS value", script=ROWS, stdin=b'[{"x":1}]')
        self.assertEqual(value["results"][0]["value"]["value"], "6162")
        timed, _ = self.run_cli("--sql", "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n",
                                "--query-seconds", "0.5", script=ROWS, stdin=b'[{"x":1}]', code=2)
        self.assertIn("Query exceeded", timed["results"][0]["error"])


    def test_edges_exact_pairing_and_turn(self):
        path = self.write("edges.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            pi("user", "request", "u"),
            pi("assistant", [{"type": "toolCall", "id": "c", "name": "bash", "arguments": {"command": "true"}}], "a", "u"),
            {"type": "message", "id": "r", "parentId": "a", "message": {"role": "toolResult", "toolCallId": "c", "content": []}},
            pi("assistant", "answer", "a2", "r")])
        value, _ = self.run_cli("edges", path, "--project", "")
        relations = {row["relation"] for row in value["results"]}
        self.assertTrue({"call_result", "recorded_parent", "turn"} <= relations)
        call = next(row for row in value["results"] if row["relation"] == "call_result")
        self.assertEqual(call["from_ref"]["item"], 0)
        self.assertEqual(call["to_ref"]["item"], 0)
        # Unpaired results never invent a partner edge.
        unpaired = self.write("unpaired.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "message", "id": "r", "message": {"role": "toolResult", "toolCallId": "missing", "content": []}}])
        value, _ = self.run_cli("edges", unpaired, "--project", "")
        self.assertFalse(any(row["relation"] == "call_result" for row in value["results"]))


    def test_typed_empty_and_all_null_bindings(self):
        value, _ = self.run_cli("SELECT count(x) AS n FROM rows", "--type", "x=BIGINT",
                                script=ROWS, stdin=b"[]")
        self.assertEqual(value["results"], [{"n": 0}])
        self.assertEqual(value["scope"]["inputs"][0]["types"]["x"], "BIGINT")
        self.assertIn("x", value["scope"]["inputs"][0]["columns"])
        value, _ = self.run_cli("SELECT typeof(x) AS kind FROM rows", "--type", "x=BIGINT",
                                script=ROWS, stdin=b'[{"x":null}]')
        self.assertEqual(value["results"], [{"kind": "BIGINT"}])
        value, _ = self.run_cli("SELECT typeof(x) AS kind FROM rows", "--type", "x=VARCHAR",
                                script=ROWS, stdin=b'[{"x":null}]')
        self.assertEqual(value["results"], [{"kind": "VARCHAR"}])

    def test_named_params_are_bound_not_spliced(self):
        value, _ = self.run_cli("SELECT $needle AS value", "--param", "needle=x' OR 1=1 --",
                                script=ROWS, stdin=b'[{"x":1}]')
        self.assertEqual(value["results"], [{"value": "x' OR 1=1 --"}])
        self.assertEqual(value["scope"]["param_names"], ["needle"])
        selected, _ = self.run_cli("select", self.fixture(), "--kind", "user_message", "--text", "Fix", "--project", "",
                                   "--param", "needle=Fix", "--sql", "SELECT $needle AS needle, count(*) AS n FROM rows")
        self.assertEqual(selected["results"], [{"needle": "Fix", "n": 1}])

    def test_typed_mixed_widening_and_empty_declared_columns(self):
        value, _ = self.run_cli("SELECT DISTINCT typeof(x) AS kind FROM rows", script=ROWS, stdin=b'[{"x":1},{"x":"a"}]')
        self.assertEqual(value["results"], [{"kind": "VARCHAR"}])
        selected, _ = self.run_cli("select", self.fixture(), "--text", "absent", "--project", "",
                                   "--type", "rows.exit_code=BIGINT",
                                   "--sql", "SELECT count(exit_code) AS n FROM rows")
        self.assertEqual(selected["results"], [{"n": 0}])
        self.assertEqual(selected["scope"]["inputs"][0]["types"]["exit_code"], "BIGINT")

    def test_full_text_hydrates_budget_omitted_sql(self):
        text = "x" * 5000 + " NEEDLE"
        path = self.write("hydrate.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "message", "id": "m", "message": {"role": "user", "content": text}}])
        query = "SELECT text_state, length(text) AS n FROM rows"
        clipped, _ = self.run_cli("select", path, "--text", "NEEDLE", "--project", "",
                                  "--max-text-bytes", 100, "--sql", query)
        self.assertEqual(clipped["results"][0]["text_state"], "budget_omitted")
        self.assertLess(clipped["results"][0]["n"], len(text))
        self.assertEqual(clipped["status"], "partial")
        hydrated, _ = self.run_cli("select", path, "--text", "NEEDLE", "--project", "",
                                   "--max-text-bytes", 100, "--full-text", "--sql", query)
        self.assertEqual(hydrated["results"], [{"text_state": "full", "n": len(text)}])
        # Every matched row was re-read, so the query no longer reports omitted text.
        self.assertNotEqual(hydrated.get("status"), "partial")
        self.assertTrue(hydrated["scope"]["text_complete"])
        self.assertEqual(hydrated["scope"]["text_rehydrated_events"], 1)
        self.assertEqual(hydrated["scope"]["text_scan_truncated_events"], 1)
        self.assertFalse(any("max-text-bytes" in str(item) for item in hydrated["next"]))

    def test_engine_grain_is_echoed(self):
        rows, _ = self.run_cli("SELECT 1 AS one", script=ROWS, stdin=b'[{"x":1}]')
        self.assertEqual(rows["scope"]["grain"], "one input row")
        selected, _ = self.run_cli("select", self.fixture(), "--sql", "SELECT count(*) AS n FROM rows")
        self.assertEqual(selected["scope"]["grain"], "one matched event")


    def test_rows_marks_text_truncated_upstream_partial(self):
        text = "x" * 5000 + " NEEDLE"
        path = self.write("coverage.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "message", "id": "m", "message": {"role": "user", "content": text}}])
        _, raw = self.run_cli("select", path, "--text", "NEEDLE", "--project", "", "--max-text-bytes", 100)
        value, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=raw)
        self.assertEqual(value["status"], "partial")
        self.assertGreater(value["omissions"]["text_truncated_events"], 0)
        self.assertEqual(value["scope"]["inputs"][0]["upstream"]["status"], "partial")


    def test_pi_request_replay_applies_compaction_and_context_edit(self):
        path = self.write("replay-pi.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "message", "id": "u1", "message": {"role": "user", "content": "one"}},
            {"type": "message", "id": "a1", "parentId": "u1", "message": {"role": "assistant", "content": "answer one"}},
            {"type": "compaction", "summary": "SUMMARY", "firstKeptEntryId": "u1"},
            {"type": "context_edit", "targetId": "u1", "replacement": "REPLACED ONE"},
            {"type": "message", "id": "u2", "parentId": "a1", "message": {"role": "user", "content": "two"}}])
        found, _ = self.run_cli("search", path, "--text", "two", "--fields", "ref", "--project", "")
        ref = only(found)["results"][0]["ref"]
        value, _ = self.run_cli("context", "--mode", "request", "--ref", json.dumps(ref))
        self.assertEqual(value["guarantee"], "replayed")
        self.assertEqual([row["block_type"] for row in value["context_items"]],
                         ["summary", "user_message", "assistant_message", "user_message"])
        self.assertEqual(value["context_items"][0]["transformation"], "replayed")
        self.assertEqual(value["context_items"][1]["transformation"], "replaced_by_context_edit")
        self.assertEqual(value["results"][1]["content_hash"] is not None, True)

    def test_codex_request_replay_and_changed_components(self):
        path = self.write("replay-codex.jsonl", [
            {"type": "session_meta", "payload": {"id": "s", "cwd": self.root}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "u1", "type": "UserMessage", "content": [{"type": "text", "text": "one"}]}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "a1", "type": "AgentMessage", "parent_id": "u1", "content": [{"type": "Text", "text": "answer"}]}}},
            {"type": "compacted", "summary": "CODEX SUMMARY", "firstKeptEntryId": "u1"},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "u2", "type": "UserMessage", "parent_id": "a1", "content": [{"type": "text", "text": "two"}]}}}])
        found, _ = self.run_cli("search", path, "--text", "two", "--fields", "ref", "--project", "")
        ref = only(found)["results"][0]["ref"]
        value, _ = self.run_cli("context", "--mode", "request", "--ref", json.dumps(ref))
        self.assertIn(value["guarantee"], {"replayed", "transcript_only"})
        self.assertEqual(value["context_items"][0]["block_type"], "summary")
        other, _ = self.run_cli("search", path, "--text", "one", "--fields", "ref", "--project", "")
        ref2 = only(other)["results"][0]["ref"]
        compared, _ = self.run_cli("context", "--mode", "request", "--ref", json.dumps(ref),
                                   "--compare-ref", json.dumps(ref2))
        self.assertTrue(compared["changed"])

    def test_claude_request_replay_is_transcript_only(self):
        path = self.write("replay-claude.jsonl", [
            {"type": "summary", "summary": "CLAUDE SUMMARY"},
            {"type": "user", "uuid": "u", "sessionId": "s", "cwd": self.root, "message": {"content": "one"}},
            {"type": "assistant", "uuid": "a", "parentUuid": "u", "message": {"content": [{"type": "text", "text": "answer"}]}},
            {"type": "user", "uuid": "u2", "parentUuid": "a", "message": {"content": "two"}}])
        found, _ = self.run_cli("search", path, "--text", "two", "--fields", "ref")
        ref = only(found)["results"][0]["ref"]
        value, _ = self.run_cli("context", "--mode", "request", "--ref", json.dumps(ref))
        self.assertEqual(value["guarantee"], "transcript_only")
        self.assertEqual(value["context_items"][-1]["block_type"], "user_message")


    def test_records_reports_parse_status_and_snapshot(self):
        path = self.write("broken.jsonl", [{"type": "session", "id": "s", "cwd": self.root}])
        with path.open("a") as handle:
            handle.write("{broken")
        value, _ = self.run_cli("records", path, "--project", "", "--parse-status", "malformed_record")
        self.assertEqual(value["matched"], 1)
        self.assertEqual(value["results"][0]["parse_status"], "malformed_record")
        self.assertEqual(value["scope"]["snapshot"][0]["file"], str(path.resolve()))
        self.assertIn("snapshot_basis", value["scope"])
        two = self.write("two.jsonl", [{"type": "session", "id": "t", "cwd": self.root}])
        value, _ = self.run_cli("overview", two, path, "--project", "")
        # Per-file bytes, not the group's running total.
        self.assertEqual([(s["bytes_scanned"], s["scan_complete"]) for s in value["scope"]["snapshot"]],
                         [(s["size"], True) for s in value["scope"]["snapshot"]])
        self.assertNotIn("snapshot", only(value)["scope"])

    def test_budget_levels_are_deterministic_and_always_fit(self):
        records = [{"type": "session", "id": "s", "cwd": self.root}]
        for i in range(30):
            records.append(pi("user", f"request {i} " + "w" * 500, f"u{i}", f"r{i - 1}" if i else None))
            records.append(pi("assistant", [{"type": "toolCall", "id": f"c{i}", "name": "bash",
                                             "arguments": {"command": f"run {i} " + "x" * 400}}], f"a{i}", f"u{i}"))
            records.append({"type": "message", "id": f"r{i}", "parentId": f"a{i}", "message": {
                "role": "toolResult", "toolCallId": f"c{i}", "isError": True, "content": [{"type": "text", "text": "no"}]}})
        path = self.write("big.jsonl", records)
        full, raw = self.run_cli("overview", path, "--max-bytes", 10 ** 7)
        self.assertNotIn("detail", full)
        self.assertGreater(len(raw), 8192)
        levels = []
        for budget in (1024, 2048, 4096, 8192):
            value, raw = self.run_cli("overview", path, "--max-bytes", budget)
            self.assertLessEqual(len(raw), budget)
            self.assertEqual(raw, self.run_cli("overview", path, "--max-bytes", budget)[1])
            self.assertIn("files_scanned", value["scope"])
            detail = value["detail"]
            levels.append(detail["level"])
            # more_bytes is the exact budget for a richer level.
            richer, raw = self.run_cli("overview", path, "--max-bytes", detail["more_bytes"])
            self.assertLessEqual(len(raw), detail["more_bytes"])
            self.assertNotEqual(richer.get("detail", {}).get("level"), detail["level"])
        order = [name for name, _ in LEVELS]
        ranks = [order.index(level) for level in levels]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        # Requests outlive leads, files and commands.
        value, _ = self.run_cli("overview", path, "--max-bytes", 4096)
        self.assertTrue(only(value)["requests"])
        self.assertFalse(only(value)["leads"])

    def test_coverage_floor_fits_minimum_budget(self):
        home = self.home_with_every_harness()
        value, raw = self.run_cli("overview", "--project", "/" + "p" * 3000, "--max-bytes", 1024,
                                  env={"HOME": str(home)})
        self.assertLessEqual(len(raw), 1024)
        self.assertEqual(value["detail"]["level"], "coverage")
        self.assertEqual([g["harness"] for g in value["harnesses"]], ["claude-code", "codex", "pi"])


    def test_preserves_summary_instruction_reasoning_config_lifecycle(self):
        path = self.write("evidence.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "compaction", "summary": "SUMMARY TEXT"},
            {"type": "message", "id": "u", "message": {"role": "user", "content": "human ask"}},
            {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "THINK TEXT"},
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "answer"}]}},
            {"type": "message", "id": "s2", "message": {"role": "system", "content": "SYSTEM INSTRUCTION"}},
            {"type": "custom", "customType": "cfg", "data": {"x": 1}},
            {"type": "context_edit", "targetId": "u", "replacement": "REPLACED"},
            {"type": "custom_message", "content": "INJECTED SUMMARY"}])
        value, _ = self.run_cli("select", path, "--project", "", "--fields", "kind,origin,state,text_state,text", "--limit", 50)
        rows = only(value)["results"]
        self.assertEqual({r["kind"] for r in rows}, {"config", "summary", "user_message", "reasoning", "assistant_message", "instruction", "context_change"})
        self.assertIn("SUMMARY TEXT", {r.get("text") for r in rows})
        self.assertIn("SYSTEM INSTRUCTION", {r.get("text") for r in rows})
        self.assertIn("THINK TEXT", {r.get("text") for r in rows})
        redacted = [r for r in rows if r["state"] == "redacted"]
        self.assertEqual(len(redacted), 1)
        self.assertEqual(redacted[0]["text_state"], "redacted")
        self.assertEqual([r for r in rows if r.get("origin") == "human"][0]["kind"], "user_message")
        self.assertIn("INJECTED SUMMARY", {r.get("text") for r in rows})

    def test_codex_preserves_instructions_reasoning_lifecycle_capability(self):
        path = self.write("codex-evidence.jsonl", [
            {"type": "session_meta", "payload": {"id": "s", "cwd": self.root, "base_instructions": {"text": "BASE INSTRUCTION"}}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "DEV INSTRUCTION"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "REASON SUMMARY"}], "encrypted_content": "opaque"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total": 3}}},
            {"type": "response_item", "payload": {"type": "additional_tools", "tools": [{"name": "x"}]}}])
        value, _ = self.run_cli("select", path, "--project", "", "--fields", "kind,origin,state,text_state,text", "--limit", 50)
        rows = only(value)["results"]
        kinds = {r["kind"] for r in rows}
        self.assertTrue({"instruction", "reasoning", "lifecycle", "usage", "capability", "config"} <= kinds)
        self.assertIn("BASE INSTRUCTION", {r.get("text") for r in rows})
        self.assertIn("DEV INSTRUCTION", {r.get("text") for r in rows})
        self.assertIn("REASON SUMMARY", {r.get("text") for r in rows})
        self.assertTrue(any(r["kind"] == "reasoning" and r["state"] == "encrypted" for r in rows))

    def test_claude_preserves_summary_meta_reasoning_attachment_unsupported(self):
        path = self.write("claude-evidence.jsonl", [
            {"type": "summary", "summary": "CLAUDE SUMMARY"},
            {"type": "user", "uuid": "u", "sessionId": "s", "cwd": self.root, "message": {"content": "human"}},
            {"type": "assistant", "uuid": "a", "parentUuid": "u", "message": {"content": [
                {"type": "thinking", "thinking": "THINK"}, {"type": "redacted_thinking", "data": "x"},
                {"type": "text", "text": "answer"}]}},
            {"type": "user", "uuid": "m", "isMeta": True, "message": {"content": "META INSTRUCTION"}},
            {"type": "attachment", "content": "ATTACHMENT TEXT"},
            {"type": "api-request", "request": {"messages": []}}])
        value, _ = self.run_cli("select", path, "--project", "", "--fields", "kind,origin,state,text_state,text", "--limit", 50)
        rows = only(value)["results"]
        kinds = {r["kind"] for r in rows}
        self.assertTrue({"summary", "instruction", "reasoning", "attachment", "config"} <= kinds)
        self.assertIn("CLAUDE SUMMARY", {r.get("text") for r in rows})
        self.assertIn("META INSTRUCTION", {r.get("text") for r in rows})
        self.assertIn("ATTACHMENT TEXT", {r.get("text") for r in rows})
        unsupported = [r for r in rows if r["kind"] == "config" and r["state"] == "unsupported"]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["text_state"], "unsupported")

    def test_representation_merges_only_proven_echoes(self):
        path = self.write("representations.jsonl", [
            {"type": "session_meta", "payload": {"id": "s", "cwd": self.root}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "u1", "type": "UserMessage", "content": [{"type": "text", "text": "same text"}]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "item": {"id": "u1"}, "content": [{"type": "input_text", "text": "same text"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "distinct event"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "u2", "type": "UserMessage", "content": [{"type": "text", "text": "distinct actual"}]}}}])
        value, _ = self.run_cli("overview", path, "--project", "")
        group = only(value)
        self.assertEqual(group["scope"]["duplicates_skipped"], 1)
        self.assertEqual(group["scope"]["representation_variants"], 0)
        self.assertEqual({e["text"] for e in group["requests"]}, {"same text", "distinct event", "distinct actual"})
        # Same ID with different text stays as two variants instead of dropping either.
        variants = self.write("variants.jsonl", [
            {"type": "session_meta", "payload": {"id": "s2", "cwd": self.root}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "u3", "type": "UserMessage", "content": [{"type": "text", "text": "first"}]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "item": {"id": "u3"}, "content": [{"type": "input_text", "text": "second"}]}}])
        value, _ = self.run_cli("overview", variants, "--project", "")
        self.assertEqual(only(value)["scope"]["representation_variants"], 1)
        self.assertEqual({e["text"] for e in only(value)["requests"]}, {"first", "second"})

    def test_overview_requests_exclude_injected_and_summaries(self):
        path = self.write("requests.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "compaction", "summary": "summary is not a request"},
            {"type": "custom_message", "content": "injected is not human"},
            {"type": "message", "id": "u", "message": {"role": "user", "content": "real request"}}])
        value, _ = self.run_cli("overview", path, "--project", "")
        self.assertEqual(only(value)["activity"]["request_count"], 1)
        self.assertEqual(only(value)["requests"][0]["text"], "real request")

    def test_describe_exposes_evidence_axes(self):
        value, _ = self.run_cli("describe")
        body = value["results"][0]
        self.assertIn("summary", body["event_kinds"])
        self.assertIn("redacted", body["text_states"])
        self.assertIn("records", body["actions"])
        self.assertIn("records", body["relations"])
        self.assertEqual(body["profiles"]["pi"]["schema_version"], "2")


class PhaseOneTests(HistoryTests):
    def test_needle_beyond_preview_in_search_and_sql(self):
        text = "a" * 5000 + " NEEDLE " + "b" * 5000
        path = self.write("long.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "message", "id": "m", "message": {"role": "user", "content": text}}])
        value, _ = self.run_cli("search", path, "--text", "NEEDLE", "--project", "")
        row = only(value)["results"][0]
        self.assertIn("NEEDLE", row["text"])
        self.assertGreater(row["text_omitted"], 9000)
        self.assertEqual(row["text_chars"], len(text))
        sql, _ = self.run_cli("select", path, "--text", "NEEDLE", "--project", "",
                              "--sql", "SELECT text_chars, text_state, contains(text,'NEEDLE') AS hit FROM rows")
        self.assertEqual(sql["results"], [{"text_chars": len(text), "text_state": "full", "hit": True}])

    def test_text_budget_marks_partial_and_sql_omission(self):
        text = "x" * 5000 + " NEEDLE"
        path = self.write("budget-text.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "message", "id": "m", "message": {"role": "user", "content": text}}])
        value, _ = self.run_cli("select", path, "--text", "NEEDLE", "--project", "",
                                "--max-text-bytes", 100, "--sql", "SELECT 1 AS one")
        self.assertEqual(value["status"], "partial")
        self.assertGreater(value["omissions"]["text_truncated_events"], 0)
        search, _ = self.run_cli("search", path, "--text", "NEEDLE", "--project", "", "--max-text-bytes", 100)
        self.assertEqual(only(search)["results"][0]["text_state"], "budget_omitted")

    def test_records_finds_profile_ignored_record_and_payload_expands(self):
        path = self.write("ignored.jsonl", [
            {"type": "session", "id": "s", "cwd": self.root},
            {"type": "custom", "customType": "config", "data": {"value": "IGNORED-CONFIG-VALUE"}}])
        value, _ = self.run_cli("records", path, "--text", "IGNORED-CONFIG", "--project", "")
        self.assertEqual(value["matched"], 1)
        row = value["results"][0]
        self.assertEqual(row["native_type"], "custom")
        payload, _ = self.run_cli("payload", "--ref", json.dumps(row["ref"]), "--pointer", row["payload_pointer"])
        self.assertIn("IGNORED-CONFIG-VALUE", payload["results"][0]["text"])

    def test_rows_accepts_links_alias_and_rejects_newer_schema(self):
        links = json.dumps({"links": [{"x": 1}, {"x": 2}]}).encode()
        value, _ = self.run_cli("SELECT sum(x) AS n FROM rows", script=ROWS, stdin=links)
        self.assertEqual(value["results"], [{"n": 3}])
        newer = json.dumps({"schema_version": 99, "results": [{"x": 1}]}).encode()
        error, _ = self.run_cli("SELECT 1 AS n", script=ROWS, stdin=newer, code=2)
        self.assertIn("newer than supported", error["results"][0]["error"])

    def test_silent_producer_is_killed_at_deadline(self):
        command = command_line([sys.executable, "-c", "import time; time.sleep(30)"])
        started = __import__("time").monotonic()
        value, _ = self.run_cli("SELECT 1", "--view", "slow=" + command,
                                "--producer-seconds", "0.5", script=ROWS, code=2)
        elapsed = __import__("time").monotonic() - started
        self.assertLess(elapsed, 10)
        self.assertIn("producer-seconds", value["results"][0]["error"])

    def test_search_accepts_fields_projection(self):
        path = self.fixture()
        value, _ = self.run_cli("search", path, "--kind", "user_message", "--fields", "text")
        self.assertEqual(set(only(value)["results"][0]), {"text", "harness", "ref"})

    def emitted(self, result, budget):
        from helper.envelope import emit
        stream = io.BytesIO()
        with mock.patch.object(sys, "stdout", mock.Mock(buffer=stream)):
            emit(result, budget)
        return json.loads(stream.getvalue()), stream.getvalue()

    def wide(self, **extra):
        """A known result next to scope too large to survive the minimum budget."""
        return {"results": [{"n": 1}], "scope": {"files_scanned": 1, "inputs": ["v" * 400] * 12}, **extra}

    def test_omitted_rows_are_partial_and_rejected_downstream(self):
        value, raw = self.emitted(self.wide(), 1024)
        self.assertLessEqual(len(raw), 1024)
        self.assertNotIn("results", value)
        self.assertTrue(value["omissions"]["envelope_omitted"])
        self.assertEqual(value["status"], "partial")
        richer, _ = self.emitted(self.wide(), value["detail"]["more_bytes"])
        self.assertEqual(richer["results"], [{"n": 1}])
        error, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=raw, code=2)
        message = error["results"][0]["error"]
        self.assertIn("omitted its rows", message)
        self.assertIn(str(value["detail"]["more_bytes"]), message)
        grouped = {"harnesses": [{"harness": "pi", "results": [{"n": 1}]}], **self.wide()}
        _, raw = self.emitted(grouped, 1024)
        self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=raw, code=2)

    def test_floor_keeps_error_status_and_empty_complete_stays_complete(self):
        value, _ = self.emitted(self.wide(status="error"), 1024)
        self.assertEqual(value["status"], "error")
        value, _ = self.emitted({"results": [], "scope": {"files_scanned": 1}}, 1024)
        self.assertEqual(value["results"], [])
        self.assertNotIn("status", value)
        empty, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=b"[]")
        self.assertNotIn("status", empty)

    def test_budget_dropped_rows_mark_partial(self):
        rows = [{"text": "é" * 300, "i": i} for i in range(40)]
        value, raw = self.emitted({"results": rows}, 4096)
        self.assertLessEqual(len(raw), 4096)
        self.assertEqual(value["detail"]["level"], "rows")
        self.assertEqual(value["status"], "partial")
        self.assertEqual(len(value["results"]) + value["omissions"]["results_budget"], 40)

    def test_capped_scan_marks_direct_and_piped_sql_partial(self):
        path = self.fixture()
        direct, _ = self.run_cli("select", path, "--max-scan-bytes", 100, "--sql", "SELECT count(*) AS n FROM rows")
        self.assertEqual(direct["status"], "partial")
        self.assertEqual(direct["omissions"]["incomplete_inputs"], ["events"])
        self.assertIn("scan_limit", direct["omissions"]["incomplete_reasons"]["events"])
        _, raw = self.run_cli("select", path, "--max-scan-bytes", 100)
        piped, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=raw)
        self.assertEqual(piped["status"], "partial")
        self.assertIn("scan_limit", piped["omissions"]["incomplete_reasons"]["t0"])
        full, _ = self.run_cli("select", path, "--sql", "SELECT count(*) AS n FROM rows")
        self.assertNotIn("status", full)

    def test_sql_limit_governs_output_and_caps_report_the_total(self):
        rows = json.dumps([{"i": i} for i in range(45)]).encode()
        value, _ = self.run_cli("--sql", "SELECT i FROM rows ORDER BY i LIMIT 40", "--max-bytes", 100000,
                                script=ROWS, stdin=rows)
        self.assertEqual(len(value["results"]), 40)
        self.assertNotIn("status", value)
        capped, _ = self.run_cli("--sql", "SELECT i FROM rows", "--limit", 10, script=ROWS, stdin=rows)
        self.assertEqual(capped["status"], "partial")
        self.assertEqual(capped["omissions"]["query_rows_total"], 45)
        self.assertEqual(capped["omissions"]["query_rows_returned"], 10)
        self.assertIn("--limit 45", capped["next"][0])
        path = self.fixture()
        direct, _ = self.run_cli("select", path, "--sql", "SELECT kind FROM rows LIMIT 40")
        self.assertFalse(direct["omissions"]["more_query_rows"])
        paged, _ = self.run_cli("select", path, "--kind", "user_message")
        self.assertEqual(len(only(paged)["results"]), 2)

    def test_query_limited_producer_and_unknown_coverage(self):
        path = self.fixture()
        _, raw = self.run_cli("select", path, "--sql", "SELECT kind FROM rows", "--limit", 1)
        value, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=raw)
        self.assertEqual(value["status"], "partial")
        self.assertIn("query_limit", value["omissions"]["incomplete_reasons"]["t0"])
        # A bare array reports no coverage: unknown, so nothing is claimed either way.
        value, _ = self.run_cli("SELECT count(*) AS n FROM rows", script=ROWS, stdin=b'[{"x":1}]')
        self.assertNotIn("status", value)
        self.assertNotIn("complete", value["scope"]["inputs"][0]["upstream"])


class CoverageRecoveryTests(unittest.TestCase):
    setUp, tearDown, write, run_cli, fixture = (HistoryTests.setUp, HistoryTests.tearDown, HistoryTests.write,
                                                 HistoryTests.run_cli, HistoryTests.fixture)

    def test_unrecognized_file_is_not_a_rootless_recording(self):
        self.fixture()
        self.write("ledger.jsonl", [{"timestamp": 1, "event": "x", "id": "l", "request": {}, "tool": "t"}])
        value, _ = self.run_cli("overview", self.directory, "--harness", "auto", "--max-bytes", 20000)
        self.assertTrue(value["scope"]["scan_complete"])
        self.assertEqual(value["scope"]["files_unrecognized"], 1)
        self.assertEqual(value["scope"]["project_unknown"], 0)
        selected, _ = self.run_cli("select", self.directory, "--sql", "SELECT count(*) AS n FROM rows")
        self.assertNotIn("status", selected)

    def test_rootless_recording_has_its_own_reason_and_inspect_command(self):
        self.fixture()
        rootless = self.write("rootless.jsonl", [pi("user", "no root", "u1")])
        selected, _ = self.run_cli("select", self.directory, "--sql", "SELECT count(*) AS n FROM rows")
        self.assertEqual(selected["omissions"]["incomplete_reasons"]["events"], ["project_unknown"])
        self.assertTrue(selected["scope"]["scan_complete"])
        inspect = [n for n in selected["next"] if isinstance(n, dict) and n["purpose"].startswith("Inspect")]
        self.assertEqual(split_line(inspect[0]["argv"])[-3:], ["--project", "", str(rootless.absolute())])

    def test_next_reruns_with_the_limit_that_reads_everything(self):
        self.fixture()
        self.write("second.jsonl", [{"type": "session", "id": "s2", "cwd": self.root},
                                    pi("user", "y" * 4000, "u1")])
        value, _ = self.run_cli("overview", self.directory, "--max-candidates", 1, "--max-files", 1,
                                "--max-bytes", 20000)
        argv = {n["purpose"]: split_line(n["argv"]) for n in value["next"]}
        self.assertEqual(argv["Read every discovery candidate"][-2:], ["--max-candidates", "2"])
        self.assertEqual(argv["Read every discovery candidate"].count("--max-candidates"), 1)
        value, _ = self.run_cli("overview", self.directory, "--max-files", 1, "--max-bytes", 20000)
        argv = {n["purpose"]: split_line(n["argv"]) for n in value["next"]}
        self.assertEqual(argv["Scan every selected recording"][-2:], ["--max-files", "2"])
        value, _ = self.run_cli("overview", self.directory, "--max-text-bytes", 100, "--max-bytes", 20000)
        self.assertFalse(value["scope"]["text_complete"])
        rerun = {n["purpose"]: split_line(n["argv"]) for n in value["next"]}["Store full text for every scanned event"]
        value, _ = self.run_cli(*rerun[2:])
        self.assertTrue(value["scope"]["text_complete"])

    def test_subagents_and_forks_count_with_their_parent(self):
        main = self.write("main.jsonl", [claude("user", "parent request", "u1", cwd=self.root)])
        self.write("agent.jsonl", [claude("user", "subagent prompt", "s1", cwd=self.root, isSidechain=True)])
        value, _ = self.run_cli("overview", self.directory, "--harness", "claude-code", "--max-bytes", 20000)
        self.assertEqual((value["scope"]["files_scanned"], value["scope"]["sessions_independent"]), (2, 1))
        self.assertEqual(value["scope"]["recordings_with_parent"], 1)
        rows, _ = self.run_cli("select", main, self.directory / "agent.jsonl", "--harness", "claude-code",
                               "--sql", "SELECT text, parent_session FROM rows WHERE kind = 'user_message' ORDER BY text")
        self.assertEqual([r["parent_session"] for r in rows["results"]], [None, "claude-session"])
        parent = self.write("pi-parent.jsonl", [{"type": "session", "id": "p", "cwd": self.root},
                                                pi("user", "start", "u1")])
        self.write("pi-child.jsonl", [{"type": "session", "id": "c", "cwd": self.root,
                                       "parentSession": str(parent.absolute())}, pi("user", "workflow step", "u1")])
        value, _ = self.run_cli("overview", parent, self.directory / "pi-child.jsonl", "--max-bytes", 20000)
        self.assertEqual((value["scope"]["files_scanned"], value["scope"]["sessions_independent"]), (2, 1))
        # Pi names the parent by file path, so coalesce(parent_session, session) would count two.
        rows, _ = self.run_cli("select", parent, self.directory / "pi-child.jsonl", "--sql",
                               "SELECT count(DISTINCT independent_session) AS lineages, "
                               "count(DISTINCT coalesce(parent_session, session)) AS naive FROM rows")
        self.assertEqual(rows["results"], [{"lineages": 1, "naive": 2}])
        shown, _ = self.run_cli("search", parent, self.directory / "pi-child.jsonl", "--kind", "user_message",
                                "--fields", "session,independent_session,text")
        self.assertEqual({r["text"]: r["independent_session"] for r in only(shown)["results"]},
                         {"start": "p", "workflow step": "p"})

    def failing_session(self, name, sid, output, parent=None, command="pnpm setup"):
        meta = {"type": "session", "id": sid, "cwd": self.root, **({"parentSession": str(parent)} if parent else {})}
        return self.write(name, [
            meta, pi("user", "run setup", "u1"),
            pi("assistant", [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": command}}], "a1", "u1"),
            {"type": "message", "id": "r1", "parentId": "a1", "message": {
                "role": "toolResult", "toolCallId": "c1", "isError": True, "details": {"exitCode": 1},
                "content": [{"type": "text", "text": output}]}}])

    def test_recurrence_counts_failed_results_by_independent_session(self):
        first = self.failing_session("a.jsonl", "a", "Exit code 1\nWarning: CREDENTIAL_MISSING for /home/x/key 42")
        self.failing_session("a-child.jsonl", "a-child", "Exit code 1\nWarning: CREDENTIAL_MISSING for /tmp/y/key 7",
                             parent=first.absolute())
        self.failing_session("b.jsonl", "b", "Exit code 2\nWarning: CREDENTIAL_MISSING for /srv/key 9",
                             command="enactry setup")
        self.failing_session("c.jsonl", "c", "Exit code 1\nsomething unique broke", command="cargo test")
        # Mentions outside a failed result are not failures: a passing grep and the operator's own words.
        self.write("d.jsonl", [
            {"type": "session", "id": "d", "cwd": self.root}, pi("user", "I keep seeing CREDENTIAL_MISSING", "u1"),
            pi("assistant", [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "rg CREDENTIAL_MISSING"}}], "a1", "u1"),
            {"type": "message", "id": "r1", "parentId": "a1", "message": {
                "role": "toolResult", "toolCallId": "c1", "content": [{"type": "text", "text": "src/setup.ts: CREDENTIAL_MISSING"}]}}])
        value, _ = self.run_cli("recurrence", self.directory, "--text", "credential_missing", "--text", "MODULE_TIMEOUT")
        self.assertEqual(value["grouped_by"], "term")
        term, absent = value["results"]
        self.assertEqual((term["key"], term["failures"], term["recordings"], term["sessions_independent"]),
                         ("credential_missing", 3, 3, 2))
        self.assertEqual((absent["key"], absent["failures"], absent["sessions_independent"], absent["examples"]),
                         ("MODULE_TIMEOUT", 0, 0, []))
        self.assertEqual({e["independent_session"]: e["failures"] for e in term["examples"]}, {"a": 2, "b": 1})
        self.assertIn("CREDENTIAL_MISSING", term["examples"][0]["text"])
        self.assertTrue(value["scope"]["text_complete"])
        # Every example ref expands to its exact exchange, which carries the cite token.
        exchange, _ = self.run_cli("context", "--ref", json.dumps(term["examples"][0]["ref"]), "--mode", "exchange")
        self.assertEqual([e["kind"] for e in exchange["results"]], ["tool_call", "tool_result"])
        self.assertTrue(all(e["cite"] for e in exchange["results"]))
        # Derived groups strip the exit banner and normalize paths and numbers; singletons fall below --min-sessions.
        errors, _ = self.run_cli("recurrence", self.directory)
        self.assertEqual([(r["key"], r["sessions_independent"]) for r in errors["results"]],
                         [("Warning: CREDENTIAL_MISSING for <path> N", 2)])
        self.assertEqual(errors["omissions"]["below_min_sessions"], 1)
        # With no group at the threshold, the envelope says why it is empty and how to see singletons.
        none, _ = self.run_cli("recurrence", self.directory, "--min-sessions", 5)
        self.assertEqual(none["results"], [])
        self.assertIn("fewer than 5", none["notes"][0])
        self.assertEqual(split_line(none["next"][-1]["argv"])[-2:], ["--min-sessions", "1"])
        commands, _ = self.run_cli("recurrence", self.directory, "--by", "command", "--min-sessions", 1, "--examples", 1)
        self.assertEqual({r["key"]: r["sessions_independent"] for r in commands["results"]},
                         {"pnpm setup": 1, "enactry setup": 1, "cargo test": 1})
        self.assertEqual(commands["results"][0]["examples"][0]["failures"], 2)
        capped, _ = self.run_cli("recurrence", self.directory, "--text", "CREDENTIAL_MISSING", "--examples", 1)
        self.assertEqual(capped["results"][0]["examples_omitted"], 1)
        rerun = split_line(capped["next"][-1]["argv"])
        self.assertEqual(rerun[-2:], ["--examples", "2"])

    def test_codex_exec_wrapper_is_a_script_operation(self):
        path = self.write("codex.jsonl", [
            {"type": "session_meta", "payload": {"id": "s", "cwd": self.root}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c", "name": "exec",
                                                  "input": "await tools.exec_command({cmd: 'ls'})"}}])
        value, _ = self.run_cli("select", path, "--sql", "SELECT operation FROM rows WHERE kind = 'tool_call'")
        self.assertEqual([r["operation"] for r in value["results"]], ["script"])

    def test_missing_include_is_named_in_the_error(self):
        path = self.fixture()
        value, _ = self.run_cli("select", path, "--sql", "SELECT repo_path FROM rows", code=2)
        self.assertIn("--include files", value["results"][0]["error"])
        value, _ = self.run_cli("select", path, "--include", "files", "--sql", "SELECT * FROM commits", code=2)
        self.assertIn("--include files,git", value["results"][0]["error"])
        value, _ = self.run_cli("select", path, "--sql", "SELECT nope FROM rows", code=2)
        self.assertNotIn("--include", value["results"][0]["error"])


    def settings_for(self, directory):
        settings = self.directory / "settings.json"
        settings.write_text(json.dumps({"recordings": {"pi": [str(directory)], "codex": [], "claude-code": []}}))
        return {"TRACE_SESSIONS_SETTINGS": str(settings)}

    def test_expansions_carry_cite_tokens_that_resolve(self):
        path = self.fixture()
        env = self.settings_for(self.directory)
        search, _ = self.run_cli("search", path, "--kind", "tool_call")
        call = only(search)["results"][0]
        # Search rows are locators: no token until the evidence is expanded.
        self.assertNotIn("cite", call)
        exchange, _ = self.run_cli("context", "--ref", json.dumps(call["ref"]), "--mode", "exchange")
        token = exchange["cite"]
        self.assertRegex(token, r"^pi@3(\.\d+)?#[0-9a-f]{12}$")
        self.assertEqual([r["cite"] for r in exchange["results"]][0], token)
        again, _ = self.run_cli("context", "--ref", token, "--mode", "exchange", env=env)
        self.assertEqual([e["kind"] for e in again["results"]], ["tool_call", "tool_result"])
        payload, _ = self.run_cli("payload", "--ref", token, "--chars", 40, env=env)
        self.assertEqual(payload["results"][0]["cite"], token)
        edges, _ = self.run_cli("edges", path)
        self.assertTrue(all(r["from_cite"] and r["to_cite"] for r in edges["results"]))
        for bare in ("s1", "pi", "0123abcd"):
            error, _ = self.run_cli("context", "--ref", bare, code=2, env=env)
            self.assertIn("not a ref", error["results"][0]["error"])
        stale, _ = self.run_cli("context", "--ref", token[:-12] + "0" * 12, code=2, env=env)
        self.assertIn("Source changed", stale["results"][0]["error"])

    def test_cite_checks_every_citation_in_a_report(self):
        path = self.fixture()
        env = self.settings_for(self.directory)
        search, _ = self.run_cli("search", path, "--human", "--limit", 1)
        turn, _ = self.run_cli("context", "--ref", json.dumps(only(search)["results"][0]["ref"]))
        report = self.directory / "report.md"
        report.write_text(f"Asked for it ({turn['cite']}).\n"
                          f"Session 0a1b2c3d said so; digest {'ab12' * 16} is data.\n")
        value, _ = self.run_cli("cite", report, code=1, env=env)
        self.assertEqual(value["check"], "fail")
        self.assertEqual({(r["kind"], r["state"]) for r in value["results"]},
                         {("cite", "ok"), ("bare_hex", "ambiguous")})
        self.assertEqual(value["results"][0]["citation"], "0a1b2c3d")
        report.write_text(f"Asked for it ({turn['cite']}).\n")
        value, _ = self.run_cli("cite", report, env=env)
        self.assertEqual((value["check"], value["scope"]["citations"]), ("pass", 1))

    def test_search_lists_recordings_only_on_request(self):
        path = self.fixture()
        value, _ = self.run_cli("search", path, "--text", "importer")
        self.assertNotIn("recordings", only(value))
        value, _ = self.run_cli("search", path, "--text", "importer", "--recordings")
        self.assertEqual(len(only(value)["recordings"]), 1)
        overview, _ = self.run_cli("overview", path)
        self.assertIn("recordings", only(overview))


if __name__ == "__main__":
    unittest.main()
