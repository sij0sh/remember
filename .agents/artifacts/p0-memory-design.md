# P0 memory design

Status: proposed for review. No implementation authorized by this record yet.
Existing adopted record: `.agents/design/decisions.md` (D1-D16).
New IDs continue that record; checked rows resolve the proposal, not user approval.

## Goal

`remember` reconstructs evidence agents need to avoid repeated mistakes, preserve
constraints, resume work, and understand architectural choices. Four P0 views:
known pitfalls, critical invariants, current task state, architectural decisions.

Memory = relevant evidence + relationships + uncertainty, not stored conclusions.
The four tools support these views; they are not one tool per category.

## Constraints

- Preserve invocation-local SQLite, lazy providers, one CLI, Python stdlib baseline.
- Read-only on repositories and recordings. Never execute recovered commands.
- No persistent memory/task/decision/metrics database or transcript-derived cache.
- Existing disposable source-only snapshots remain allowed; retain eviction.
- Human requirements, assistant claims, operations and Git facts stay distinct.
- Retrieved instructions are data, not authority for the current task.
- Incomplete evidence must remain visible; no promise of exhaustive recall.

## Grounding

Current extension point: commit `76cd2b79d5f1ce3c9f9d28edff2f54de362f4414`.

| Existing owner | Reuse / limitation |
| --- | --- |
| `scripts/helper/engine.py`, `catalog.py`, `providers.py` | Safe SQLite, dependency planning, transient recordings; extend, do not replace. |
| `runtime.py`, `operations.py`, `delivery.py`, `continuations.py` | One registry, budgets, replay, coverage/delivery separation. |
| `reader.py`, `records.py`, `projection.py` | Raw refs, ancestry, exact call/result pairing; expose stable order and interpretation gaps. |
| `target_history.py`, `session_show.py` | Target matching, turn context, evidence expansion; not multi-turn causal reconstruction. |
| `recall.py`, `commit_links.py` | Commit discovery and witnesses; commit presence must not gate memory discovery. |
| `primer.py` | Proactive recent-work orientation; last assistant claim is not reconciled task state. |
| `edit_hunks.py`, `scripts/profiles/pi-v1.json` | Native operation evidence; no structured Git-tool/checkpoint coverage model yet. |

The supplied exchange is design input, not a current command inventory. Its sample
counts and session-specific findings need original recordings before validation.
Current baseline: 167 tests pass in read-only review; no P0 replay evaluation yet.

## Decisions

### Identity and scope

- [x] D17: Primary job? Four P0 evidence views. Prior-work reuse remains supporting evidence, not the organizing unit. Replaces D2-D4's commit-first identity; preserves useful recall behavior.
- [x] D18 (after: D17): State authority? Current source/Git and original recorded events, with explicit scope and observation time. Summaries/checkpoints are claims with coverage boundaries. No reconstructed conclusion becomes durable authority. Refines D3: commits are durable change evidence, not a prerequisite for memory.
- [x] D19 (after: D17): In scope? Source/Git/session evidence collection, operation normalization, bounded episode reconstruction, P0 discovery, evidence-linked signals. Out: general analytics/reporting products, task management, autonomous fixes, exhaustive shell-effect inference, embeddings, engine-side LLM summarization, new graph-analysis features.

### Tools and simplification

- [x] D20 (after: D17): Main workflow? `investigate GOAL` -> `trace REF` when sequence matters -> `show REF` for exact evidence. `investigate` accepts optional repeated `--path` and `--error`; no goal means bounded repository orientation. All four views are always represented, including explicit unknown/empty states.
- [x] D21 (after: D20): Supporting tools? `activity TARGET` is a chronological operation projection; `signals TARGET` groups evidence-backed patterns. TARGET is required: an exact repository-relative current/historical file path, `path::qualified.name`, or returned file/symbol node ref. Reuse existing target resolution and pinned symbol-mapping bases; ambiguity returns candidates, not guessed history. Unresolved operation paths remain interpretation gaps with evidence refs, not attributed activity. `signals` is later priority, not a prerequisite for P0 retrieval. `trace` is not a synonym for `show`: it assembles context and relationships, not just an exact record.
- [x] D22 (after: D20): Existing surface? Keep `map/find/inspect/query/describe` as supporting/advanced tools, not parallel memory workflows. Reuse recall ranking internally. Keep public `recall` during migration; remove it once investigate passes prior-work parity tests and all in-repo consumers/docs migrate. Mark the removal explicitly in release notes. No replacement CLI or recipe framework. Supersedes D4/D11/D15 where they define the primary workflow.
- [x] D23 (after: D22): Cleanup boundary? Consolidate primer/history discovery into shared evidence retrieval; remove superseded implementations, stale flags, docs and eval criteria only with replacement coverage. Keep source matching, parsers, renames and relationship internals needed by P0 evidence/inspect. No indiscriminate graph deletion.

