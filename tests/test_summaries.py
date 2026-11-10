"""Summary occurrences: structured, rendered and native normalization with snapshot refs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import operation_facts, reconciliation, summaries
from helper.options import ScanOptions
from helper.reader import scan_file
from helper.records import RecordCache, new_scope

try:  # The retrieval tests need the shared runtime and its sibling _engine.
    from support import invoke
except ModuleNotFoundError:
    invoke = None


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return str(path)


def checkpoint(checkpoint_id, coverage, structured, parent="m3", order_id="cp"):
    return {"type": "custom", "id": order_id, "parentId": parent,
            "customType": "shadow-compact.checkpoint",
            "data": {"version": 3, "checkpointId": checkpoint_id, "coverage": coverage,
                     "firstKeptEntryId": "m1", "structured": structured}}


PI_BASE = [{"type": "session", "version": 3, "id": "s1", "timestamp": "2026-01-01T10:00:00Z"},
           {"type": "model_change", "id": "m0", "parentId": None, "provider": "p", "modelId": "m"},
           {"type": "message", "id": "m1", "parentId": "m0",
            "message": {"role": "user", "content": [{"type": "text", "text": "Fix tests"}]}},
           {"type": "message", "id": "m2", "parentId": "m1",
            "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash",
                                                         "arguments": {"command": "pytest"}}]}},
           {"type": "message", "id": "m3", "parentId": "m2",
            "message": {"role": "toolResult", "toolCallId": "c1", "toolName": "bash", "isError": False,
                        "content": [{"type": "text", "text": "2 passed"}]}},
           {"type": "message", "id": "m4", "parentId": "m3",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]}}]
STRUCTURED = {
    "objective": [{"text": "Fix tests", "evidenceRefs": ["E0001"]}],
    "completedWork": [{"text": "Ran pytest", "evidenceRefs": ["E0003"]}],
    "verificationState": [{"text": "2 passed", "status": "verified", "evidenceRefs": ["E0004"]},
                          {"text": "Live check", "status": "unverified", "evidenceRefs": ["E0002"]}],
    "modifiedFiles": [{"text": "src/app.py", "evidenceRefs": ["E0002"]}],
    "continuationState": {"currentGoal": "Ship", "currentApproach": "Small steps",
                          "immediateNextStep": "Run live check"},
}
RENDERED = """# Continuation Checkpoint

## Objective
- Fix tests

## Constraints
(none)

## Completed Work
- Ran pytest

## Current State
(none)

## Decisions
(none)

## Open Issues
(none)

## Next Actions
(none)

## Critical Context
(none)

## Modified Files
- src/app.py

## Referenced Files
(none)

## Verification State
- [verified] 2 passed
- [unverified] Live check

