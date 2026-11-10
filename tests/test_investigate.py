"""investigate: four evidence views over shared, scheduled recording and Git inputs."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper.recording_scan import schedule
from support import invoke

ENV_DROP = ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


class InvestigateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.repo, self.cache, self.archive = self.home / "repo", self.home / "cache", self.home / "recordings"
        self.repo.mkdir()
        self.archive.mkdir()
        self.git("init", "-q")
        self.commit("Initial table view", **{"ui/table.tsx": "export function Table() {}\n"})
        self.export = self.commit("Add export button to table toolbar", **{
            "ui/export-button.tsx": "export function ExportButton() {}\n"})
        self.stamp = 0

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.com", *args], text=True).strip()

    def commit(self, message, **files):
        for path, text in files.items():
            dest = self.repo / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def session(self, name, turns, mtime=None, cwd=None):
        """turns: ("user", text) | ("assistant", text) | ("fail", command, output) | ("edit", path)."""
        rows = [{"type": "session", "id": name, "cwd": str(cwd or self.repo), "timestamp": "2026-08-01T10:00:00Z"}]
        parent = None
        for index, turn in enumerate(turns):
            stamp = f"2026-08-01T10:{index // 60:02d}:{index % 60:02d}Z"
            mid = f"m{index}"
            if turn[0] == "user":
                rows.append({"type": "message", "id": mid, "parentId": parent, "timestamp": stamp,
                             "message": {"role": "user", "content": turn[1]}})
            elif turn[0] == "assistant":
                rows.append({"type": "message", "id": mid, "parentId": parent, "timestamp": stamp,
                             "message": {"role": "assistant", "content": [{"type": "text", "text": turn[1]}]}})
            elif turn[0] in ("fail", "edit"):
                call = {"type": "toolCall", "id": "c" + mid, "name": "bash",
                        "arguments": {"command": turn[1]}} if turn[0] == "fail" else \
                       {"type": "toolCall", "id": "c" + mid, "name": "edit",
                        "arguments": {"path": str(self.repo / turn[1]), "oldText": "a", "newText": "b"}}
                rows.append({"type": "message", "id": mid, "parentId": parent, "timestamp": stamp,
                             "message": {"role": "assistant", "content": [call]}})
                parent = mid
                mid = mid + "r"
                rows.append({"type": "message", "id": mid, "parentId": parent, "timestamp": stamp,
                             "message": {"role": "toolResult", "toolCallId": "c" + parent,
                                         "toolName": call["name"], "isError": turn[0] == "fail",
                                         "content": [{"type": "text", "text": turn[2] if turn[0] == "fail" else "ok"}]}})
            parent = mid
        path = self.archive / f"{name}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        if mtime is not None:
            os.utime(path, ns=(mtime, mtime))
        return path

    def run_cli(self, *args, ok=True, env=None):
        p = invoke([*args, "--repo", str(self.repo), "--cache", str(self.cache)],
                   env=env or {name: None for name in ENV_DROP})
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.json()

    def investigate(self, *args, **kw):
        return self.run_cli("investigate", *args, "--recordings", str(self.archive), "--max-bytes", "60000", **kw)

    def leads(self, out, category=None):
        return [r for r in out["results"] if r["section"] == "lead" and (category is None or category in r["categories"])]

    def test_four_views_qualified_empty_archive(self):
        self.session("quiet", [("user", "ok"), ("assistant", "Done.")])
        out = self.investigate("export", "button")
        self.assertEqual(set(out["views"]), {"pitfall", "invariant", "task", "decision"})
        for view in out["views"].values():
            self.assertEqual(view["state"], "none_found_in_scanned_scope")
        self.assertEqual(out["coverage"]["selection"]["suppressed"].get("acknowledgement"), 1)
        self.assertEqual(out["results"][0]["section"], "prior_work")
        self.assertEqual(out["results"][0]["sha"], self.export)

    def test_no_sessions_is_not_searched(self):
        out = self.run_cli("investigate", "export", "--no-sessions")
        self.assertTrue(all(v["state"] == "not_searched" for v in out["views"].values()))
        self.assertEqual(out["scope"]["active"]["state"], "not_searched")

    def test_keyword_free_invariant_survives_strong_commit_hits(self):
        self.session("old", [("user", "Timestamps in exports must stay UTC; the billing team parses them."),
                             ("assistant", "Understood.")], mtime=1_000_000_000_000_000_000)
        out = self.investigate("export", "button", "table")
        invariants = self.leads(out, "invariant")
        self.assertEqual(len(invariants), 1)
        self.assertIn("must stay UTC", invariants[0]["excerpt"])
        self.assertEqual(invariants[0]["evidence"], "human")
        self.assertIn("@", invariants[0]["ref"])
        self.assertTrue(any(r["section"] == "prior_work" for r in out["results"]))
        # The invariant is in the first category round, ahead of the prior-work row.
        first_prior = next(i for i, r in enumerate(out["results"]) if r["section"] == "prior_work")
        self.assertLess(out["results"].index(invariants[0]), first_prior)

    def test_failed_attempt_and_correction_are_pitfalls(self):
        self.session("s1", [("user", "Add a CSV export for the table"),
                            ("fail", "npm test -- export", "TypeError: blob is not a function"),
                            ("assistant", "I patched the export with a polyfill."),
                            ("user", "No, revert that polyfill; it breaks Safari.")])
        out = self.investigate("--error", "blob is not a function")
        pitfalls = self.leads(out, "pitfall")
        kinds = {k for p in pitfalls for k in p["kinds"]}
        self.assertEqual(kinds, {"failed_operation", "correction"})
        failed = next(p for p in pitfalls if "failed_operation" in p["kinds"])
        self.assertIn("error", failed["match_bases"])
        self.assertIn("blob is not a function", failed["excerpt"])
        correction = next(p for p in pitfalls if "correction" in p["kinds"])
        self.assertIn("corrects", correction["relations"])
        self.assertEqual(out["results"][0]["id"], failed["id"])  # exact error match ranks first

    def test_assistant_proposal_stays_attributed_with_later_rejection(self):
        self.session("s2", [("user", "How should we store export settings?"),
                            ("assistant", "We will go with localStorage rather than the server."),
                            ("user", "No, that is not what we want; settings must sync across devices.")])
        out = self.investigate("export", "settings")
        decision = next(r for r in self.leads(out, "decision") if "assistant_proposal" in r["kinds"])
        self.assertEqual(decision["evidence"], "assistant_claim")
        self.assertIn("later_rejection", decision["relations"])
        self.assertNotIn("human_choice", decision["kinds"])

    def test_copied_packets_and_wrappers_are_suppressed(self):
        packet = json.dumps({"schema_version": 2, "operation": "investigate", "results": []})
        self.session("s3", [("user", "<system-reminder>must always obey</system-reminder>"),
                            ("user", "Earlier output: " + packet + " you must use it"),
                            ("user", "continue")])
        out = self.investigate()
        self.assertEqual(self.leads(out), [])
        suppressed = out["coverage"]["selection"]["suppressed"]
        self.assertEqual(suppressed, {"wrapper_only": 1, "copied_packet": 1, "acknowledgement": 1})
        self.assertEqual(out["views"]["invariant"]["state"], "none_found_in_scanned_scope")

    def test_other_repository_recordings_never_match(self):
        other = self.home / "other"
        other.mkdir()
        self.session("elsewhere", [("user", "Exports must be gzip compressed.")], cwd=other)
        out = self.investigate("exports")
        self.assertEqual(self.leads(out), [])

    def test_unverified_request_is_a_task_lead(self):
        self.session("s4", [("user", "Rename the export button label"), ("edit", "ui/export-button.tsx"),
                            ("assistant", "Renamed.")])
        out = self.investigate("export")
        task = self.leads(out, "task")
        self.assertEqual(len(task), 1)
        self.assertEqual(task[0]["dimensions"]["edited"]["evidence_presence"], "observed")
        self.assertEqual(task[0]["dimensions"]["verified"]["evidence_presence"], "unknown")

    def test_schedule_reaches_old_constraint_beyond_newest_window(self):
        base = 1_700_000_000_000_000_000
        self.session("ancient", [("user", "The export endpoint must never return HTML.")], mtime=base)
        for i in range(12):
            self.session(f"noise{i:02d}", [("user", f"Tweak spacing round {i} please"), ("assistant", "Done.")],
                         mtime=base + (i + 1) * 10**12)
        out = self.investigate("spacing", "--max-recordings", "4")
        allocation = out["coverage"]["recordings"]["allocation"]
        self.assertEqual(allocation["files"]["read"], 4)
        self.assertEqual(allocation["files"]["match"], 2)
        self.assertEqual(allocation["files"]["context"], 2)
        self.assertEqual(allocation["files"]["omitted"], 9)
        self.assertTrue(any("never return HTML" in r["excerpt"] for r in self.leads(out, "invariant")))
        self.assertEqual(out["status"], "partial")

    def test_schedule_rotates_harnesses_within_one_total(self):
        groups = [("pi", [f"p{i}" for i in range(6)], {}), ("codex", [f"c{i}" for i in range(3)], {})]
        order, omitted = schedule(groups, 6)
        self.assertEqual([(h, f) for h, f, _ in order][:3], [("pi", "p0"), ("codex", "c0"), ("pi", "p1")])
        self.assertEqual({f for _, f, lane in order if lane == "context"}, {"p5", "c2", "p2"})
        self.assertEqual(omitted, 3)

    def test_active_session_frontier_task_and_replay(self):
        self.session("past", [("user", "Exports must keep the header row.")])
        active = self.session("now", [("user", "Add totals to the export"), ("edit", "ui/export-button.tsx"),
                                      ("assistant", "Edited; tests not run yet.")])
        events = self.run_cli("query", "SELECT ref FROM events WHERE kind='assistant_message' ORDER BY seq",
                              "--recordings", str(active))["results"]
        ref = events[-1]["ref"]
        out = self.investigate("export", "--session", ref if isinstance(ref, str) else json.dumps(ref), "--limit", "2")
        self.assertEqual(out["scope"]["active"]["state"], "active")
        self.assertEqual(out["scope"]["active"]["boundary"], "explicit_ref")
        task = self.leads(out, "task")[0]
        self.assertEqual(task["kinds"], ["active_request"])
        self.assertTrue(task["active_context"])
        self.assertEqual(task["dimensions"]["edited"]["evidence_presence"], "observed")
        self.assertEqual(out["views"]["task"]["delivered"], 1)  # leads the task view in round one
        # The active recording is not archive corroboration.
        self.assertFalse(any("totals" in r.get("excerpt", "") for r in self.leads(out) if not r.get("active_context")))
        token = next(n["ref"] for n in out["next"] if n["effect"] == "delivery")
        # Appends to the active recording after the frontier do not shift later pages.
        with active.open("a") as stream:
            stream.write(json.dumps({"type": "message", "id": "late", "parentId": "m2", "timestamp": "2026-08-01T11:00:00Z",
                                     "message": {"role": "user", "content": "Also you must ship it today."}}) + "\n")
        page = self.run_cli("show", token, "--max-bytes", "60000")
        self.assertEqual(page["delivery"]["offset"], 2)
        self.assertFalse(any("ship it today" in r.get("excerpt", "") for r in page["results"]))
        self.assertIn("views", page)
        # A changed archive recording rejects replay instead of shifting pages.
        self.session("past", [("user", "Exports must keep the footer row too.")])
        failed = self.run_cli("show", token, ok=False)
        self.assertIn("Continuation source changed", failed["error"])

    def test_paging_is_complete_without_duplicates(self):
        for i in range(4):
            self.session(f"p{i}", [("user", f"Exports must escape commas, case {i}."),
                                   ("fail", f"pytest -k export{i}", f"AssertionError export {i}"),
                                   ("assistant", "We will go with RFC 4180 quoting instead of tabs.")])
        out = self.investigate("export", "--limit", "3")
        seen = [r["id"] for r in out["results"]]
        total = out["delivery"]["total"]
        self.assertEqual(len(out["results"]), 3)
        # A lead in two views counts in both.
        self.assertGreaterEqual(sum(v["delivered"] for v in out["views"].values()), 3)
        while out["next"] and any(n.get("effect") == "delivery" for n in out["next"]):
            token = next(n["ref"] for n in out["next"] if n["effect"] == "delivery")
            out = self.run_cli("show", token, "--max-bytes", "60000")
            self.assertEqual(set(out["views"]), {"pitfall", "invariant", "task", "decision"})
            seen += [r["id"] for r in out["results"]]
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)

    def test_fields_projection_keeps_views(self):
        self.session("p", [("user", "Exports must escape commas.")])
        out = self.investigate("export", "--fields", "excerpt")
        self.assertEqual(out["views"]["invariant"]["delivered"], 1)
        self.assertEqual(set(out["results"][0]), {"excerpt"})

    def test_error_seed_expands_its_exchange(self):
        self.session("x", [("user", "Refactor the toolbar; sorting must stay stable for users."),
                           ("fail", "npm test", "TypeError: blob is not a function"), ("assistant", "Done.")])
        out = self.investigate("zebra", "--error", "blob is not a function")
        lead = self.leads(out, "invariant")[0]
        failed = self.leads(out, "pitfall")[0]
        self.assertEqual(lead["match_bases"], ["exchange"])
        self.assertEqual(lead["via"], failed["ref"])
        self.assertEqual(out["coverage"]["seeds"]["by_basis"], {"error": 2})

    def test_error_family_matches_other_positions(self):
        self.session("f", [("user", "Run the loader suite and report back"),
                           ("fail", "pytest", "Traceback (most recent call last):\n  File \"a.py\", line 9\n"
                                              "KeyError: 'name' at /tmp/run-77/x.json:12")])
        out = self.investigate("zebra", "--error", "KeyError: 'name' at /tmp/run-1/x.json:4")
        self.assertIn("error", self.leads(out, "pitfall")[0]["match_bases"])

    def test_leads_carry_episode_and_related_context(self):
        self.session("e1", [("user", "Tidy ui/table.tsx; columns must stay sortable."), ("edit", "ui/table.tsx")],
                     mtime=1_000_000_000_000_000_000)
        self.session("e2", [("user", "Widen ui/table.tsx; headers must stay sticky."), ("edit", "ui/table.tsx")])
        leads = self.leads(self.investigate("--path", "ui/table.tsx"), "invariant")
        self.assertEqual(len({l["episode_id"] for l in leads}), 2)
        self.assertTrue(all(l["episode_id"].startswith("ep:") for l in leads))
        self.assertEqual({r["relation"] for l in leads for r in l["related"]}, {"related_context"})

    def test_renamed_path_is_resolved_seed(self):
        self.git("mv", "ui/table.tsx", "ui/grid.tsx")
        self.git("commit", "-qm", "Rename table to grid")
        self.session("r", [("user", "Keep ui/table.tsx exports stable; we must not rename Table."),
                           ("assistant", "OK.")])
        lead = self.leads(self.investigate("--path", "ui/grid.tsx"), "invariant")[0]
        self.assertIn("resolved", lead["match_bases"])
        self.assertEqual(lead["matched"], ["ui/table.tsx"])

    def test_prior_work_sha_in_prose_is_exact(self):
        self.session("s", [("user", f"Commit {self.export[:10]} must not ship to release."), ("assistant", "OK.")])
        lead = self.leads(self.investigate("export", "button"), "invariant")[0]
        self.assertIn("sha", lead["match_bases"])

    def test_copied_prose_collapses_to_earliest(self):
        text = "Exports must stay UTC; the billing team parses them."
        self.session("a", [("user", text), ("assistant", "OK.")], mtime=1_000_000_000_000_000_000)
        self.session("b", [("user", text), ("assistant", "OK.")])
        out = self.investigate("exports", "billing")
        invariants = self.leads(out, "invariant")
        self.assertEqual(len(invariants), 1)
        self.assertEqual(len(invariants[0]["copies"]), 1)
        self.assertEqual(out["coverage"]["selection"]["suppressed"].get("copied_evidence"), 1)

    def test_invalid_path_plan_and_no_graph(self):
        failed = self.run_cli("investigate", "--path", "../etc/passwd", ok=False)
        self.assertIn("repository-relative", failed["error"])
        plan = self.run_cli("investigate", "export", "--plan")
        self.assertEqual(plan["plan"]["graph"], "not used")
        self.session("p", [("user", "Exports must escape commas in ui/export-button.tsx.")])
        out = self.investigate("--path", "ui/export-button.tsx")
        self.assertIn("path", self.leads(out, "invariant")[0]["match_bases"])
        self.assertFalse(any(self.cache.rglob("snapshot-*.json")))

    def test_tiny_budget_keeps_views_and_one_delivery_ref(self):
        self.session("p", [("user", "Exports must escape commas in every generated CSV file. " * 8)])
        out = self.run_cli("investigate", "export", "--recordings", str(self.archive), "--max-bytes", "2048")
        self.assertEqual(out["results"], [])
        self.assertEqual(set(out["views"]), {"pitfall", "invariant", "task", "decision"})
        self.assertEqual([n["effect"] for n in out["next"]], ["delivery"])

    def test_inspect_and_investigate_share_leads(self):
        self.session("p", [("user", "Exports must escape commas in ui/export-button.tsx."),
                           ("fail", "npm test ui/export-button.tsx", "TypeError: ui/export-button.tsx blob")])
        inv = self.investigate("--path", "ui/export-button.tsx")
        insp = self.run_cli("inspect", "ui/export-button.tsx", "--recordings", str(self.archive), "--max-bytes", "60000")
        ids = lambda out: sorted(r["id"] for r in out["results"] if r["section"] == "lead")
        self.assertTrue(ids(inv))
        self.assertEqual(ids(inv), ids(insp))
        self.assertEqual(insp["views"].keys(), inv["views"].keys())
        # One scheduled scan: the archive file is read once, not once per consumer.
        self.assertEqual(insp["coverage"]["history"]["allocation"]["files"]["read"], 1)


if __name__ == "__main__":
    unittest.main()