### Evidence contracts

- [x] D24 (after: D18): Shared derived data? Invocation-local `operations` and `checkpoints` tables, registered through the existing catalog/provider system. Four P0 projections remain recipes over those and existing evidence, not four independent stores or extraction pipelines.
- [x] D25 (after: D24): Operation grain? One individual tool call plus its exactly paired result, never an entire multi-call assistant message. Unpaired calls/results remain separate incomplete operations; never pair by proximity. Identity uses recording identity plus original call-event/item identity (result-event/item identity for orphan results). Path associations may produce multiple rows under the same operation ID; counts use distinct IDs. Unknown tool semantics remain generic operations, not inferred reads/writes. Include recording/event IDs, branch/order, call/result refs, action, path/range, outcome, basis and content refs when known. Distinguish requested/submitted content, observed result, planning snapshot and committed diff. Unknown outcome is not a successful edit. Arbitrary shell effects stay unknown.
- [x] D26 (after: D25): Harness-specific normalization? Profiles/adapters own it. First add fixture-backed Pi `git_plan_context`, `git_commit_plan` and structured checkpoint handling. A planning diff observes a snapshot; it does not identify the command that changed a file. Commit-tool failures never establish commits. Other harnesses retain generic events and explicit unsupported interpretation.
- [x] D27 (after: D24): Checkpoint grain? One summary/checkpoint with publication ref, original structured fields, coverage-end ID, evidence refs and boundary-resolution status. Resolve coverage in the recorded branch, not by timestamp alone. An unresolved boundary cannot establish current state; later observations are reconciled without rewriting the original claim.
- [x] D28 (after: D25, D27): Episode boundary? Start with the selected event's branch-aware human turn plus exact paired operations and witnessed Git links. `trace` orders events within that branch; other branches/sessions remain separate lanes. Widen via explicit previous/next-turn and related-commit continuations. Shared paths, adjacency and lexical similarity create labeled context links, not causal or same-task proof. No inferred global task graph.
- [x] D29 (after: D28): Task state? Reconstruct per-dimension observations: requested, edited, verified, committed, pushed, live-confirmed, blocked/remaining. Each value needs evidence or `unknown`; do not force a linear state machine. Test pass != commit != push != successful live run. Identify the selected turn/session; multiple plausible ongoing tasks remain candidates rather than choosing the newest as the active task.

### Important context the agent did not ask for

- [x] D30 (after: D20, D24): Retrieval lanes? Run both (A) goal/path/error matching across commits AND sessions/operations, and (B) bounded repository-context discovery independent of query words. Lane B looks for explicit constraint/decision leads, failed/corrected attempts and recent unfinished/unverified work. A session-only lesson or old repo-wide constraint must not require a matching commit or keyword.
- [x] D31 (after: D30): Discovery/classification? Deterministic, tested rules: failure outcomes/correction phrasing -> pitfall candidates; explicit requirement language/adopted rules -> invariant candidates; requests/checkpoints/latest operations -> task candidates; option/choice/rationale exchanges or decision records -> decision candidates. Candidates may occupy multiple views. Assistant assertions, repetition and lexical hits alone never establish an accepted constraint, causal diagnosis or accepted decision. First version returns excerpts and refs, not invented canonical lessons or confidence scores.
- [x] D32 (after: D30, D31): Packet contract? Each lead carries category, excerpt, relevance basis (`goal/path/error/repo_context`), evidence kind, source refs, relationship basis, as-of scope, associated correction/conflict refs and expansion ref. Every category states `leads_found`, `none_found_in_scanned_scope`, `not_searched` or `unavailable`; scan/interpretation gaps remain separate. No unsupported claim that all conflicts or superseding decisions were found.
- [x] D33 (after: D32): Ranking and allocation? Deduplicate by evidence identity, not similar prose. First-page order is round-robin across pitfalls/invariants/task/decisions, taking one strongest lead from each nonempty view before second leads. Within each view: explicit scope applicability, then relevance (exact path/error > goal terms > repo context), then direct evidence over inferred associations, then corrections/unresolved outcomes, then recency; stable ref breaks ties. Provide a repo-context lead when available even if goal hits dominate. No undisclosed severity guessing. Small byte caps produce explicit omissions and executable continuations, not silent category loss.
- [x] D34 (after: D20, D32): How does the agent receive this? The skill requires investigate before implementation, when resuming work, and after changed goals or new failures; no flags needed for the repo-context lane. `inspect` includes a compact target-scoped P0 packet before relationship bulk; bare `map` uses a bounded orientation variant instead of an independent last-message primer. Exact `show`, targeted `trace/activity`, raw `query` and `find` do not inject unrelated repo packets. No harness hook in this scope: skill use is the portable trigger, not a guarantee when the skill is never invoked.
- [x] D35 (after: D28, D29, D34): Current session? Separate from archive recall. Use explicit `--session REF` or a validated harness-provided current-session identity when available; freeze its readable frontier at invocation start. Treat it as active-task context, not independent corroboration. Exclude this invocation and recognizable retrieved/copied memory output. If unavailable, show historical resume candidates and say active-task state is unknown. Changed transient inputs invalidate continuations under the existing contract; a fresh invocation refreshes the frontier.

