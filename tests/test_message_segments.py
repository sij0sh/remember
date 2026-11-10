"""M1: task prose separated from embedded material; every excerpt resolves to its original span."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import message_segments as ms
from support import invoke

ENV_DROP = ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
SKILL = ('<skill name="guide" location="/skills/guide/SKILL.md">\n'
         + "You must never push without review. Always run the linter.\n" * 60 + "</skill>")


def kinds(text):
    return [(s.kind, s.basis) for s in ms.parse(text)]


class ParseTests(unittest.TestCase):
    def test_long_skill_block_then_error_report(self):
        text = SKILL + "\n\nI got this error: KeyError: 'name' in loader.py"
        self.assertEqual(kinds(text), [("reference", "skill_tag"), ("prose", "remainder")])
        self.assertEqual(ms.prose(text), "I got this error: KeyError: 'name' in loader.py")

    def test_multiple_skill_blocks_and_task_around_them(self):
        text = f"Start here.\n{SKILL}\nmiddle ask\n{SKILL}\nfinal ask"
        self.assertEqual([k for k, _ in kinds(text)], ["prose", "reference", "prose", "reference", "prose"])
        self.assertEqual(ms.prose(text), "Start here. middle ask final ask")

    def test_quote_and_code_are_not_prose(self):
        text = "Look:\n> you must never do X\nand\n```py\nassert must\n```\nthen fix it"
        self.assertEqual([k for k, _ in kinds(text)], ["prose", "quote", "prose", "code", "prose"])
        self.assertNotIn("must", ms.prose(text))

    def test_malformed_wrappers_are_unknown_not_deleted(self):
        self.assertEqual(kinds("<skill name=x>\nnever closed"), [("unknown", "unclosed_wrapper")])
        self.assertEqual(kinds("<skill name=a>\n<skill name=b>\n</skill>"), [("unknown", "unclosed_wrapper")])
        # A stored window that starts inside a block shows only its close tag.
        self.assertEqual(kinds("rule text\n</skill>\nreal ask"), [("unknown", "orphan_close"), ("prose", "remainder")])
        self.assertEqual(kinds("```\nunclosed"), [("code", "unclosed_fence")])

    def test_inline_tag_mention_is_prose(self):
        self.assertEqual(kinds("use the <skill> tag here"), [("prose", "remainder")])

    def test_harness_wrappers_anywhere_and_command_args_are_prose(self):
        text = ("<command-name>/plan</command-name>\n<command-args>Read the plan</command-args>"
                " <system-reminder>you must</system-reminder>")
        self.assertEqual(ms.prose(text), "Read the plan")
        self.assertNotIn("must", ms.visible(text))

    def test_unicode_offsets_are_code_points(self):
        text = "héllo 🚀 <system-reminder>x</system-reminder> wörld"
        prose = [s for s in ms.parse(text) if s.kind == "prose"]
        self.assertEqual(text[prose[1].start:prose[1].end].strip(), "wörld")

    def test_spans_cover_text_in_order(self):
        text = f"A\n{SKILL}\n> q\nB"
        segments = ms.parse(text)
        self.assertEqual([s.start for s in segments], sorted(s.start for s in segments))
        self.assertTrue(all(a.end <= b.start for a, b in zip(segments, segments[1:])))

    def test_rows_shift_by_stored_window_and_mark_partial(self):
        event = {"kind": "user_message", "role": "user", "origin": "human", "text": "tail\n</skill>\nask",
                 "text_start": 100, "text_omitted": 50, "event_id": "e", "recording_id": "r",
                 "ref": {"file": "/x.jsonl", "offset": 1}}
        rows, coverage = ms.rows([{"events": [event]}])
        self.assertEqual([r["kind"] for r in rows], ["unknown", "prose"])
        self.assertEqual(rows[1]["start_char"], 100 + event["text"].index("</skill>") + len("</skill>"))
        self.assertFalse(rows[1]["complete"])
        self.assertEqual((coverage["messages_partial"], coverage["orphan_close"]), (1, 1))
        self.assertFalse(coverage["complete"])


class InvestigateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.repo, self.cache, self.archive = home / "repo", home / "cache", home / "recordings"
        self.repo.mkdir()
        self.archive.mkdir()

    def session(self, *turns):
        rows = [{"type": "session", "id": "s", "cwd": str(self.repo), "timestamp": "2026-08-01T10:00:00Z"}]
        parent = None
        for index, (role, text) in enumerate(turns):
            content = text if role == "user" else [{"type": "text", "text": text}]
            rows.append({"type": "message", "id": f"m{index}", "parentId": parent,
                         "timestamp": f"2026-08-01T10:00:{index:02d}Z", "message": {"role": role, "content": content}})
            parent = f"m{index}"
        (self.archive / "s.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def investigate(self, *args):
        p = invoke(["investigate", *args, "--recordings", str(self.archive), "--max-bytes", "60000",
                    "--repo", str(self.repo), "--cache", str(self.cache)], env={n: None for n in ENV_DROP})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return [r for r in p.json()["results"] if r["section"] == "lead"]

    def test_embedded_rules_are_not_human_requirements(self):
        self.session(("user", SKILL + "\n\nPlease add the linter config to the build."), ("assistant", "Done."))
        leads = self.investigate("linter", "config")
        self.assertFalse([l for l in leads if "invariant" in l["categories"]])

    def test_quoted_must_never_is_not_a_requirement(self):
        self.session(("user", "The docs say:\n> you must never cache tokens\nCan you check the cache module?"),
                     ("assistant", "Checking."))
        self.assertFalse([l for l in self.investigate("cache") if "invariant" in l["categories"]])

    def test_requirement_after_reference_keeps_span_and_reference_link(self):
        task = "Exports must stay UTC for the billing team."
        message = SKILL + "\n\n" + task
        self.session(("user", message), ("assistant", "Understood."))
        lead = next(l for l in self.investigate("exports") if "invariant" in l["categories"])
        start, end = lead["span"]
        self.assertEqual(" ".join(message[start:end].split()), lead["excerpt"])
        self.assertTrue(lead["segment_id"].startswith("seg:"))
        self.assertEqual(len(lead["relations"]["references"]), 1)
        self.assertNotIn("partial", lead)

    def test_embedded_paths_still_count_for_matching(self):
        self.session(("user", "Fix this:\n```\nTraceback\n  File \"src/loader.py\"\nKeyError: 'name'\n```\n"
                              "We decided to go with a dict default instead."), ("assistant", "OK."))
        leads = self.investigate("unrelated", "--path", "src/loader.py")
        decision = next(l for l in leads if "decision" in l["categories"])
        self.assertIn("path", decision["match_bases"])
        self.assertNotIn("Traceback", decision["excerpt"])


if __name__ == "__main__":
    unittest.main()
