# P0 slice 1: fidelity and shared inputs

Status: implementation plan for review; no implementation authorized or performed.
Grounded at `117a984` (code baseline `76cd2b7`).
Inputs: `memory-P0.md`, `p0-memory-design.md`, adopted D1-D16.
Implements proposed D24-D27 plus the fidelity/coverage portions of D21, D28,
D36-D39. Does not adopt the remaining P0 proposal implicitly.

## Outcome

Recorded requests, attempted operations, observed outcomes and summary coverage
remain distinguishable. One transient projection supplies SQL and thin
`activity TARGET`; neither creates task conclusions or persistent memory.

Success: every displayed fact expands to original evidence; unsupported, missing
and contradictory inputs cannot silently become successful edits or commits.

## Scope

- Ingestion regression fixtures; exact pairing, request attribution, branch order.
- Interpretation coverage separate from scan coverage and output delivery.
- Invocation-local `operations` and `checkpoints` tables.
- Fixture-backed Pi native tools, `git_plan_context`, `git_commit_plan`, structured
  checkpoints; other tools remain generic operations.
- Thin chronological `activity TARGET`, including historical paths and symbols.

Exclude `trace`, checkpoint/task-state reconciliation, `investigate`, proactive
packets, `signals`, recall retirement, arbitrary shell-effect inference and new
persistent caches. No harness changes. Preserve existing recall/inspect behavior
except correctness fixes.

## Grounded problems / owners

| Owner | Required change |
| --- | --- |
| `scripts/helper/edit_hunks.py:derive` | Currently skips failed, not unknown outcomes; proposed content can become historical hunks/symbols. |
| `reader.py:scan_file` | Every user-shaped message advances the turn; injected context can replace the human request. Ancestry resolves in one forward pass. |
| `reader.py`, `records.py` | Head/tail ordering uses different units; preserve physical position separately from ancestry. |
| `profiles.py:interpret`, `records.py:new_scope` | Matching rules with missing/malformed collections can silently emit nothing. Scan success is not interpretation success. |
| `target_history.py`, `projection.py`, `session_show.py` | Reuse corrected request/ancestry facts; do not infer a correction from a sibling branch. |
| `commit_links.py` | Structured commit results need original witnesses, not fabricated shell commands. |
| `catalog.py`, `providers.py` | Register shared transient tables; scan recordings once. |
| `operations.py`, `runtime.py`, `delivery.py`, `continuations.py` | Register/present/replay activity through existing command infrastructure. |

Paths above are under `scripts/helper/` unless stated otherwise.

## Slice decisions

Local IDs below do not renumber or adopt the proposed global decisions.

- [x] F1: Fact owner? Reader owns recording identity, raw refs, exact pairing and
  ancestry/request attribution. Normalizers own tool semantics. Providers own
  I/O/budgets. Recipes select rows; delivery pages them.
- [x] F2 (after: F1): Identity? Operation ID derives from recording identity plus
  original call-event/item identity; orphan results use result-event/item identity.
  Never use toolCallId or session ID alone as the primary key. Preserve native IDs.
- [x] F3 (after: F2): Pairing? Exact recorded call ID with unambiguous compatible
  ancestry. Never proximity. Duplicate IDs, conflicting results, unresolved
  ancestry and missing halves retain refs and explicit pairing status.
- [x] F4 (after: F1): Human attribution? Track nearest supported human request on
  the same ancestry separately from conversational turn and origin. Harness
  injection never replaces it. Delegated requests stay delegated; parent-session
  metadata alone does not prove a message is human. Ambiguity stays unknown.
- [x] F5 (after: F4): Order? Preserve `(byte_offset, item_index)` physical order;
  resolve the complete bounded ancestry graph before attribution. Ancestors precede
  descendants within a branch. Siblings are separate lanes; timestamps are labels,
  not causal ordering. Explicit recorded turn identity survives missing ancestry
  but does not prove request lineage. Conflicts/cycles are gaps, not repaired links.
- [x] F6 (after: F3): Outcomes? `succeeded`, `failed`, `unknown` describe the recorded
  operation result. Missing/malformed result means unknown. Only supported success
  can establish an edit; attempted content remains inspectable but cannot supply
  confirmed hunks, historical source reconstruction or `at_edit` symbols.
