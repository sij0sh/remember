"""Join fidelity: recording-scoped requests, multi-session turns and commit witnesses."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import commit_rank, target_history


def event(recording, request, stamp):
    return {"kind": "user_message", "origin": "human", "request_origin": "human",
            "recording_id": recording, "session": "same", "text": request,
            "timestamp": stamp, "cite": f"{recording}@1#abc", "ref": {"file": recording, "offset": 1}}


class FidelityJoinTests(unittest.TestCase):
    def test_reused_session_id_does_not_cross_attribute_requests(self):
        events = [event("rec-a", "Request A", "2026-01-01T10:00:00Z"),
                  event("rec-b", "Request B", "2026-01-01T11:00:00Z")]
        links = [{"recording_id": "rec-a", "session": "same", "repo_id": "repo", "sha": "a" * 40,
                  "timestamp": "2026-01-01T10:01:00Z"},
                 {"recording_id": "rec-b", "session": "same", "repo_id": "repo", "sha": "b" * 40,
                  "timestamp": "2026-01-01T11:01:00Z"}]
        found = commit_rank.requests(events, links)
        self.assertEqual(found[("repo", "a" * 40)][0], "Request A")
        self.assertEqual(found[("repo", "b" * 40)][0], "Request B")

    def test_sibling_branch_correction_is_not_attached(self):
        rows = [
            {"kind": "user_message", "origin": "human", "lane": "u1", "order": 1,
             "text": "Keep lazy loading", "ref": {"file": "rec", "offset": 1}},
            {"kind": "user_message", "origin": "human", "lane": "u2", "order": 6,
             "text": "No, revert that; it broke the loader", "ref": {"file": "rec", "offset": 9}},
        ]
        found = target_history.intent({"kind": "tool_call", "lane": "u1", "order": 4, "signals": []},
                                      {}, rows, [])
        self.assertEqual(found["signals"], [])
        self.assertNotIn("followup", found)

    def test_compact_keeps_turns_from_two_recordings_and_all_witnesses(self):
        ref = lambda name, offset: {"file": name, "offset": offset, "length": 10, "sha256": "x" * 64, "line": 1}
        request = lambda name: {"text": "Request " + name, "origin": "human", "timestamp": "2026-01-01T10:00:00Z",
                                "ref": ref(name, 1)}
        rows = [
            {"category": "recorded_activity", "basis": "exact_file_activity", "ref": ref("rec-a", 5),
             "evidence": {"recording_id": "rec-a", "session": "same", "lane": "u1", "turn": "u1",
                          "operation": "file_read"},
             "intent": {"request": request("rec-a"), "signals": []}},
            {"category": "recorded_activity", "basis": "exact_file_activity", "ref": ref("rec-b", 5),
             "evidence": {"recording_id": "rec-b", "session": "same", "lane": "u1", "turn": "u1",
                          "operation": "file_edit"},
             "intent": {"request": request("rec-b"), "signals": []}},
        ]
        compact = target_history.compact(rows)
        self.assertEqual({row["text"] for row in compact}, {"Request rec-a", "Request rec-b"})
        sha = "a" * 40
        rows.append({"category": "git_change", "ref": sha, "repo": "/repo", "subject": "One commit",
                     "committed_at": "2026-01-01T12:00:00Z", "changes": []})
        rows += [{"category": "commit_witness", "ref": ref("rec-a", 9), "evidence": {"sha": sha, "repo_id": "/repo"}},
                 {"category": "commit_witness", "ref": ref("rec-b", 9), "evidence": {"sha": sha, "repo_id": "/repo"}}]
        compact = target_history.compact(rows)
        commit = next(row for row in compact if row["source"] == "git")
        self.assertEqual(commit["recorded_in"], ["rec-a", "rec-b"])


if __name__ == "__main__":
    unittest.main()
