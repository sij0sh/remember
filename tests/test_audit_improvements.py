"""Tests for audit-driven improvements: active Pi exclusion, pitfall signal/noise, table readability, .agents paths."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMEMBER = ROOT / "scripts" / "remember.py"
ENV_DROP = ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID", "DSH_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID")


def _write_pi(path, cwd, entries, sid):
    header = {"type": "session", "version": 3, "id": sid, "timestamp": "2026-11-25T10:00:00.000Z", "cwd": str(cwd)}
    lines = [json.dumps(header)] + [json.dumps(e) for e in entries]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(*args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, str(REMEMBER), *args], capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0, f"cmd failed ({p.returncode}): {p.stdout}\n{p.stderr}"
    return p.stdout


class AuditImprovementsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.cache = self.root / "cache"
        self.sessions = self.root / "pi-sessions"
        self.sessions.mkdir()
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({"recordings": {"pi": [str(self.sessions)]}}), encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / ".gitignore").write_text(".agents/\n", encoding="utf-8")
        (self.repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "app.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self):
        return {"TRACE_SESSIONS_SETTINGS": str(self.settings)}

    def test_active_pi_session_auto_excluded_and_opt_in_include_current(self):
        old_file = self.sessions / "2026-11-20T10-00-00-000Z_old-session.jsonl"
        _write_pi(old_file, self.repo, [
            {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-11-20T10:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Fix the widget deadlock in app.py"}]}},
            {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-11-20T10:00:05Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Root cause: widget deadlocks when init waits before first prompt."}]}},
        ], "old-session")
        os.utime(old_file, (1700000000, 1700000000))

        live_file = self.sessions / "2026-11-25T11-00-00-000Z_live-session.jsonl"
        _write_pi(live_file, self.repo, [
            {"type": "message", "id": "u2", "parentId": None, "timestamp": "2026-11-25T11:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Fix the widget deadlock in app.py and must not break tests"}]}},
            {"type": "message", "id": "a2", "parentId": "u2", "timestamp": "2026-11-25T11:00:02Z",
             "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "tc1", "name": "bash",
                                                          "arguments": {"command": "python3 remember.py investigate widget deadlock"}}]}},
        ], "live-session")

        out = json.loads(_run("investigate", "widget", "deadlock", "--repo", str(self.repo), "--cache", str(self.cache),
                              env_extra=self._env()))
        sessions = {r.get("session") for r in out["results"] if r.get("section") == "lead"}
        self.assertIn("old-session", sessions)
        self.assertNotIn("live-session", sessions)

        out_inc = json.loads(_run("investigate", "widget", "deadlock", "--include-current",
                                  "--repo", str(self.repo), "--cache", str(self.cache), env_extra=self._env()))
        sessions_inc = {r.get("session") for r in out_inc["results"] if r.get("section") == "lead"}
        self.assertIn("live-session", sessions_inc)

    def test_pitfall_noise_suppressed_and_assistant_finding_indexed(self):
        rec = self.sessions / "2026-11-20T10-00-00-000Z_findings.jsonl"
        _write_pi(rec, self.repo, [
            {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-11-20T10:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Investigate skill expansion in app.py"}]}},
            {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-11-20T10:00:02Z",
             "message": {"role": "assistant", "content": [
                 {"type": "toolCall", "id": "tc1", "name": "edit", "arguments": {"path": "app.py"}},
                 {"type": "toolCall", "id": "tc2", "name": "bash", "arguments": {"command": "rg -n foo app.py"}},
                 {"type": "toolCall", "id": "tc3", "name": "read", "arguments": {"path": "missing.py"}},
             ]}},
            {"type": "message", "id": "r1", "parentId": "a1", "timestamp": "2026-11-20T10:00:03Z",
             "message": {"role": "toolResult", "toolCallId": "tc1", "toolName": "edit", "isError": True,
                         "content": [{"type": "text", "text": "Could not find edits[0] in app.py"}]}},
            {"type": "message", "id": "r2", "parentId": "r1", "timestamp": "2026-11-20T10:00:04Z",
             "message": {"role": "toolResult", "toolCallId": "tc2", "toolName": "bash", "isError": True,
                         "content": [{"type": "text", "text": "app.py\n.gitignore"}]}},
            {"type": "message", "id": "r3", "parentId": "r2", "timestamp": "2026-11-20T10:00:05Z",
             "message": {"role": "toolResult", "toolCallId": "tc3", "toolName": "read", "isError": True,
                         "content": [{"type": "text", "text": "ENOENT: no such file or directory, open 'missing.py'"}]}},
            {"type": "message", "id": "a2", "parentId": "r3", "timestamp": "2026-11-20T10:00:06Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "It also caught a Pi 1.5.2 quirk: /skill:name requires a space before its arguments; a newline leaves it unexpanded."}]}},
        ], "findings-session")
        os.utime(rec, (1700000000, 1700000000))

        out = json.loads(_run("investigate", "skill", "expansion", "quirk", "--repo", str(self.repo),
                              "--cache", str(self.cache), "--recordings", str(rec)))
        suppressed = out["coverage"]["selection"]["suppressed"]
        self.assertEqual(suppressed.get("transient_edit_mismatch"), 1)
        self.assertEqual(suppressed.get("probe_command_exit"), 1)
        self.assertEqual(suppressed.get("probe_missing_file"), 1)
        pitfalls = [r for r in out["results"] if r.get("section") == "lead" and "pitfall" in r.get("categories", [])]
        self.assertEqual(len(pitfalls), 1)
        self.assertIn("assistant_finding", pitfalls[0]["kinds"])
        self.assertIn("Pi 1.5.2 quirk", pitfalls[0]["excerpt"])

    def test_show_and_trace_table_readability(self):
        long_msg = ("Two changes are required for this to work: "
                    "1. Shadow-compact must preserve modelThinkingLevels before removing backup settings. "
                    "2. HTTP 400 insufficient credits is non-retryable so a hidden continuation is required.")
        rec = self.sessions / "2026-11-20T10-00-00-000Z_readable.jsonl"
        _write_pi(rec, self.repo, [
            {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-11-20T10:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": long_msg}]}},
            {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-11-20T10:00:02Z",
             "message": {"role": "assistant", "content": [
                 {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {"command": "npm test -- --grep failover"}}]}},
            {"type": "message", "id": "r1", "parentId": "a1", "timestamp": "2026-11-20T10:00:03Z",
             "message": {"role": "toolResult", "toolCallId": "tc1", "toolName": "bash", "isError": True,
                         "content": [{"type": "text", "text": "AssertionError: expected HTTP 400 to trigger continuation"}]}},
            {"type": "message", "id": "a2", "parentId": "r1", "timestamp": "2026-11-20T10:00:04Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Root cause: Pi treats HTTP 400 as non-retryable, so switching models at agent_end leaves the turn failed."}]}},
        ], "readable-session")
        os.utime(rec, (1700000000, 1700000000))

        inv = json.loads(_run("investigate", "failover", "HTTP", "400", "--repo", str(self.repo),
                              "--cache", str(self.cache), "--recordings", str(rec)))
        inv_lead = next(r for r in inv["results"] if r.get("section") == "lead" and "invariant" in r.get("categories", []))

        show_tbl = _run("show", inv_lead["ref"], "--repo", str(self.repo), "--cache", str(self.cache),
                        "--format", "table", env_extra=self._env())
        self.assertIn("hidden continuation is required.", show_tbl)

        trace_tbl = _run("trace", inv_lead["ref"], "--repo", str(self.repo), "--cache", str(self.cache),
                         "--format", "table", env_extra=self._env())
        self.assertIn("conclusion", trace_tbl)
        self.assertIn("Root cause: Pi treats HTTP 400 as non-retryable", trace_tbl)
        self.assertIn("npm test -- --grep failover", trace_tbl)
        self.assertIn("AssertionError: expected HTTP 400", trace_tbl)

    def test_gitignored_agents_path_works_in_inspect_and_activity(self):
        artifact = self.repo / ".agents" / "artifacts" / "plan.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Plan\nImportant steps here.\n", encoding="utf-8")

        rec = self.sessions / "2026-11-20T10-00-00-000Z_agents.jsonl"
        _write_pi(rec, self.repo, [
            {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-11-20T10:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Read .agents/artifacts/plan.md"}]}},
            {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-11-20T10:00:02Z",
             "message": {"role": "assistant", "content": [
                 {"type": "toolCall", "id": "tc1", "name": "read", "arguments": {"path": str(artifact)}}]}},
            {"type": "message", "id": "r1", "parentId": "a1", "timestamp": "2026-11-20T10:00:03Z",
             "message": {"role": "toolResult", "toolCallId": "tc1", "toolName": "read", "isError": False,
                         "content": [{"type": "text", "text": "# Plan\nImportant steps here.\n"}]}},
        ], "agents-session")
        os.utime(rec, (1700000000, 1700000000))

        insp = json.loads(_run("inspect", ".agents/artifacts/plan.md", "--repo", str(self.repo),
                               "--cache", str(self.cache), "--recordings", str(rec)))
        src_rows = [r for r in insp["results"] if r.get("section") == "source"]
        self.assertEqual(len(src_rows), 1)
        self.assertIn("Important steps here.", src_rows[0]["text"])

        act = json.loads(_run("activity", ".agents/artifacts/plan.md", "--repo", str(self.repo),
                              "--cache", str(self.cache), "--recordings", str(rec)))
        self.assertGreaterEqual(act["coverage"]["activity"]["matched_operations"], 1)


if __name__ == "__main__":
    unittest.main()