- [x] F7 (after: F6): Evidence kinds? Separate submitted content, observed output,
  planning snapshot and committed diff. A successful planning tool observes a
  snapshot, not the mutation that produced it. Commit is not push or live success.
- [x] F8 (after: F1): Coverage? Report scan, interpretation and delivery independently.
  Count records/items considered, interpreted, intentionally ignored/empty,
  unsupported, malformed and unresolved; provide bounded gap refs by reason.
  Unknown semantics remain generic operations, not disappeared events.
- [x] F9 (after: F1): Adapter mechanism? Extend profiles with explicit adapter IDs
  backed by small built-in Python functions. No plugin framework, tool-name regex
  guesses, arbitrary evaluation or execution of recovered commands.
- [x] F10 (after: F5): Checkpoints? Preserve original claims and coverage boundaries.
  Resolve boundary only on supported ancestry. Publication time cannot substitute
  for covered time. This slice exposes boundaries; later slice reconciles state.
- [x] F11 (after: F2, F7): Row grain? One row per operation/path association, with a
  stable association key; pathless operations retain one null-path row. Counts use
  distinct operation IDs. Checkpoints have one row per original checkpoint item.
- [x] F12 (after: F8): Activity? Select shared facts only. No fresh scanner, duplicate
  interpretation, episode inference or implicit repo-wide packet.

## Work plan and gates

### 1. Establish fixtures before behavior changes

Add focused tests (`tests/test_ingestion.py`, `test_operation_facts.py`,
`test_checkpoints.py`, `test_activity.py`) and sanitized fixtures under
`tests/fixtures/recordings/`. Keep existing inline fixtures where appropriate.

- Cover supported Pi, Claude Code and Codex shapes; native reads/writes/edits,
  multiple calls in one message, exact results, failures, missing halves,
  duplicated IDs, orphan results and unknown tools.
- Cover injected context, delegated messages, repeated identical human requests,
  interleaved forks, child-before-parent, missing parents, cycles and equal/missing
  timestamps. Include Pi's chained parallel results.
- Cover empty/malformed content arrays, unknown nested items, truncated JSONL,
  head/tail scans and truncated patches.
- Add sanitized real Pi structured Git/checkpoint payloads, preserving field
  shapes, ancestry and call/result relationships. Store expected projections and
  source provenance without private transcript content.

Gate: new regression tests fail for the intended reasons; existing baseline is
recorded. Synthetic fixtures test defensive behavior but do not prove real format
support. Do not claim adapter support until a sanitized real sample passes.

### 2. Repair shared ingestion fidelity

Change `reader.py`, `records.py`, `projection.py`, `profiles.py` first.

- Separate physical position, ancestry, turn identity and human-request ref/basis.
  Resolve IDs from the full bounded scan with cycle detection; absent scanned
  parents remain unresolved. Do not join omitted head/tail ranges by proximity.
- Keep exact pairing conservative; preserve evidence for rejected/ambiguous pairs.
- Require identity/ancestry-compatible deduplication; identical text on another
  turn/branch is not an echo.
- Make ignored/empty versus invalid/unsupported interpretation explicit.
- Bound and account for tail-line alignment reads; sampled scans must not evade
  byte limits through an unbounded skipped line.
- Update request and correction consumers (`target_history`, `session_show`,
  `primer`, `recall`, commit-link attribution) to use shared facts rather than
  independent nearest-message heuristics.

Then fix `edit_hunks.py`: unknown attempts stay evidence, never confirmed edits.
Validate declared unified-hunk lengths before reconstruction; truncated diffs
cannot produce pinned historical symbols. Expose extraction gaps without changing
raw source evidence.

Gate: branches cannot borrow each other's requests/results/corrections; unknown
writes cannot establish changed-file content; head/tail positions are comparable;
existing straightforward human-turn and successful-edit tests still pass.

### 3. Add shared transient contracts

Use a distinctly named module, e.g. `operation_facts.py`; existing `operations.py`
is the CLI registry. Avoid importing recipes into providers.

Register `operations` and `checkpoints` in `catalog.py`; add provider dependencies
and loaders in `providers.py`. Depend on recordings; load Git links only when
needed, never the whole source graph for path-only activity.