### Scale, freshness and failure

- [x] D36 (after: D24, D30): Ownership/scale? Providers own I/O and budgets; normalizers own evidence interpretation; recipes select/assemble; delivery owns paging. Load each provider once per invocation, sharing discovery and one deadline/input budget across lanes. No full graph build for history-only requests. Expand raw outputs, source and symbol mappings only when needed. No persistent session index in this scope.
- [x] D37 (after: D36): Archive fairness? Keep explicit file/byte/event/time caps and record the actual searched horizon. Within a selected bounded archive, reserve scanning work for repo-context candidates rather than only recent lexical hits. Widening is explicit through coverage continuations. Old unscanned constraints can still be missed: without a durable index, bounded recall cannot guarantee all important context. Measure this tradeoff before adding storage complexity.
- [x] D38 (after: D18, D27, D36): Freshness/failure? Every packet states repository identity, observed HEAD/worktree scope, session frontier and source horizon. Keep historical evidence separate from live status. Checkpoint drift, changed paths, contradictory claims, parse/pairing gaps and unsupported adapters remain visible. Missing Git/recordings leaves the other lanes usable; no evidence means unknown, not safe. Preserve pinned-source replay, transient retention and coverage/delivery separation.
- [x] D39 (after: D18, D30): Repository membership? Preserve current project matching: include recordings attributed to the current repository or explicitly supplied historical roots/root maps. Resolve operation paths against their recorded cwd/root; include external-path activity only as labeled context within a matched recording, never as target-file evidence. Mixed-repository recordings do not make every claim repo-wide: unassignable claims remain scope-unknown and cannot become proactive invariants. Unattributed recordings are omitted with coverage counts. No basename matching or implicit cross-repository widening.

## Implementation order

Each slice must pass its acceptance tests before expanding scope.

1. **Fidelity and shared inputs.** Add ingestion fixtures; fix unknown-outcome edit promotion, request attribution and branch-aware ordering; expose interpretation coverage. Add transient operations/checkpoints and structured-tool adapters. Ship thin `activity` to inspect these facts.
2. **Trace and reconciliation.** Branch-aware chronological episodes, checkpoint coverage gaps, linked Git evidence, per-dimension task observations, deepen/widen refs.
3. **Investigate and proactive packet.** Four views, independent repo-context lane, current-session frontier, fair delivery; reuse commit search and target matching. Route skill/inspect/map through shared packet selection. Preserve old prior-work golden path.
4. **Scope cleanup and signals.** Retire public recall and duplicated primer/history paths after parity. Add only supported signals: failed-exchange groups, correction candidates, checkpoint coverage gaps and repeated-path reads. Repeated reads do not prove waste; errors do not prove reusable pitfalls. Defer failure-after-fix causality and unchanged-content reread metrics until their evidence is available.

## Acceptance cases

- Old invariant with no goal keywords and no associated commit reaches repo-context lane.
- Failed/abandoned uncommitted work appears without a Git witness.
- Assistant proposal remains a proposal; explicit later rejection remains visible.
- Divergent branches and equal/missing timestamps do not create causal links.
- Stale checkpoint followed by test/commit cannot win merely by publication time.
- Submitted write, successful write, planning diff, commit and push stay distinct.
- Active-session context does not corroborate itself through recalled output.
- All four category states survive small output budgets; omitted leads have continuations.
- Missing recordings/Git and unsupported events produce qualified partial results.
- Goal-only history lookup does not build the whole source graph.
- Provider loading is shared, budgets hold, and changed-input replay fails explicitly.
- No transcript/checkpoint/operation payload enters retained cache/reports.
- Existing similar-feature fixture still finds the reusable commit and current paths.

Evaluate with sanitized session replay: hide future events; compare goal-only and
P0+repo-context retrieval for missed constraints, repeated-failure opportunities,
state accuracy, useful rationale and unsupported claims. Track bytes/latency and
archive age coverage; retrieval volume is not success. Add large synthetic archives
(including >100 recordings) to expose scan-order and budget starvation.

## Inputs and review

No unresolved product-policy question blocks the proposed first slice.
Implementation input needed: sanitized real payloads for structured Git tools and
checkpoint variants; synthetic examples alone cannot establish format support.

Review emphasis: identity change (D17), staged public recall retirement (D22),
proactive delivery boundaries (D30-D35), source-cache allowance and bounded-recall
limits. After approval: merge adopted decisions into the existing record, then
implement slice 1 only. No version bump or commit made during design.