## Continuation
- Goal: Ship
- Approach: Small steps
- Next: Run live check
"""


def scan(rows, harness="pi"):
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "session.jsonl"
    write(path, rows)
    recording = scan_file(path, ScanOptions(harness=harness, max_text_bytes=1 << 20), new_scope())
    checkpoints, _ = operation_facts.checkpoints([recording], RecordCache())
    return recording, checkpoints, directory


def build(rows, harness="pi"):
    recording, checkpoints, directory = scan(rows, harness)
    result = summaries.build([recording], RecordCache(), checkpoints)
    return result, directory


class StructuredTests(unittest.TestCase):
    def test_structured_v3_items_status_refs_and_continuation(self):
        rows = PI_BASE + [checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3",
                                            "publishedAtLeafId": "m3"}, STRUCTURED)]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        summary = next(row for row in records if row["physical_kind"] == "checkpoint")
        self.assertEqual(summary["representation"], "structured_json")
        self.assertEqual(summary["producer"], "shadow")
        self.assertEqual(summary["schema_version"], "3")
        self.assertEqual(summary["installation"], "custom_only")
        self.assertEqual(summary["coverage_status"], "resolved")
        self.assertEqual(summary["snapshot_id"].rsplit(":", 1)[-1], "m3")
        self.assertEqual(summary["parse_status"], "complete")
        self.assertEqual(coverage["complete"], True)
        by_text = {item["text"]: item for item in items}
        self.assertEqual(by_text["2 passed"]["status"], "verified")
        self.assertEqual(by_text["Live check"]["status"], "unverified")
        self.assertEqual(by_text["Ship"]["scope"], "continuation")
        self.assertTrue(all(not item.get("labels") for item in items))
        _, checkpoint_rows, extra = scan(PI_BASE + [checkpoint(
            "p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3", "publishedAtLeafId": "m3"},
            STRUCTURED)])
        self.addCleanup(extra.cleanup)
        checkpoint_row = checkpoint_rows[0]
        self.assertEqual(checkpoint_row["version"], "3")
        self.assertEqual(checkpoint_row["checkpoint_key"], "p1")
        self.assertEqual(checkpoint_row["prepared_leaf_id"], "m3")
        resolved = {ref["label"]: ref for ref in refs}
        self.assertEqual(resolved["E0002"]["resolved_id"], "m1")
        self.assertEqual(resolved["E0004"]["resolved_id"], "m3")
        self.assertIn("line", resolved["E0004"]["resolved_ref"])

    def test_missing_snapshot_and_out_of_range_stay_explicit(self):
        rows = PI_BASE + [checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "ghost"},
                                     {"objective": [{"text": "x", "evidenceRefs": ["E0002", "E0099"]}]})]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        statuses = {ref["label"]: ref["status"] for ref in refs}
        self.assertEqual(statuses["E0002"], "missing_snapshot")
        self.assertEqual(statuses["E0099"], "missing_snapshot")
        self.assertEqual(coverage["refs_missing_snapshot"], 2)
        rows = PI_BASE + [checkpoint("p2", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3"},
                                     {"objective": [{"text": "x", "evidenceRefs": ["E0099"]}]},
                                     parent="m4", order_id="cp2")]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        statuses = {ref["label"]: ref["status"] for ref in refs}
        self.assertEqual(statuses["E0099"], "missing_entry")

    def test_prior_checkpoint_label_resolves_to_earlier_occurrence(self):
        first = checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3"}, STRUCTURED)
        second = checkpoint("p2", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3"},
                            {"objective": [{"text": "From prior", "evidenceRefs": ["E0000"]}]},
                            parent="m4", order_id="cp2")
        rows = PI_BASE + [first, second]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        prior = next(ref for ref in refs if ref["label"] == "E0000")
        self.assertEqual(prior["status"], "prior_summary")
        self.assertEqual(prior["resolved_id"], "p1")

    def test_record_ancestry_includes_record_without_a_projected_event(self):
        rows = [PI_BASE[0], PI_BASE[1], PI_BASE[2],
                {"type": "message", "id": "empty", "parentId": "m1", "message": {
            "role": "assistant", "content": [], "stopReason": "error", "errorMessage": "no content"}},
            {"type": "message", "id": "m2", "parentId": "empty", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "after"}]}},
            {"type": "message", "id": "m3", "parentId": "m2", "message": {
                "role": "user", "content": [{"type": "text", "text": "next"}]}},
            checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3"},
                       {"objective": [{"text": "x", "evidenceRefs": ["E0003"]}]})]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        self.assertEqual(refs[0]["status"], "resolved")
        self.assertEqual(refs[0]["resolved_id"], "empty")
        self.assertEqual(coverage["refs_missing_snapshot"], 0)

    def test_malformed_structured_shape_is_unsupported_not_invented(self):
        bad = checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3"}, "not an object")
        (records, items, refs, coverage), directory = build(PI_BASE + [bad])
        self.addCleanup(directory.cleanup)
        summary = records[0]
        self.assertEqual(summary["parse_status"], "unsupported")
        self.assertEqual(items, [])
        self.assertEqual(coverage["opaque"], 0)

    def test_unknown_structured_section_is_preserved(self):
        structured = dict(STRUCTURED)
        structured["futureSection"] = [{"text": "Future claim", "evidenceRefs": ["E0002"]}]
        rows = PI_BASE + [checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3"},
                                     structured)]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        future = next(item for item in items if item["section"] == "futureSection")
        self.assertEqual(future["kind"], "unknown")
        self.assertEqual(future["text"], "Future claim")
        self.assertIn("E0002", {ref["label"] for ref in refs})

    def test_legacy_flat_checkpoint_stays_readable(self):
        legacy = {"type": "shadow-compact.checkpoint", "id": "legacy", "parentId": "m3",
                  "coverageEndEntryId": "m3",
                  "structured": {"version": 1, "objective": "Keep lock order",
                                 "constraints": ["Do not edit config blindly"]}}
        (records, items, refs, coverage), directory = build(PI_BASE + [legacy])
        self.addCleanup(directory.cleanup)
        summary = records[0]
        self.assertEqual(summary["schema_version"], "1")
        self.assertEqual(items[0]["text"], "Keep lock order")
        self.assertIn("Do not edit config blindly", [item["text"] for item in items])


class RenderedTwinTests(unittest.TestCase):
    def test_structured_and_rendered_twin_pair_without_merging_rows(self):
        compaction = {"type": "compaction", "id": "c1", "parentId": "m3", "summary": RENDERED,
                      "firstKeptEntryId": "m2",
                      "details": {"shadowCheckpoint": {"checkpointId": "p1", "retainedAnchor": "m2",
                                                       "coverage": {"coverageEndEntryId": "m3",
                                                                    "preparedAtLeafId": "m3",
                                                                    "publishedAtLeafId": "m3"}}}}
        rows = PI_BASE + [checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3",
                                             "publishedAtLeafId": "m3"}, STRUCTURED), compaction]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        self.assertEqual(len(records), 2)
        self.assertEqual({row["twin_status"] for row in records}, {"paired_consistent"})
        custom = next(row for row in records if row["physical_kind"] == "checkpoint")
        self.assertEqual(custom["installation"], "canonical_compaction")
        self.assertEqual(custom["lifecycle_basis"], "matching_recorded_compaction_twin")
        rendered = next(row for row in records if row["representation"] == "rendered_markdown")
        self.assertEqual(rendered["producer"], "shadow")
        self.assertEqual(rendered["installation"], "canonical_compaction")
        self.assertEqual(rendered["citation_availability"], "none")
        rendered_items = [item for item in items if item["summary_id"] == rendered["summary_id"]]
        self.assertEqual({item["text"] for item in rendered_items},
                         {"Fix tests", "Ran pytest", "2 passed", "Live check", "src/app.py",
                          "Ship", "Small steps", "Run live check"})
        self.assertEqual([item["status"] for item in rendered_items if item["text"] == "Live check"],
                         ["unverified"])

    def test_rendered_without_structured_twin_is_still_available(self):
        compaction = {"type": "compaction", "id": "c1", "parentId": "m3", "summary": RENDERED,
                      "details": {"shadowCheckpoint": {"checkpointId": "only", "retainedAnchor": "m2",
                                                       "coverage": {"coverageEndEntryId": "m3",
                                                                    "preparedAtLeafId": "m3"}}}}
        (records, items, refs, coverage), directory = build(PI_BASE + [compaction])
        self.addCleanup(directory.cleanup)
        self.assertEqual(records[0]["twin_status"], "only")
        self.assertEqual(records[0]["installation"], "canonical_compaction")
        self.assertTrue(items)

    def test_same_producer_id_with_different_text_stays_visible(self):
        changed = RENDERED.replace("- Fix tests", "- Different claim")
        compaction = {"type": "compaction", "id": "c1", "parentId": "m3", "summary": changed,
                      "details": {"shadowCheckpoint": {"checkpointId": "p1", "coverage": {
                          "coverageEndEntryId": "m3", "preparedAtLeafId": "m3"}}}}
        rows = PI_BASE + [checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3",
                                             "publishedAtLeafId": "m3"}, STRUCTURED), compaction]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        self.assertEqual({row["twin_status"] for row in records}, {"paired_mismatch"})
        self.assertEqual(len(records), 2)
        self.assertEqual(coverage["complete"], False)


class NativeTests(unittest.TestCase):
    def test_pi_native_layout_keeps_split_turn_and_statuses(self):
        text = """## Goal
