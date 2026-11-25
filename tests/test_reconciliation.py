"""Per-dimension observations and scope-checked checkpoint reconciliation."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import reconciliation


def operation(op_id, action, outcome, order, **extra):
    path = extra.get("path")
    row = {"operation_id": op_id, "recording_id": "rec", "lane": "u1", "branch": "a1",
           "order": order, "timestamp": f"2026-01-01T10:00:0{order}Z", "action": action,
           "outcome": outcome, "basis": "recorded_tool", "tool": "bash", "ref": {"offset": order},
           "path": path, "path_location": "repository" if path else None}
    row.update(extra)
    return row


def witness(witness_id, value, order, **extra):
    row = {"witness_id": witness_id, "relation": "commit", "recording_id": "rec", "lane": "u1",
           "branch": "a1", "order": order, "timestamp": f"2026-01-01T10:01:0{order}Z",
           "confidence": "recorded", "sha": value, "recorded_sha": value,
           "basis": "structured_commit_result", "subject": "Keep lock ordering",
           "object_resolved": "true", "reachable_from_head": "true", "ref": {"offset": order}}
    row.update(extra)
    return row


def checkpoint(cp_id="cp", boundary=5, order=9, status="resolved", **structured):
    return {"checkpoint_id": cp_id, "recording_id": "rec", "lane": "u1", "branch": "a1",
            "order": order, "boundary_order": boundary, "boundary_status": status,
            "publication_ref": {"offset": order},
            "structured": {"version": 1, **structured}}


class ObservationTests(unittest.TestCase):
    def test_dimensions_stay_independent(self):
        ops = [operation("e", "file_edit", "succeeded", 2, path="src/app.py"),
               operation("t", "command", "succeeded", 3, command="python3 -m pytest -q")]
        wits = [witness("w", "a" * 40, 4)]
        rows = reconciliation.observations(None, ops, wits)
        summary = reconciliation.summarize(rows, [])
        self.assertEqual(summary["edited"]["evidence_presence"], "observed")
        self.assertEqual(summary["verified"]["evidence_presence"], "observed")
        self.assertEqual(summary["committed"]["evidence_presence"], "observed")
        self.assertEqual(summary["pushed"]["evidence_presence"], "unknown")
        self.assertEqual(summary["live_confirmed"]["evidence_presence"], "unknown")
        self.assertFalse(any(row.get("value") == "completed" for row in rows))
        self.assertEqual(set(reconciliation.DIMENSIONS), set(summary))

    def test_failed_then_passed_runs_remain_separate(self):
        ops = [operation("t1", "command", "failed", 2, command="pytest -q"),
               operation("t2", "command", "succeeded", 3, command="pytest -q")]
        rows = reconciliation.observations(None, ops, [])
        values = [row["value"] for row in rows if row["dimension"] == "verified"]
        self.assertEqual(values, ["test_run_failed", "test_run_passed"])
        self.assertEqual(len({row["observation_id"] for row in rows}), 2)

    def test_compound_command_keeps_test_and_push_observations_separate(self):
        ops = [operation("c", "command", "failed", 2,
                         command="git push origin main && pytest -q")]
        rows = reconciliation.observations(None, ops, [])
        values = {(row["dimension"], row["value"]) for row in rows}
        self.assertIn(("verified", "test_run_failed"), values)
        self.assertIn(("pushed", "push_failed"), values)

    def test_pushed_claim_compares_with_push_observation(self):
        cp = checkpoint(currentState="Changes are unpushed.")
        observations = reconciliation.observations(
            None, [operation("p", "command", "succeeded", 4, command="git push origin main")], [])
        rows = reconciliation.compare(cp, reconciliation.claims_for(cp), observations)
        pushed = [row for row in rows if row["dimension"] == "pushed"]
        self.assertEqual([row["relation"] for row in pushed], ["contradicts"])

    def test_observation_without_order_is_not_comparable(self):
        cp = checkpoint(currentState="uncommitted")
        observation = witness("w", "a" * 40, 4)
        observation["order"] = None
        rows = reconciliation.compare(cp, reconciliation.claims_for(cp),
                                      reconciliation.observations(None, [], [observation]))
        self.assertEqual([row["relation"] for row in rows], ["not_comparable"])
        self.assertEqual(rows[0]["reason"], "observation_without_order")

    def test_unknown_write_and_planning_snapshot_are_not_confirmed_edits(self):
        ops = [operation("w", "file_edit", "unknown", 2, path="src/app.py"),
               operation("p", "plan_snapshot", "succeeded", 3, basis="planning_snapshot",
                         path="src/other.py")]
        rows = reconciliation.observations(None, ops, [])
        edited = [row["value"] for row in rows if row["dimension"] == "edited"]
        self.assertEqual(edited, ["observed_snapshot"])

    def test_arbitrary_zero_exit_shell_is_not_verification(self):
        ops = [operation("c", "command", "succeeded", 2, command="make build")]
        rows = reconciliation.observations(None, ops, [])
        self.assertFalse([row for row in rows if row["dimension"] == "verified"])

    def test_failed_commit_result_is_not_committed_state(self):
        ops = [operation("e", "file_edit", "succeeded", 2, path="src/app.py")]
        wits = [witness("w", None, 3, confidence="unresolved", failed=True, sha=None, recorded_sha="b" * 40)]
        rows = reconciliation.observations(None, ops, wits)
        committed = [row for row in rows if row["dimension"] == "committed"]
        self.assertEqual([row["value"] for row in committed], ["commit_failed"])
        self.assertEqual(committed[0]["evidence_kind"], "tool_observation")
        self.assertEqual(reconciliation.summarize(rows, [])["committed"]["evidence_presence"], "observed")

    def test_subject_inference_does_not_establish_committed_state(self):
        wits = [witness("w", "a" * 40, 3, confidence="inferred", basis="repo_subject")]
        rows = reconciliation.observations(None, [], wits)
        self.assertEqual(rows[0]["value"], "subject_inferred")
        self.assertEqual(rows[0]["evidence_kind"], "context")


class ClaimTests(unittest.TestCase):
    def test_uncommitted_claim_inside_coverage_contradicts(self):
        cp = checkpoint(currentState="Changes are uncommitted.")
        claims = reconciliation.claims_for(cp)
        observations = reconciliation.observations(None, [], [witness("w", "a" * 40, 4)])
        rows = reconciliation.compare(cp, claims, observations)
        self.assertEqual([row["relation"] for row in rows], ["contradicts"])
        self.assertEqual(rows[0]["reason"], "observation_inside_covered_range")

    def test_commit_after_boundary_is_later_observation_not_contradiction(self):
        cp = checkpoint(boundary=1, currentState="uncommitted")
        observations = reconciliation.observations(None, [], [witness("w", "a" * 40, 4)])
        rows = reconciliation.compare(cp, reconciliation.claims_for(cp), observations)
        self.assertEqual([row["relation"] for row in rows], ["later_observation"])
        self.assertEqual(rows[0]["reason"], "observation_after_coverage_boundary")

    def test_parallel_branch_is_not_comparable(self):
        cp = checkpoint(currentState="uncommitted")
        cp["branch"] = "a-side"
        observations = reconciliation.observations(None, [], [witness("w", "a" * 40, 4)])
        rows = reconciliation.compare(cp, reconciliation.claims_for(cp), observations)
        self.assertEqual([row["relation"] for row in rows], ["not_comparable"])

    def test_unresolved_boundary_never_permits_contradiction(self):
        cp = checkpoint(status="ambiguous", currentState="uncommitted")
        observations = reconciliation.observations(None, [], [witness("w", "a" * 40, 4)])
        rows = reconciliation.compare(cp, reconciliation.claims_for(cp), observations)
        self.assertEqual([row["relation"] for row in rows], ["not_comparable"])
        self.assertEqual(rows[0]["reason"], "unresolved_coverage_boundary")

    def test_claim_extraction_keeps_verbatim_excerpts(self):
        cp = checkpoint(openIssues=["config edit blocked"], nextActions=["run tests"],
                        verificationState="tests passed", constraints=["never edit config"])
        claims = reconciliation.claims_for(cp)
        fields = {claim["field"] for claim in claims}
        self.assertEqual(fields, {"openIssues", "nextActions", "verificationState", "constraints"})
        self.assertTrue(all(claim["value"] == claim["excerpt"] for claim in claims))
        summary = reconciliation.summarize([], claims)
        self.assertEqual(summary["blocked_remaining"]["evidence_presence"], "claimed")
        self.assertEqual(summary["verified"]["evidence_presence"], "claimed")

    def test_unsupported_checkpoint_shape_yields_no_invented_claims(self):
        cp = checkpoint()
        cp["structured"] = ["not", "a", "mapping"]
        self.assertEqual(reconciliation.claims_for(cp), [])


class OutcomeTests(unittest.TestCase):
    """M0: evidence presence and recorded outcome stay separate; failures stay failures."""

    def summary(self, ops=(), wits=()):
        return reconciliation.summarize(reconciliation.observations(None, list(ops), list(wits)), [])

    def test_failed_test_is_observed_but_failed(self):
        verified = self.summary([operation("t", "command", "failed", 2, command="pytest -q")])["verified"]
        self.assertEqual((verified["evidence_presence"], verified["outcome"]), ("observed", "failed"))
        self.assertFalse(reconciliation.settled(verified))

    def test_failed_then_passed_is_mixed_and_latest_passed(self):
        verified = self.summary([operation("t1", "command", "failed", 2, command="pytest"),
                                 operation("t2", "command", "succeeded", 3, command="pytest")])["verified"]
        self.assertEqual(verified["outcome"], "mixed")
        self.assertEqual(verified["latest"]["value"], "test_run_passed")
        self.assertTrue(reconciliation.settled(verified))

    def test_failed_push_is_failed(self):
        pushed = self.summary([operation("p", "command", "failed", 2, command="git push")])["pushed"]
        self.assertEqual(pushed["outcome"], "failed")

    def test_failed_edit_is_an_attempt_not_an_edit(self):
        edited = self.summary([operation("e", "file_edit", "failed", 2, path="a.py")])["edited"]
        self.assertEqual((edited["evidence_presence"], edited["outcome"]), ("observed", "failed"))

    def test_masked_test_exit_is_unattributed(self):
        for command in ("pytest; echo done", "pytest || true", "pytest | tail -3", "if pytest; then :; fi"):
            rows = reconciliation.observations(None, [operation("t", "command", "succeeded", 2,
                                                                command=command)], [])
            verified = [row for row in rows if row["dimension"] == "verified"]
            self.assertEqual([row["value"] for row in verified], ["test_run_unattributed"], command)
            self.assertEqual(verified[0]["outcome"], "unknown")
        for command in ("pytest -q", "cd x && pytest 2>&1", "pytest >log 2>&1"):
            self.assertEqual(reconciliation.test_attribution(command), "attributable", command)

    def test_unverified_full_sha_is_not_a_git_fact(self):
        for resolved in ("unknown", "false"):
            wits = [witness("w", "a" * 40, 3, object_resolved=resolved, reachable_from_head="unknown")]
            rows = reconciliation.observations(None, [], wits)
            self.assertEqual(rows[0]["evidence_kind"], "tool_observation")
            committed = reconciliation.summarize(rows, [])["committed"]
            self.assertEqual((committed["evidence_presence"], committed["outcome"]), ("observed", "unknown"))

    def test_resolved_sha_is_a_git_fact_without_implying_reachability(self):
        wits = [witness("w", "a" * 40, 3, reachable_from_head="false")]
        rows = reconciliation.observations(None, [], wits)
        self.assertEqual(rows[0]["evidence_kind"], "git_fact")
        self.assertEqual(rows[0]["reachable_from_head"], "false")

    def test_requested_and_remaining_have_no_outcome(self):
        summary = self.summary()
        self.assertEqual(summary["requested"]["outcome"], "not_applicable")
        self.assertEqual(summary["verified"]["outcome"], "unknown")


class GitVerifyTests(unittest.TestCase):
    def test_verify_separates_resolution_and_reachability(self):
        import subprocess
        import tempfile
        from helper.git_resolve import GitResolver
        with tempfile.TemporaryDirectory() as temp:
            def git(*args):
                return subprocess.run(["git", "-C", temp, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                                      capture_output=True, text=True, check=True).stdout.strip()
            git("init", "-q")
            git("commit", "-q", "--allow-empty", "-m", "one")
            head = git("rev-parse", "HEAD")
            git("checkout", "-q", "-b", "side")
            git("commit", "-q", "--allow-empty", "-m", "two")
            side = git("rev-parse", "HEAD")
            git("checkout", "-q", "-")
            resolver = GitResolver()
            self.assertEqual(resolver.verify(temp, head), ("true", "true"))
            self.assertEqual(resolver.verify(temp, side), ("true", "false"))
            self.assertEqual(resolver.verify(temp, "f" * 40), ("false", "unknown"))
            self.assertEqual(resolver.verify(None, head), ("unknown", "unknown"))


if __name__ == "__main__":
    unittest.main()