Minimum operation fields:

- Identity: `operation_id`, association key, `recording_id`, `session`, native
  call ID, call/result refs, pairing status.
- Context: event IDs, parent ID, branch/lane reference, physical position,
  branch-local order, ancestry status, timestamp, turn, request ref/basis, origin.
- Semantics: tool, action, outcome, exit code when known, interpretation status,
  adapter/basis.
- Evidence: normalized path and raw path/location, requested or observed range
  with range basis and submitted/observed content refs. Commit witnesses join
  through `commit_links`, not a scalar commit SHA on the operation. Unknown fields remain null. Hash only exact available content;
  do not present a partial read hash as a whole-file version.

Minimum checkpoint fields:

- Checkpoint ID, recording/session, publication event/ref and branch position.
- Original structured fields and evidence refs, original coverage-end ID.
- Resolved boundary ref/position, status (`resolved`, `missing`, `ambiguous`,
  `off_branch`, `invalid`), adapter/basis. Preserve dangling evidence refs.

Document SQL grain/keys/types through `describe`; preserve `recording_id` joins.
Keep raw payloads behind refs where possible. Structured checkpoint JSON is
invocation-local only. Path associations respect recorded cwd/root maps; external
or unresolved paths never become repository-target matches.

Gate: SQL selects both tables; multi-path tools do not inflate operation counts;
unknown tools and orphan results remain queryable; loading both tables scans each
recording once; no transcript-bearing retained output or cache is written.

### 4. Implement fixture-backed structured adapters

Update `scripts/profiles/pi-v1.json` and normalizers. Read decoded original
payloads via the record cache, not flattened display text.

- `git_plan_context`: project observed changed paths/status/diffs as planning
  snapshot associations. A snapshot cannot establish which shell call edited a
  file or when an intermediate change occurred.
- `git_commit_plan`: accept explicit successful result witnesses; validate SHAs
  through existing Git resolution where available. Expose recorded evidence and
  validation status separately when Git is absent. Failed, missing or ambiguous
  results never establish a successful commit. Do not infer success from inputs.
- Feed structured witnesses into `commit_links.py` with original refs and basis;
  retain existing shell witness behavior and distinguish inferred links.
- Structured checkpoint variants: retain claims, publication identity and coverage
  boundary; report unsupported versions/shapes rather than partial success guesses.

Gate: success/failure/unknown/malformed variants pass; planning diffs, native
edits and committed changes remain distinguishable in SQL and evidence expansion.

### 5. Ship thin `activity TARGET`

- Accept exact current/historical repository-relative path,
  `path::qualified.name`, or returned file/symbol node ref. Reuse target resolution;
  ambiguity returns candidates. Do not basename-match or widen repositories.
- File activity selects associated operation facts. Symbol activity uses supported
  pinned mappings; unmappable changes are coverage gaps, not guessed symbol hits.
- Default rows: operation ID, lane/order, timestamp, request ref, action, path/range,
  outcome, basis, commit witness when available, call/result expansion refs.
  Group branch lanes deterministically; chronological only within each lane.
- Report unresolved/external path and unsupported interpretation counts separately.
  No matching rows means none found within scanned scope, not no historical work.
- Register parser/normalization/budget flags and continuation allowlists. Reuse
  standard delivery, limits, table/JSON formats and coverage widening.
- Fingerprint all relevant scanned inputs, not just visible rows. Changed recordings
  invalidate paging; output trimming does not rewrite provider completeness.
- Add concise command/reference documentation when implementing; do not migrate
  the skill to investigate or remove recall in this slice.

Gate: path activity works without source graph build; historical missing files
remain valid targets; every row expands through `show`; small-byte paging, changed
input rejection, deep coverage widening and transient retention checks pass.

## Verification / completion checklist

- [ ] Full suite: `python3 -m unittest discover -s tests`.
- [ ] Focused fixture assertions cover all regressions and new contracts above.
- [ ] Native plus structured facts agree across SQL, activity and existing history
  consumers; a failed/unknown write never becomes a confirmed edit elsewhere.
- [ ] Stale checkpoint publication cannot move its resolved coverage boundary.
- [ ] Missing Git/recordings, unsupported adapters and sampled ancestry produce
  usable qualified output, not false success or unrelated fallback evidence.
