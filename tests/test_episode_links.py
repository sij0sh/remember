"""M3: episodes and bounded candidate links."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import episode_links


class Builder:
    """Synthetic recordings in reader shape: events, operations, witnesses."""

    def __init__(self):
        self.recordings, self.operations, self.witnesses = [], [], []

    def recording(self, name, session=None, parent=None, complete=True, stamp="2026-08-01T10"):
        rec = {"file": f"/rec/{name}.jsonl", "session": name, "parent_session": parent, "events": [],
               "scan_complete": complete, "_request": None, "_stamp": stamp}
        rec["_isession"] = session or name
        self.recordings.append(rec)
        return rec

    def _event(self, rec, kind, text, **extra):
        order = len(rec["events"])
        ref = {"file": rec["file"], "offset": order * 100, "item": 0}
        event = {"kind": kind, "text": text, "order": order, "ref": ref, "lane": "main",
                 "event_id": f"{rec['session']}-{order}", "timestamp": f"{rec['_stamp']}:{order:02d}:00Z",
                 "independent_session": rec["_isession"], **extra}
        if kind == "user_message":
            event.update(role="user", origin="human", request_origin="human")
            if not extra.get("ack"):
                rec["_request"] = ref
        event["request_ref"] = rec["_request"]
        event.pop("ack", None)
        rec["events"].append(event)
        return event

    def user(self, rec, text, **extra):
        return self._event(rec, "user_message", text, ack=len(text) < 12, **extra)

    def assistant(self, rec, text):
        return self._event(rec, "assistant_message", text, role="assistant")

    def op(self, rec, action, path, outcome="succeeded", output=""):
        call = self._event(rec, "tool_call", "")
        result = self._event(rec, "tool_result", output)
        self.operations.append({"operation_id": f"op-{call['event_id']}", "recording_id": rec["file"],
                                "action": action, "outcome": outcome, "repo_path": path, "path": path,
                                "path_location": "repository", "order": call["order"],
                                "request_ref": rec["_request"], "call_ref": call["ref"], "result_ref": result["ref"]})

    def commit(self, rec, sha):
        self.witnesses.append({"recording_id": rec["file"], "request_ref": rec["_request"], "sha": sha,
                               "order": len(rec["events"]), "failed": False})

    def build(self):
        return episode_links.build(self.recordings, self.operations, self.witnesses, "/repo")


def relations(links):
    return sorted(link["relation"] for link in links)


class EpisodeTests(unittest.TestCase):
    def test_acknowledgement_stays_in_its_episode_and_boundaries(self):
        b = Builder()
        rec = b.recording("a")
        b.user(rec, "Add the export button to the toolbar")
        b.assistant(rec, "Done.")
        b.user(rec, "ok")
        b.op(rec, "file_edit", "ui/export.tsx")
        b.user(rec, "Now document the export flag please")
        cut = b.recording("b", complete=False, stamp="2026-08-01T11")
        b.user(cut, "Investigate slow startup in the loader")
        rows, _, coverage, _ = b.build()
        self.assertEqual([r["boundary"] for r in rows], ["closed", "open", "truncated"])
        self.assertEqual((rows[0]["events"], rows[0]["operations"]), (5, 1))
        self.assertEqual(coverage["episodes"], 3)

    def test_forked_duplicate_collapses(self):
        b = Builder()
        for name in ("a", "b"):
            rec = b.recording(name, session="root")
            b.user(rec, "Add the export button to the toolbar", message_id="m1")
        rows, _, coverage, _ = b.build()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["recordings"]), 2)
        self.assertEqual(coverage["duplicates_merged"], 1)


class LinkTests(unittest.TestCase):
    def test_proposal_responses(self):
        b = Builder()
        rec = b.recording("a")
        b.user(rec, "How should we store the export settings?")
        b.assistant(rec, "I'd go with JSON instead of YAML.")
        b.user(rec, "yes")
        b.op(rec, "file_edit", "src/settings.py")
        b.user(rec, "Next, pick a cache layout for the build")
        b.assistant(rec, "Option one: we will use a flat dir.")
        b.assistant(rec, "Option two: I chose nested dirs.")
        b.user(rec, "ok")
        b.user(rec, "Then pick a log format for the server")
        b.assistant(rec, "We decided on plain text rather than JSON.")
        b.user(rec, "No, that breaks the parser; use JSON.")
        _, links, _, _ = b.build()
        responses = [(l["response"], len(l["alternatives"])) for l in links if l["relation"] == "proposal_response"]
        self.assertEqual(responses, [("accepted", 0), ("unclear", 1), ("unclear", 1), ("rejected", 0)])
        impl = [l for l in links if l["relation"] == "implementation_link"]
        self.assertEqual([l["matched"] for l in impl], [["src/settings.py"]])

    def test_regression_needs_fix_attempt_between_failures(self):
        error = "Traceback (most recent call last):\n  File \"x.py\", line 3\nKeyError: 'name'"
        b = Builder()
        first = b.recording("a", stamp="2026-08-01T10")
        b.user(first, "Fix the loader crash in src/loader.py")
        b.op(first, "command", "src/loader.py", "failed", error)
        b.op(first, "file_edit", "src/loader.py")
        b.assistant(first, "Fixed: the loader now defaults the name.")
        second = b.recording("b", stamp="2026-08-02T10")
        b.user(second, "The loader crashes again on start")
        b.op(second, "command", "src/loader.py", "failed", error.replace("line 3", "line 9"))
        _, links, _, _ = b.build()
        link, = links
        self.assertEqual((link["relation"], link["strength"], link["ordering_basis"]),
                         ("possible_regression", "supported", "timestamp_only"))
        self.assertIn("KeyError: 'name'", link["matched"])
        self.assertIn("src/loader.py", link["matched"])

    def test_same_problem_without_fix_and_named_commit_followup(self):
        b = Builder()
        first = b.recording("a", stamp="2026-08-01T10")
        b.user(first, "Run the loader tests for src/loader.py")
        b.op(first, "command", "src/loader.py", "failed", "ValueError: bad header")
        b.commit(first, "3f9ab21" + "0" * 33)
        second = b.recording("b", stamp="2026-08-02T10")
        b.user(second, "After 3f9ab21 the loader still fails")
        b.op(second, "command", "src/loader.py", "failed", "ValueError: bad header")
        _, links, _, _ = b.build()
        self.assertEqual(relations(links), ["explicit_followup", "possible_same_problem"])

    def test_parent_session_followup_and_path_context_bounds(self):
        b = Builder()
        parent = b.recording("p", stamp="2026-08-01T09")
        b.user(parent, "Plan the toolbar rework for the table")
        b.op(parent, "file_edit", "ui/table.tsx")
        child = b.recording("c", parent="p", stamp="2026-08-01T10")
        b.user(child, "Delegated: implement the toolbar rework")
        for index in range(7):
            b.user(child, f"Step {index}: adjust ui/table.tsx spacing")
            b.op(child, "file_edit", "ui/table.tsx")
        _, links, coverage, _ = b.build()
        followup = [l for l in links if l["relation"] == "explicit_followup"]
        self.assertEqual([l["basis"] for l in followup], ["parent_session"])
        context = [l for l in links if l["relation"] == "related_context" and l["source"] == links[0]["source"]]
        self.assertEqual(len(context), episode_links.PER_RELATION)
        self.assertGreater(coverage["links_omitted"], 0)
        self.assertNotIn("complete", coverage)


if __name__ == "__main__":
    unittest.main()


class TraceLinkTests(unittest.TestCase):
    def test_trace_emits_link_rows_and_episode_continuations(self):
        import json
        import tempfile
        from helper.options import ScanOptions
        from helper.reader import scan_file
        from helper.records import new_scope
        from support import invoke
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repo = home / "repo"
            (repo / "src").mkdir(parents=True)
            path = home / "s.jsonl"
            rows = [{"type": "session", "id": "s", "cwd": str(repo), "timestamp": "2026-08-01T10:00:00Z"}]
            parent = None

            def message(mid, role, content):
                nonlocal parent
                rows.append({"type": "message", "id": mid, "parentId": parent, "timestamp": "2026-08-01T10:00:00Z",
                             "message": {"role": role, "content": content}})
                parent = mid

            def edit(mid):
                message(mid, "assistant", [{"type": "toolCall", "id": "c" + mid, "name": "edit", "arguments": {
                    "path": str(repo / "src/app.py"), "oldText": "a", "newText": "b"}}])
                message(mid + "r", "toolResult", [{"type": "text", "text": "ok"}])
                rows[-1]["message"].update(toolCallId="c" + mid, toolName="edit", isError=False)
            message("u1", "user", "Choose the storage format for exports")
            message("a1", "assistant", [{"type": "text", "text": "We will use JSON instead of YAML."}])
            message("u2", "user", "yes")
            edit("e1")
            message("u3", "user", "Also adjust src/app.py logging for the exports")
            edit("e2")
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            recording = scan_file(path, ScanOptions(harness="pi", max_text_bytes=1 << 20), new_scope())
            ref = next(e["ref"] for e in recording["events"] if e.get("external_message_id") == "u1")
            out = invoke(["trace", json.dumps(ref), "--recordings", str(path), "--repo", str(repo),
                          "--cache", str(home / "cache")]).json()
        links = [r for r in out["results"] if r["section"] == "link"]
        self.assertEqual({(l["relation"], l["direction"]) for l in links},
                         {("proposal_response", "later"), ("implementation_link", "later"), ("related_context", "later")})
        response = next(l for l in links if l["relation"] == "proposal_response")
        self.assertEqual((response["response"], response["target"]), ("accepted", None))
        self.assertTrue(any("related context episode" in n["purpose"] for n in out["next"]))
