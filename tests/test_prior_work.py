"""Golden path: a planned feature surfaces the earlier similar commit, its request and current paths (investigate prior_work)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import commit_rank
from support import invoke


class PriorWorkTests(unittest.TestCase):
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
            "ui/export-button.tsx": "export function ExportButton() {}\n",
            "ui/table.tsx": "import { ExportButton } from './export-button';\n"})
        self.commit("Tune table spacing", **{"ui/table.css": ".table { gap: 4px }\n"})

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

    def session(self, request, sha, subject):
        command = f'git commit -m "{subject}"'
        rows = [{"type": "session", "id": "past", "cwd": str(self.repo), "timestamp": "2026-08-01T10:00:00Z"},
                {"type": "message", "id": "u", "timestamp": "2026-08-01T10:00:01Z",
                 "message": {"role": "user", "content": request}},
                {"type": "message", "id": "a", "parentId": "u", "timestamp": "2026-08-01T10:05:00Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "toolCall", "id": "c", "name": "bash", "arguments": {"command": command}}]}},
                {"type": "message", "id": "r", "parentId": "a", "timestamp": "2026-08-01T10:05:01Z",
                 "message": {"role": "toolResult", "toolCallId": "c", "toolName": "bash", "isError": False,
                             "content": [{"type": "text", "text": f"[main {sha[:7]}] {subject}\n 2 files changed"}]}}]
        (self.archive / "past.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def run_cli(self, *words, sessions=True, ok=True):
        extra = ["--recordings", str(self.archive)] if sessions else ["--no-sessions"]
        p = invoke(["investigate", *words, *extra, "--max-bytes", "60000",
                    "--repo", str(self.repo), "--cache", str(self.cache)])
        self.assertEqual(p.returncode, 0 if ok else 2, p.stdout + p.stderr)
        return p.json()

    def prior(self, *words, sessions=True):
        out = self.run_cli(*words, sessions=sessions)
        return out, [r for r in out["results"] if r["section"] == "prior_work"]

    def test_planned_feature_finds_earlier_similar_commit(self):
        out, rows = self.prior("add", "a", "csv", "download", "button", "to", "the", "table", sessions=False)
        self.assertEqual(rows[0]["sha"], self.export)
        self.assertEqual(rows[0]["paths"][0], "ui/export-button.tsx")
        self.assertEqual(out["scope"]["goal_terms"], ["csv", "download", "button", "table"])
        self.assertEqual(out["scope"]["archive_horizon"]["commits"], 3)

    def test_linked_session_supplies_the_original_request(self):
        self.session("Users need to export the table; add a button", self.export, "Add export button to table toolbar")
        _, rows = self.prior("add", "a", "csv", "download", "button", "to", "the", "table")
        hit = next(r for r in rows[:3] if r["sha"] == self.export)
        self.assertEqual(hit["requests"][0]["request"], "Users need to export the table; add a button")
        self.assertIn("@", hit["requests"][0]["ref"])

    def test_moved_and_deleted_paths_report_current_state(self):
        (self.repo / "ui/actions").mkdir()
        self.git("mv", "ui/export-button.tsx", "ui/actions/export-button.tsx")
        self.git("rm", "-q", "ui/table.css")
        self.git("commit", "-qm", "Reorganize ui actions")
        _, rows = self.prior("export", "button", "table", "spacing", sessions=False)
        rows = {r["sha"]: r for r in rows}
        self.assertIn("ui/export-button.tsx -> ui/actions/export-button.tsx", rows[self.export]["paths"])
        spacing = next(r for r in rows.values() if r["subject"] == "Tune table spacing")
        self.assertEqual(spacing["paths"], ["ui/table.css -> gone"])

    def test_generic_words_only_is_an_input_error(self):
        out = self.run_cli("add", "new", "ui", sessions=False, ok=False)
        self.assertIn("descriptive words", out["error"])

    def test_recall_operation_is_retired(self):
        p = invoke(["recall", "export", "--repo", str(self.repo), "--cache", str(self.cache)])
        self.assertEqual(p.returncode, 2)
        self.assertIn("recall", p.stdout + p.stderr)

    def test_rare_terms_outweigh_project_wide_terms(self):
        commits = [{"sha": str(i), "subject": f"table tweak {i}", "committed_at": "2026-01-01"} for i in range(9)]
        commits.append({"sha": "x", "subject": "table export", "committed_at": "2025-01-01"})
        _, rows = commit_rank.rank("table export", commits, [], [], [], str(self.repo))
        self.assertEqual(rows[0]["sha"], "x")


if __name__ == "__main__":
    unittest.main()