- [ ] Shared providers load once; deadlines, byte/event/row caps include derivation
  and multi-path expansion. Large synthetic archive (>100 recordings) reports the
  actual bounded horizon rather than implying exhaustive coverage.
- [ ] Replay tokens contain refs/arguments/fingerprints, never transcript payloads;
  no new retained transcript/checkpoint/operation artifact.
- [ ] Existing recall golden path and inspect/request evidence tests remain green.

## Critical review: joins and remaining-slice contracts

Proposed amendments for review; no implementation authorized. These refine F2,
F7, F11 and the handoff to slices 2-4, not expand slice 1 into investigation.

### Gaps in current code

- `recall.py:requests` groups by native session, chooses one prior request and
  retains one request per SHA. Recording/branch identity and multiple contexts
  are lost; `rank` only returns commits. Session scanning != session recall.
- `target_history.py:compact` groups commits by SHA and keeps one `recorded_in`;
  turn grouping uses session/turn rather than recording/branch identity.
- `catalog.py` has composite commit keys and recording keys, but no witness key.
  Operation/path rows with one optional SHA cannot express all commit relations.
- `primer.py` uses first request/last reply heuristics and drops session material
  before commits under budget pressure. Long/final replies are not decisions.
- Existing D30-D39 set the right direction but lack measurable retrieval,
  eligibility and cross-session presentation gates.

### Proposed decisions

- [x] R1: Join identities? Events/turns use recording plus original event/branch
  identity; native session IDs are metadata. Commits use `(repo_id, sha)`.
  Preserve recorded cwd/worktree scope: shared repository identity does not prove
  two sessions observed the same working state.
- [x] R2 (after: R1): Witness cardinality? Extend transient `commit_links` with
  stable `witness_id`, optional `operation_id`, relation/basis, original refs,
  recorded/resolved SHA and separate repository-resolution/validation status.
  One operation may witness multiple commits; one commit may have many witnesses.
  IDs derive from source event/context ordinal, not a possibly unresolved SHA.
  Keep operation/path and witness relations separate; aggregate each before joins
  to avoid path x witness multiplication. Count distinct operations/recordings.
- [x] R3 (after: R2): Several sessions before one commit? Retrieve all eligible
  related session lanes within scanned scope, not only the committing session.
  Distinguish `commit_witness`, `related_context`, and supported content linkage.
  Related context can use explicit references or scoped path/content matches,
  carrying its basis; chronology alone is insufficient. Matching a path does not
  prove contribution, and matching content does not prove authorship.
  Keep weaker candidates expandable, not silently promoted to contributors.
- [x] R4 (after: R3): Relation owner? Slice 1 preserves witnesses and raw facts.
  Slice 2 builds transient related-context projections from existing evidence;
  no global task graph or new persistent association store. Each relation carries
  commit identity, recording/event identity, evidence refs and basis. Keep request
  and decision-only context eligible even without an edit. Preserve multiplicity
  through compact views with bounded excerpts, omission counts and continuation.
- [x] R5: Independent recall? Operations, requests and checkpoints are available
  with empty commits/links or unavailable Git. Legacy public recall may remain
  commit-oriented until migration; new shared retrieval must not inherit that gate.
- [x] R6 (after: R5): Frequency? Slice 3 searches bounded session and commit lanes
  on every ordinary investigate, including when commit hits are strong. Skill
  triggers: before implementation, resume, changed goal, new failure. Search often;
  show only eligible useful leads. Exact evidence tools remain uninjected.
  No harness guarantee if the skill is not invoked.
- [x] R7 (after: R6): High signal? Scope/provenance eligibility precedes ranking.
  Explicit requirements, supported choice/rationale exchanges, failed/corrected
  attempts and unresolved claims qualify as leads, not established conclusions.
  Acknowledgements, wrappers, copied packets, generic progress and routine reads
  do not independently qualify for proactive delivery; remain in explicit history.
  Short constraints survive; verbose/final assistant text gains no special status.
  Filter wrappers in displayed excerpts as well as selection. Repetition is not
  independent support. Never fill empty categories with weak evidence.
