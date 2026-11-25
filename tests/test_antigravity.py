"""Antigravity recordings normalize into the same event model as pi, codex, claude-code and dsh."""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from helper import antigravity, cite, commit_links, edit_hunks, file_observations, message_segments as ms  # noqa: E402
from helper import operation_facts  # noqa: E402
from helper.discovery import candidate_files  # noqa: E402
from helper.git_resolve import GitResolver  # noqa: E402
from helper.options import DiscoveryOptions, ScanOptions  # noqa: E402
from helper.profiles import detected, read_view  # noqa: E402
from helper.reader import scan_file  # noqa: E402
from helper.records import RecordCache, new_scope, read_ref  # noqa: E402
from support import invoke  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/recordings/antigravity-slice1.jsonl"
CONV_ID = "792001c1-dcbd-4c20-a99c-0dd3ea17d060"
PARENT_ID = "11111111-2222-3333-4444-555555555555"
ENV_DROP = ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
            "DSH_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID")


def make_archive(base, conv_id=CONV_ID, workspace_uris=("file:///work/project",), parent_id="",
                 fixture=FIXTURE, rows=None, with_db=True, with_history=False, wal=False):
    """Build a realistic <appDataDir>/brain/<uuid>/.system_generated/logs/ layout."""
    app_dir = Path(base) / "antigravity-cli"
    logs = app_dir / "brain" / conv_id / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    full = logs / "transcript_full.jsonl"
    if rows is not None:
        full.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    else:
        shutil.copy(fixture, full)
    # Write compact + chunk duplicates that discovery and cite must ignore.
    (logs / "transcript.jsonl").write_text(full.read_text(encoding="utf-8"), encoding="utf-8")
    for sub in ("transcript", "transcript_full"):
        chunk_dir = logs / "chunks" / sub
        chunk_dir.mkdir(parents=True, exist_ok=True)
        (chunk_dir / "00000000.jsonl").write_text(full.read_text(encoding="utf-8"), encoding="utf-8")
    if with_db:
        db = app_dir / "conversation_summaries.db"
        con = sqlite3.connect(str(db))
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                workspace_uris TEXT NOT NULL,
                parent_conversation_id TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS conversation_parents (
                conversation_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL
            )
        """)
        con.execute("INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?)",
                    (conv_id, json.dumps(list(workspace_uris)), parent_id))
        con.commit()
        con.close()
    if with_history:
        ws = antigravity.uri_to_path(workspace_uris[0]) if workspace_uris else ""
        (app_dir / "history.jsonl").write_text(
            json.dumps({"display": "hi", "timestamp": 1, "workspace": ws, "conversationId": conv_id}) + "\n",
            encoding="utf-8")
    return app_dir, full


def scan(path=FIXTURE):
    scope = new_scope()
    recording = scan_file(path, ScanOptions(harness="auto", max_text_bytes=1024 * 1024), scope)
    return recording, scope


class AntigravityTests(unittest.TestCase):
    def test_detect_distinguishes_antigravity_from_other_harnesses_and_history(self):
        first = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(detected(first), "antigravity")
        for other, name in (("pi-slice1.jsonl", "pi"), ("dsh-slice1.jsonl", "dsh")):
            sample = json.loads((ROOT / "tests/fixtures/recordings" / other).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(detected(sample), name)
        history_row = {"display": "/plan", "timestamp": 1795615211386, "workspace": "/work/project",
                       "conversationId": CONV_ID, "type": "slash_command"}
        self.assertIsNone(detected(history_row))

    def test_positional_pairing_ancestry_exit_codes_and_unknown_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            _, full = make_archive(temp)
            recording, scope = scan(full)
        self.assertEqual(recording["harness"], "antigravity")
        self.assertEqual(recording["session"], CONV_ID)
        self.assertEqual(recording["root"], "/work/project")
        kinds = {e["kind"] for e in recording["events"]}
        self.assertLessEqual({"user_message", "assistant_message", "reasoning", "tool_call", "tool_result",
                              "instruction", "unknown"}, kinds)
        # Step 0 must be registered as "step-0" (non-falsy) and anchor the linear ancestry chain.
        users = [e for e in recording["events"] if e["kind"] == "user_message"]
        self.assertEqual([(u["external_message_id"], u["origin"]) for u in users],
                         [("step-0", "human"), ("step-10", "harness")])
        self.assertEqual(users[1]["channel"], "injected")
        self.assertEqual(users[1]["turn"], "step-0")
        done = next(e for e in recording["events"] if e["kind"] == "assistant_message" and e["text"] == "Done.")
        self.assertEqual(done["request_basis"], "recorded_ancestry")
        self.assertEqual(done["turn"], "step-0")
        self.assertEqual(done["lane"], "step-0")
        # SYSTEM_MESSAGE becomes an injected system instruction.
        instructions = [e for e in recording["events"] if e["kind"] == "instruction"]
        self.assertEqual([(i["origin"], i["channel"], i["purpose"]) for i in instructions],
                         [("system", "injected", "system_message")])
        # Unrecognized future step type stays visible as an unknown gap.
        self.assertEqual([e["native_type"] for e in recording["events"] if e["kind"] == "unknown"],
                         ["FUTURE_STEP_KIND"])
        # Positional pairing across parallel tool calls, exit codes and ERROR status.
        calls = [e for e in recording["events"] if e["kind"] == "tool_call"]
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(c.get("result_ref") for c in calls))
        self.assertEqual(scope["unpaired_results"], 0)
        self.assertEqual(scope["calls_without_result"], 0)
        by_tool = [(c["tool"], c["outcome"], c.get("exit_code"), c.get("path"), c.get("command")) for c in calls]
        self.assertEqual(by_tool, [
            ("read_file", "succeeded", None, "app.py", None),
            ("run_command", "failed", 1, None, "pytest -q"),
            ("edit_file", "succeeded", None, "app.py", None),
            ("write_file", "failed", None, "notes.md", None),
            ("run_command", "succeeded", 0, None, 'git commit -m "Return 2"'),
        ])
        self.assertTrue(calls[1]["is_shell"])
        self.assertTrue(calls[4]["is_shell"])
        # No internal private keys leak onto events.
        self.assertFalse(any("_ag_exit_code" in e for e in recording["events"]))

    def test_positional_pairing_flushes_on_turn_change_gap_and_orphan_results(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "edge.jsonl"
            rows = [
                {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                 "created_at": "2026-11-25T14:00:00Z", "content": "<USER_REQUEST>One</USER_REQUEST>"},
                # Two tool calls, but only one result before a new USER_INPUT.
                {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                 "created_at": "2026-11-25T14:00:01Z", "tool_calls": [
                     {"name": "read_file", "args": {"Path": "/a.py"}},
                     {"name": "read_file", "args": {"Path": "/b.py"}}]},
                {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE",
                 "created_at": "2026-11-25T14:00:02Z", "content": "Showing lines 1-1 of 1\n1: a"},
                {"step_index": 3, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                 "created_at": "2026-11-25T14:00:03Z", "content": "<USER_REQUEST>Two</USER_REQUEST>"},
                # Orphan GENERIC after USER_INPUT must NOT pair with leftover /b.py call.
                {"step_index": 4, "source": "MODEL", "type": "GENERIC", "status": "DONE",
                 "created_at": "2026-11-25T14:00:04Z", "content": "Showing lines 1-1 of 1\n1: orphan"},
                # Truncated command output still preserves exit code 0.
                {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                 "created_at": "2026-11-25T14:00:05Z", "tool_calls": [
                     {"name": "run_command", "args": {"CommandLine": "echo big"}}]},
                {"step_index": 6, "source": "MODEL", "type": "GENERIC", "status": "DONE",
                 "created_at": "2026-11-25T14:00:06Z", "content": (
                     "Created At: 2026-11-25T06:00:05-08:00\nCompleted At: 2026-11-25T06:00:06-08:00\n"
                     "The tool's output was truncated due to being too long.\n\n"
                     "Here is a preview with the middle of the output truncated:\n"
                     "Created At: 2026-11-25T06:00:05-08:00\nCompleted At: 2026-11-25T06:00:06-08:00\n"
                     "Command exited with code 0.\n\nCommand output:\nbig")},
                # Backgrounded async command without exit code stays unknown (not falsely succeeded).
                {"step_index": 7, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                 "created_at": "2026-11-25T14:00:07Z", "tool_calls": [
                     {"name": "run_command", "args": {"CommandLine": "sleep 60"}}]},
                {"step_index": 8, "source": "MODEL", "type": "GENERIC", "status": "DONE",
                 "created_at": "2026-11-25T14:00:08Z", "content": "Command running in background as task t1."},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            scope = new_scope()
            recording = scan_file(path, ScanOptions(harness="auto", max_text_bytes=128), scope)
        calls = [e for e in recording["events"] if e["kind"] == "tool_call"]
        self.assertTrue("result_ref" in calls[0])
        self.assertNotIn("result_ref", calls[1])
        self.assertEqual(scope["calls_without_result"], 1)
        self.assertEqual(scope["unpaired_results"], 1)
        # Even under tight max_text_bytes=128, exit code 0 was extracted before clipping.
        self.assertEqual((calls[2]["outcome"], calls[2]["exit_code"]), ("succeeded", 0))
        self.assertEqual((calls[3]["outcome"], calls[3].get("exit_code")), ("unknown", None))

    def test_message_segments_isolate_user_request_from_metadata(self):
        text = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])["content"]
        self.assertEqual(ms.prose(text), "Update app.py to return 2 and verify with pytest.")
        self.assertNotIn("walkthrough", ms.prose(text))
        self.assertNotIn("Model Selection", ms.visible(text))
        self.assertEqual([(s.kind, s.name) for s in ms.parse(text)], [
            ("harness_context", "user_request"),
            ("prose", None),
            ("harness_context", "user_request"),
            ("harness_context", "additional_metadata"),
            ("harness_context", "user_settings_change"),
        ])

    def test_read_adapter_reports_complete_range_truncated_and_rejects_outer_truncation(self):
        complete_text = (
            "Created At: 2026-11-25T06:00:00-08:00\nCompleted At: 2026-11-25T06:00:00-08:00\n"
            "Showing lines 1-3 of 3\n1: def main():\n2:     return 1\n3: "
        )
        view = read_view("antigravity", {}, complete_text, {})
        self.assertEqual((view["start_line"], view["line_count"], view["total_lines"],
                          view["completeness"], view["adapter"]),
                         (1, 3, 3, "complete", "antigravity:read_file"))
        self.assertEqual(view["content"], "def main():\n    return 1\n")

        ranged_text = "Showing lines 2-3 of 10\n2: x = 1\n3: y = 2"
        view = read_view("antigravity", {}, ranged_text, {"StartLine": 2, "EndLine": 3})
        self.assertEqual((view["start_line"], view["line_count"], view["total_lines"], view["completeness"]),
                         (2, 2, 10, "range"))

        # Unranged partial read (e.g. 800-line cap) is truncated.
        view = read_view("antigravity", {}, "Showing lines 1-2 of 10\n1: a\n2: b", {})
        self.assertEqual(view["completeness"], "truncated")

        # Outer tool-output truncation never claims a valid view.
        outer = (
            "Created At: 2026-11-25T06:00:00-08:00\n"
            "The tool's output was truncated due to being too long.\n"
            "Showing lines 1-3 of 3\n1: a\n3: c"
        )
        self.assertIsNone(read_view("antigravity", {}, outer, {}))
        self.assertIsNone(read_view("antigravity", {}, "not a read result", {}))

    def test_edit_hunks_handle_edit_write_and_append(self):
        record = {"tool_calls": [
            {"name": "edit_file", "args": {"Path": "/work/project/app.py",
                                           "Target": "def main():\n    return 1\n",
                                           "Replacement": "def main():\n    return 2\n"}},
            {"name": "write_file", "args": {"Path": "/work/project/n.md", "Content": "# N\n", "Overwrite": True}},
            {"name": "write_file", "args": {"Path": "/work/project/n.md", "Content": "more\n", "Append": True}},
        ]}
        edit_ev = {"harness": "antigravity", "kind": "tool_call", "tool": "edit_file", "operation": "file_edit",
                   "payload_pointer": "/tool_calls/0/args", "outcome": "succeeded"}
        self.assertIs(edit_hunks._extractor(edit_ev), edit_hunks._antigravity)
        found, _ = edit_hunks._antigravity(edit_ev, record)
        change, source, hunks, original, after, truncated = found
        self.assertEqual((change, source, original, after, truncated),
                         ("update", "antigravity_edit_arguments", None, None, 0))
        self.assertEqual(hunks[0][4], ["-    return 1", "+    return 2"])

        write_ev = {**edit_ev, "tool": "write_file", "payload_pointer": "/tool_calls/1/args"}
        found, _ = edit_hunks._antigravity(write_ev, record)
        self.assertEqual((found[0], found[1], found[4]), ("write", "antigravity_write_content", "# N\n"))

        append_ev = {**edit_ev, "tool": "write_file", "payload_pointer": "/tool_calls/2/args"}
        found, _ = edit_hunks._antigravity(append_ev, record)
        self.assertEqual((found[0], found[1], found[4]), ("append", "antigravity_write_append", None))

    def test_operations_observations_and_commit_witnesses(self):
        with tempfile.TemporaryDirectory() as temp:
            _, full = make_archive(temp)
            recording, _ = scan(full)
            cache = RecordCache()
            ops, op_cov = operation_facts.operations([recording], cache)
            self.assertEqual(op_cov["paired"], 5)
            self.assertEqual(op_cov["failed"], 2)
            repo_path = lambda op: op.get("path") if op.get("path_location") == "repository" else None
            observations, changes, _ = file_observations.build([recording], cache, ops, repo_path)
            read = next(r for r in observations if r["kind"] == "read_result" and r["path"] == "app.py")
            self.assertEqual((read["start_line"], read["line_count"], read["total_lines"], read["completeness"]),
                             (1, 3, 3, "complete"))
            edit = next(r for r in observations if r["kind"] == "native_patch" and r["path"] == "app.py")
            self.assertEqual(edit["adapter"], "antigravity_edit_arguments")
            self.assertTrue(changes)
            witnesses = commit_links.witnesses([recording], GitResolver(), cache)
            self.assertEqual(len(witnesses), 1)
            self.assertEqual(witnesses[0]["recorded_sha"], "1234567")
            self.assertEqual(witnesses[0]["subject"], "Return 2")

    def test_discovery_deduplicates_reads_wal_multiroot_parent_and_history_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            # 1) WAL DB + multi-root + parent_conversation_id + percent-encoded space
            app_dir, full = make_archive(
                temp, conv_id=CONV_ID,
                workspace_uris=("file:///other/repo", "file:///work/My%20Project"),
                parent_id=PARENT_ID, wal=True)
            scope = new_scope()
            groups = candidate_files(
                DiscoveryOptions(files=(str(app_dir / "brain"),), project="/work/My Project"), scope)
            ag = next(g for g in groups if g[0] == "antigravity")
            self.assertEqual(ag[1], [str(full.absolute())])
            self.assertEqual(ag[2]["files_selected"], 1)
            self.assertEqual(scope["candidates_found"], 1)  # transcript.jsonl and chunks/ were skipped
            recording, _ = scan(full)
            self.assertEqual(recording["parent_session"], PARENT_ID)

            # 2) Fallback to history.jsonl when conversation_summaries.db is absent
            conv2 = "22222222-3333-4444-5555-666666666666"
            app2, full2 = make_archive(
                Path(temp) / "hist", conv_id=conv2,
                workspace_uris=("file:///work/from-history",), with_db=False, with_history=True)
            groups2 = candidate_files(
                DiscoveryOptions(files=(str(app2 / "brain"),), project="/work/from-history"), new_scope())
            ag2 = next(g for g in groups2 if g[0] == "antigravity")
            self.assertEqual(ag2[1], [str(full2.absolute())])

            # 3) Missing DB and history -> reported honestly as project_unknown
            conv3 = "33333333-4444-5555-6666-777777777777"
            app3, _ = make_archive(Path(temp) / "nodb", conv_id=conv3, with_db=False, with_history=False)
            groups3 = candidate_files(
                DiscoveryOptions(files=(str(app3 / "brain"),), project="/work/project"), new_scope())
            ag3 = next(g for g in groups3 if g[0] == "antigravity")
            self.assertEqual(ag3[1], [])
            self.assertEqual(ag3[2]["project_unknown"], 1)

        # Windows file:// URI normalization
        self.assertEqual(antigravity.uri_to_path("file:///C:/Users/dev/My%20Repo"), "C:/Users/dev/My Repo")

    def test_cite_tokens_resolve_canonically_and_env_excludes_current_session(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            cache = Path(temp) / "cache"
            repo.mkdir()
            (repo / "app.py").write_text("def main():\n    return 2\n", encoding="utf-8")
            rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
            # Rewrite fixture paths to point inside temp repo.
            for r in rows:
                for tc in r.get("tool_calls") or []:
                    if "Path" in tc.get("args", {}):
                        tc["args"]["Path"] = str(repo / Path(tc["args"]["Path"]).name)
            app_dir, full = make_archive(temp, conv_id=CONV_ID, workspace_uris=(repo.as_uri(),), rows=rows)
            self.assertEqual(cite.recording_id(full), CONV_ID)
            recording, _ = scan(full)
            first_ev = recording["events"][0]
            tok = cite.token(first_ev["ref"])
            self.assertTrue(tok.startswith(f"{CONV_ID}@1#"))
            self.assertEqual(read_ref(first_ev["ref"])["type"], "USER_INPUT")
            with mock.patch("helper.cite.default_recordings", return_value={"antigravity": [app_dir / "brain"]}):
                cite._recording.cache_clear()
                resolved = cite.resolve(tok)
                self.assertEqual(resolved["file"], str(full.absolute()))
                self.assertEqual(resolved["line"], 1)

            empty = Path(temp) / "empty"
            empty.mkdir()
            settings = Path(temp) / "settings.json"
            settings.write_text(json.dumps({"recordings": {
                "pi": [str(empty)], "codex": [str(empty)], "claude-code": [str(empty)],
                "dsh": [str(empty)], "antigravity": [str(app_dir / "brain")]}}), encoding="utf-8")
            base_env = {"TRACE_SESSIONS_SETTINGS": str(settings), **{n: None for n in ENV_DROP}}

            # End-to-end query + investigate + show
            q = invoke(["query", "SELECT session, harness, tool_calls, failed_calls, first_request FROM sessions",
                        "--repo", str(repo), "--cache", str(cache)], env=base_env)
            self.assertEqual(q.returncode, 0, q.stdout + q.stderr)
            srow = q.json()["results"][0]
            self.assertEqual((srow["session"], srow["harness"], srow["tool_calls"], srow["failed_calls"]),
                             (CONV_ID, "antigravity", 5, 2))
            self.assertEqual(srow["first_request"], "Update app.py to return 2 and verify with pytest.")

            # Injected <ADDITIONAL_METADATA> "must never" must NOT become a human invariant lead.
            inv = invoke(["investigate", "walkthrough", "--repo", str(repo), "--cache", str(cache)], env=base_env)
            self.assertEqual(inv.returncode, 0, inv.stdout + inv.stderr)
            self.assertFalse([r for r in inv.json()["results"]
                              if r.get("section") == "lead" and "invariant" in r.get("categories", [])])

            # ANTIGRAVITY_CONVERSATION_ID automatically excludes the active session.
            excl = invoke(["query", "SELECT session FROM sessions", "--repo", str(repo), "--cache", str(cache)],
                          env={**base_env, "ANTIGRAVITY_CONVERSATION_ID": CONV_ID})
            self.assertEqual(excl.returncode, 0, excl.stdout + excl.stderr)
            self.assertEqual(excl.json()["results"], [])

            # show <cite> expands the event and its turn request.
            sh = invoke(["show", tok, "--repo", str(repo), "--cache", str(cache)], env=base_env)
            self.assertEqual(sh.returncode, 0, sh.stdout + sh.stderr)
            self.assertTrue(any(r.get("selected") for r in sh.json()["results"]))


if __name__ == "__main__":
    unittest.main()