- Old goal

## Constraints & Preferences
- (none)

## Progress
### Done
- [x] Done thing
### In Progress
- [ ] Doing thing
### Blocked
- [ ] Blocked thing

## Key Decisions
- **Fetch first**: a missing ref caused a false compare

## Next Steps
1. Do the next thing

## Critical Context
- Context line

## Verification State
- [unverified] Live behavior not checked

## Modified Files
- src/x.py

---

## Original Request
- New ask

## Progress So Far
- Split progress

## Context Needed to Continue
- Need this path

```
## Fenced must not become a heading
```

<read-files>
/home/u/a.py
</read-files>"""
        rows = [{"type": "session", "version": 3, "id": "s", "timestamp": "2026-01-01T10:00:00Z"},
                {"type": "compaction", "id": "n1", "parentId": None, "summary": text}]
        (records, items, refs, coverage), directory = build(rows)
        self.addCleanup(directory.cleanup)
        summary = records[0]
        self.assertEqual(summary["representation"], "native_markdown")
        self.assertEqual(summary["producer"], "harness_native")
        self.assertEqual(summary["coverage_status"], "unknown")
        self.assertEqual(summary["parse_status"], "complete")  # fenced headings stay content
        by = {(item["scope"], item["section"], item["text"]): item for item in items}
        self.assertEqual(by[("history", "Progress", "Done thing")]["status"], "done")
        self.assertEqual(by[("history", "Progress", "Doing thing")]["status"], "in_progress")
        self.assertEqual(by[("history", "Progress", "Blocked thing")]["status"], "blocked")
        self.assertEqual(by[("history", "verificationState", "Live behavior not checked")]["status"], "unverified")
        self.assertIn(("turn_prefix", "Original Request", "New ask"), by)
        self.assertIn(("history", "Goal", "Old goal"), by)
        self.assertIn(("history", "readFiles", "/home/u/a.py"), by)
        self.assertEqual(by[("history", "readFiles", "/home/u/a.py")]["scope"], "history")
        self.assertNotIn("(none)", [item["text"] for item in items])
        self.assertIn("```\n## Fenced must not become a heading\n```", [item["text"] for item in items])

    def test_claude_standard_sections_boundary_pairing_and_wrapper(self):
        body = """This session is being continued from a previous conversation that ran out of context.