- [x] R8 (after: R7): Fairness? Reserve bounded scan work for older/repo-context
  evidence and output room for eligible session-only leads. Diversify first-page
  excerpts across recordings/episodes without merging distinct identical requests.
  Disclose selected horizon and eligible overflow; continuations expose the rest.
  Do not claim all relevant sessions were found in a bounded archive.

### Join contract / expected example

`events -> sessions USING(recording_id)`; operation facts retain that identity.
`commit_links -> commits ON repo_id AND sha`; witnesses retain event/operation refs.
Related-context projections are separate many-to-many joins, not fabricated
commit witnesses. Null/unresolved joins retain original evidence via left joins.
Do not join recordings to commits by bare session ID, timestamp or SHA alone.

Example: A records a requirement, B implements/revises it, C commits. A commit
result can show A's requirement, B's observed edits and C's witness in separate
lanes, each with its association basis. Only supported evidence establishes that
B's content is present in the commit; unrelated earlier session D is excluded.
Missing contribution proof must not hide relevant A/B context. Missing context
proof must not label every earlier session as related.

### Slice gates and ownership

1. **Fidelity:** fix identity/multiplicity losses in existing consumers; add witness
   keys/status and independent-input tests. Preserve recording/cwd/content refs
   needed later. Distinguish scan, interpretation and delivery coverage.
2. **Trace:** start from requests/failures/checkpoints without commits; expose
   multi-session commit context with labeled noncausal relations. Reconcile only
   supported branch evidence; assistant reports remain claims. Explicitly test
   compound shell failure after a successful commit: overall failure does not
   erase a directly observed commit or imply all suboperations succeeded.
3. **Investigate:** enforce R6-R8 through shared selection for skill/inspect/map.
   Replace primer's implicit ancestor-root fallback with explicit historical roots.
   Report discovered/scanned/interpretable/eligible/selected/delivered separately;
   suppression reasons belong to selection, not scan incompleteness.
4. **Cleanup/signals:** retire old paths only after session-only and multi-session
   parity, not merely reusable-commit parity. Signals cannot turn repetition or
   context associations into causality.

### Additional acceptance / evaluation

- [ ] A/B/C example returns every eligible lane; unrelated D excluded; bounded
  omissions visible. Several witnesses/requests never collapse into one intent.
- [ ] Reused native session/turn IDs, sibling branches, multiple worktrees and
  identical SHAs across repository lanes cannot cross-attribute requests.
- [ ] One operation/multiple commits and multiple paths/witnesses do not multiply
  operation or independent-session counts. Unknown links remain inspectable.
- [ ] Session-only invariant/failure/decision lead survives no Git and strong commit
  hits; no-commit trace works without loading commit discovery.
- [ ] >100-recording noise archive: older scoped constraint in the allocated scan
  survives recent progress chatter; all-noise archive yields qualified empty views.
- [ ] Short prohibition beats verbose generic status; copied memory/current
  invocation cannot corroborate itself; sibling correction cannot supersede it.
- [ ] Tiny budgets preserve category states and eligible-session continuation.
- [ ] Future-hidden labeled replay compares baseline/candidate under equal budgets:
  session-search execution rate at eligible triggers; session-only recall@budget;
  first-page precision; low-signal byte fraction; single-session dominance;
  scanned age/horizon, latency and omitted eligible leads. No persistent metrics DB.
  Deterministic fixtures require zero false cross-branch links/accepted decisions.
  Choose numeric quality targets after labeling replay cases; volume is not success.

Open implementation input remains real structured-tool/checkpoint samples. Real
usage frequency and retrieval quality are not established by current unit tests.

## Inputs, risks, handoff

Open input: sanitized real `git_plan_context`, `git_commit_plan` and checkpoint
variants. Obtain explicit sample sources before claiming format support; generic
operations and ingestion work can proceed independently.

Main risks: attribution changes affecting ranking, partial scans losing ancestry,
row expansion exhausting budgets, and snapshot diffs being mistaken for edits.
Fixtures and explicit basis/coverage are release gates, not optional diagnostics.

Next: review this plan, obtain samples, implement steps 1-2 first. No product-policy
question otherwise blocks slice 1. Plan committed on request; version incremented
from 0.4.7 to 0.4.8 per project rule. Implementation remains pending review.