<system-reminder>
<tools-used-before-summary>Bash</tools-used-before-summary>
</system-reminder>
1. **Primary Request and Intent**
- Implement the thing

2. Key Technical Concepts
- Concept

3. Errors and fixes
- Error and fix

4. Problem Solving
- Solved

5. All user messages
- "do it"

6. Pending Tasks
- None

7. Current Work
- Presenting

8. Optional Next Step
- Present the report

If you need specific details from before compaction, read the transcript.

The messages after this summary are kept verbatim."""
        rows = [
            {"type": "system", "subtype": "compact_boundary", "uuid": "b1", "parentUuid": None,
             "logicalParentUuid": "h1", "sessionId": "s", "timestamp": "2026-01-01T10:00:00Z",
             "content": "Conversation compacted",
             "compactMetadata": {"trigger": "auto", "preTokens": 10,
                                 "preservedSegment": {"headUuid": "h2", "anchorUuid": "a1", "tailUuid": "t1"},
                                 "preservedMessages": {"uuids": ["h2", "t1"]}}},
            {"type": "user", "isCompactSummary": True, "uuid": "a1", "parentUuid": "b1",
             "sessionId": "s", "timestamp": "2026-01-01T10:00:01Z",
             "message": {"role": "user", "content": body}}]
        (records, items, refs, coverage), directory = build(rows, "claude-code")
        self.addCleanup(directory.cleanup)
        boundary = next(row for row in records if row["physical_kind"] == "compact_boundary")
        summary = next(row for row in records if row["physical_kind"] == "compact_summary")
        self.assertEqual(boundary["installation"], "canonical_compaction")
        self.assertEqual(summary["installation"], "canonical_compaction")
        self.assertEqual(summary["coverage_status"], "metadata_only")
        self.assertEqual(summary["first_kept_id"], "h2")
        self.assertEqual(summary["retained_ids"], ["h2", "t1"])
        self.assertEqual(summary["parse_status"], "complete")
        by = {item["text"]: item for item in items}
        self.assertEqual(by["Implement the thing"]["kind"], "objective")
        error = next(text for text in by if text.startswith("Error and fix"))
        self.assertEqual(by[error]["kind"], "pitfall")
        self.assertEqual(by['"do it"']["kind"], "reported_user_message")
        self.assertEqual(by["Present the report"]["kind"], "next_action")
        self.assertEqual(by["This session is being continued from a previous conversation that ran out of context."]["scope"],
                         "wrapper")
        self.assertIn("footer", {item["section"] for item in items})


class ClaimTests(unittest.TestCase):
    def test_claims_use_exact_item_text_not_serialized_json_and_keep_labels(self):
        checkpoint_row = {"checkpoint_id": "cp", "publication_ref": {"file": "x", "offset": 0,
                                                                      "length": 1, "sha256": "a"},
                          "structured": STRUCTURED}
        claims = reconciliation.claims_for(checkpoint_row)
        objective = next(claim for claim in claims if claim["field"] == "objective")
        self.assertEqual(objective["value"], "Fix tests")
        self.assertEqual(objective["labels"], ["E0001"])
        continuation = next(claim for claim in claims if claim["field"] == "continuationState.immediateNextStep")
        self.assertEqual(continuation["value"], "Run live check")
        self.assertEqual(continuation["dimension"], "blocked_remaining")
        verified = next(claim for claim in claims if claim["value"] == "2 passed")
        self.assertEqual(verified["claimed_status"], "verified")


@unittest.skipIf(invoke is None, "runtime/_engine unavailable in this checkout")
class RetrievalTests(unittest.TestCase):
    def repository(self):
        import subprocess
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name) / "repo"
        repo.mkdir()
        (repo / "a.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e", "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e",
                        "commit", "-qm", "init"], check=True)
        archive = Path(directory.name) / "recordings"
        archive.mkdir()
        return repo, archive

    def investigate(self, repo, archive, goal="Fix tests"):
        env = {name: None for name in ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID",
                                       "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")}
        return invoke(["investigate", goal, "--repo", str(repo), "--recordings", str(archive)],
                      env=env).json()

    def test_checkpoint_items_feed_one_attributed_task_lead(self):
        repo, archive = self.repository()
        rows = json.loads(json.dumps(PI_BASE))
        rows[0]["cwd"] = str(repo)
        rows.append(checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3",
                                      "publishedAtLeafId": "m3"}, STRUCTURED))
        write(archive / "s.jsonl", rows)
        output = self.investigate(repo, archive)
        task = [row for row in output["results"] if "task" in row.get("categories", [])]
        self.assertEqual(len(task), 1)
        self.assertEqual(task[0]["kinds"], ["checkpoint_claim"])
        self.assertIn("Fix tests", task[0]["excerpt"])
        self.assertIn("summary_id", task[0])

    def test_summary_tables_are_queryable_without_promoting_claims(self):
        repo, archive = self.repository()
        rows = json.loads(json.dumps(PI_BASE))
        rows[0]["cwd"] = str(repo)
        rows.append(checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3",
                                      "publishedAtLeafId": "m3"}, STRUCTURED))
        write(archive / "s.jsonl", rows)
        env = {name: None for name in ("PI_SESSION_FILE", "PI_SESSION_ID", "CODEX_THREAD_ID",
                                       "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")}
        output = invoke(["query", "SELECT section, status, text FROM summary_items WHERE section='verificationState' "
                                   "ORDER BY ordinal", "--repo", str(repo), "--recordings", str(archive)],
                        env=env).json()
        self.assertEqual([(row["status"], row["text"]) for row in output["results"]],
                         [("verified", "2 passed"), ("unverified", "Live check")])
        self.assertEqual(output["coverage"]["summaries"]["refs_resolved"], 5)

    def test_rendered_only_summary_feeds_task_view_but_a_twin_does_not_duplicate(self):
        repo, archive = self.repository()
        rows = json.loads(json.dumps(PI_BASE))
        rows[0]["cwd"] = str(repo)
        rows.append({"type": "compaction", "id": "c1", "parentId": "m3", "summary": RENDERED,
                     "details": {"shadowCheckpoint": {"checkpointId": "only", "coverage": {
                         "coverageEndEntryId": "m3", "preparedAtLeafId": "m3"}}}})
        write(archive / "s.jsonl", rows)
        task = [row for row in self.investigate(repo, archive)["results"] if "task" in row.get("categories", [])]
        self.assertEqual(len(task), 1)
        self.assertEqual(task[0]["kinds"], ["summary_claim"])
        self.assertEqual(task[0]["installation"], "canonical_compaction")

        rows = json.loads(json.dumps(PI_BASE))
        rows[0]["cwd"] = str(repo)
        rows.append(checkpoint("p1", {"coverageEndEntryId": "m3", "preparedAtLeafId": "m3",
                                      "publishedAtLeafId": "m3"}, STRUCTURED))
        rows.append({"type": "compaction", "id": "c1", "parentId": "m3", "summary": RENDERED,
                     "details": {"shadowCheckpoint": {"checkpointId": "p1", "coverage": {
                         "coverageEndEntryId": "m3", "preparedAtLeafId": "m3"}}}})
        write(archive / "s.jsonl", rows)
        task = [row for row in self.investigate(repo, archive)["results"] if "task" in row.get("categories", [])]
        self.assertEqual([row["kinds"] for row in task], [["checkpoint_claim"]])


if __name__ == "__main__":
    unittest.main()
