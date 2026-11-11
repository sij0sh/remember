For an AI coding harness, lack of memory mainly causes repeated discovery, repeated mistakes, and loss of task continuity.

The most valuable memories are things the agent cannot cheaply rediscover from the codebase.

## 1. Problems caused by poor memory

| Problem                  | What happens                                                        |
| ------------------------ | ------------------------------------------------------------------- |
| Repeated exploration     | Agent repeatedly searches the same files to understand architecture |
| Repeated failures        | Retries approaches that previously failed                           |
| Lost decisions           | Reverses architectural choices without knowing why they were made   |
| Convention violations    | Ignores project-specific patterns and preferences                   |
| Task discontinuity       | Forgets progress, blockers, and next steps between sessions         |
| Duplicate implementation | Builds functionality that already exists                            |
| Broken assumptions       | Forgets subtle dependencies and hidden invariants                   |
| Inefficient testing      | Repeats expensive tests or overlooks known failing tests            |
| Context drift            | Long tasks lose their original requirements and constraints         |
| Environment confusion    | Forgets setup steps, tool limitations, or required commands         |

## 2. What should a memory system recall?

| Priority | Memory category         | Examples                                           |
| -------- | ----------------------- | -------------------------------------------------- |
| P0       | Known pitfalls          | "Do not use API X; it causes deadlocks"            |
| P0       | Critical invariants     | "All writes must pass through transaction manager" |
| P0       | Current task state      | Completed work, pending changes, blockers          |
| P0       | Architectural decisions | Why Redis was chosen over local caching            |
| P1       | Codebase navigation     | Important modules, entry points, ownership         |
| P1       | Project conventions     | Error handling, naming, testing patterns           |
| P1       | Working procedures      | Build commands, test setup, deployment steps       |
| P1       | Failure history         | Failed attempts, error causes, successful fixes    |
| P2       | User preferences        | Preferred libraries, code style, verbosity         |
| P2       | Environment knowledge   | Platform constraints, tool quirks                  |
| P2       | Performance discoveries | Slow tests, expensive operations, bottlenecks      |

## 3. What makes a memory useful?

A useful memory answers at least one of four questions:

- What should I do? — conventions, procedures, preferred patterns.
- What should I avoid? — failed approaches, traps, incompatibilities.
- Why is it this way? — design rationale, historical decisions.
- Where did I leave off? — progress, unfinished work, validation status.

Consider these two memories:

Low-value memory

"The authentication implementation is in `src/auth/`."

Cheap to rediscover using search.

High-value memory

"Authentication tokens must be refreshed before database transactions. Refreshing inside a transaction can cause connection exhaustion. This was confirmed in issue #142."

Expensive to rediscover and prevents a specific failure.

## 4. Memory system design principles

I'd prioritize five properties:

1. Selective storage: Remember surprising, reusable, expensive-to-discover information rather than every action.
2. Contextual retrieval: Recall memories relevant to the active task, files, errors, and goals.
3. Evidence and provenance: Attach file paths, commits, test results, and confidence.
4. Freshness: Invalidate memories when code or dependencies change; never assume remembered task state is current.
5. Scoped memory: Separate global preferences, repository knowledge, and session/task-specific state.

## 5. A practical memory record

```
type: pitfall
scope: repo:backend
topic: database-transactions

trigger:
  - modifying transaction handling
  - authentication token refresh

lesson: >
  Do not refresh auth tokens inside
  database transactions.

reason: >
  Can exhaust database connections
  under concurrent load.

evidence:
  issue: 142
  file: src/auth/refresh.ts

verified_commit: a31f2c
confidence: high
```

This structure helps retrieval, validation, and invalidation.

Central insight: A coding agent generally doesn't need to remember what the code says because it can inspect the repository. It needs to remember what it previously learned about the code, why certain approaches work or fail, and what remains unfinished.

A useful metric for memory quality is how much redundant investigation, repeated failure, and incorrect work each recalled memory prevents, rather than how much information is stored.

# Designing P0 memory from Pi session history

The two sessions suggest building a structured event analytics system first, with memory extraction on top.

The central idea: don't ask an LLM to summarize everything and call that memory. Instead, reconstruct tasks, failures, decisions, validations, and state transitions from observable events. Use those relationships to identify what is worth remembering.

## 1. What the two sessions reveal

I parsed both JSONL files.

| Observed data                 | `ahead-by-design` | `simple-by-design` |
| ----------------------------- | ----------------- | ------------------ |
| JSONL events                  | 291               | 165                |
| User messages                 | 10                | 4                  |
| Tool calls                    | 142               | 78                 |
| Tool results marked errors    | 6                 | 4                  |
| File reads                    | 60                | 26                 |
| Repeat reads of the same path | 36                | 9                  |
| Compactions                   | 3                 | 1                  |
| Shadow checkpoints            | 4                 | 2                  |

These are event counts, not independent failure rates. Some tool errors are expected negative tests or harmless command failures.

The sessions have different strengths:

- `ahead-by-design`: repeated user-reported failures, root-cause investigations, fixes, regression tests, releases, and subsequent failures. Excellent for mining pitfalls and invariants.
- `simple-by-design`: requirements, evolving plans, decisions to remove complexity, implementation, verification, and commits. Excellent for architectural decisions and task continuity.

### An important discovery: the sessions already contain structured memory

Pi's `shadow-compact.checkpoint` events contain a `structured` object with fields including:

`objective`, `constraints`, `completedWork`, `currentState`, `decisions`, `openIssues`, `nextActions`, `criticalContext`, `modifiedFiles`, `referencedFiles`, `verificationState`, and `continuationState`.

These are useful pre-extracted candidates, often with `evidenceRefs`.

But there is a revealing problem:

Both final checkpoints contain stale task state.

- The final `ahead-by-design` checkpoint says the 0.1.6 work is uncommitted and unpushed. Subsequent events within its coverage gap show that the agent committed and reported pushing 0.1.6.
- The final `simple-by-design` checkpoint says the consolidation and README remain uncommitted. Earlier tool results outside its summarized coverage show successful commits.

The checkpoints were published near the session ends, but their `coverageEndEntryId` values point to earlier events.

This is exactly the kind of problem a memory system must solve: a plausible, structured summary can be less current than the underlying event history.

Memory therefore needs explicit coverage boundaries, event ordering, reconciliation, and evidence-based updates.

## 2. Data model: turning session history into tables

I'd use three layers: raw events, derived operational facts, and memory candidates.

Layer 1 — Raw session history

Messages · Tool calls · Results · Compactions · Checkpoints

Layer 2 — Reconstructed facts

Tasks · Failures · File changes · Tests · Decisions · State transitions

Layer 3 — P0 memory

Pitfalls · Invariants · Task state · Architecture

### Layer 1: source-of-truth tables

| Table          | Key fields                                                      | Purpose                           |
| -------------- | --------------------------------------------------------------- | --------------------------------- |
| `sessions`     | session_id, repo_root, started_at                               | Repository/session scope          |
| `events`       | event_id, session_id, parent_id, seq, timestamp, type, raw_json | Append-only source log            |
| `messages`     | event_id, role, text, model, stop_reason, tokens                | Conversation content              |
| `tool_calls`   | call_id, event_id, tool_name, arguments                         | Requested operations              |
| `tool_results` | call_id, is_error, output, duration_ms                          | Actual operation outcomes         |
| `checkpoints`  | event_id, coverage_end_id, structured_json                      | Historical summaries and coverage |

Preserve Pi's original IDs, `parentId` links, and raw JSON. Join tool results using the exact `toolCallId`. Don't assume every event is on the same active branch.

### Layer 2: analytics tables

These require additional parsing and inference.

| Table               | Important fields                                                          |
| ------------------- | ------------------------------------------------------------------------- |
| `tasks`             | task_id, repo_id, goal, created_seq, status                               |
| `task_events`       | task_id, event_id, event_role                                             |
| `file_activity`     | path, action, task_id, event_id, commit_sha, content_hash                 |
| `failure_episodes`  | issue_id, error_signature, trigger, affected_path, first_seen, resolution |
| `verification_runs` | task_id, command, exit_code, passed, failed, commit_sha                   |
| `change_sets`       | commit_sha, changed_paths, branch, commit_status, push_status             |
| `decisions`         | decision_id, subject, chosen_option, rationale, status, supersedes        |
| `state_transitions` | task_id, from_state, to_state, evidence_event_id                          |

A task is not necessarily one user message. For example, `ahead-by-design` contains a larger task—make the extension reliable—with multiple cycles of investigation, correction, testing, and publication.

### Layer 3: P0 memory schema

One common memory record can serve all four categories.

```
memory_id: string
category: pitfall | invariant | task | decision

scope:
  repo: string
  paths: [string]
  symbols: [string]

subject: string
trigger: string
claim: string
rationale: string

status: active | pending | superseded | stale
confidence: high | medium | low

evidence:
  - session_id: string
    event_id: string
    evidence_type: user | tool | code | commit

valid_at_commit: string | null
last_verified_at: timestamp | null
supersedes: [memory_id]
```

Keep facts separate from their supporting evidence. Multiple memories can cite one event, and one memory can require several events.

Importantly, memory statements and evidence should be independently queryable.

## 3. Analytics: signals worth calculating

The operational tables allow deterministic metrics that identify where memory extraction has value.

| Metric              | Calculation                                            | Signal                                  |
| ------------------- | ------------------------------------------------------ | --------------------------------------- |
| Failure recurrence  | Failure episodes grouped by root-cause signature       | Reusable pitfall                        |
| Post-fix failure    | New user failure following a verified release          | Missing test coverage or incomplete fix |
| Rework intensity    | Repeated exploration of unchanged source across turns  | Possible recall opportunity             |
| Decision churn      | Proposed → revised → accepted → superseded transitions | Important evolving rationale            |
| Invariant exposure  | Changes intersecting protected paths/contracts         | Recall relevant constraints             |
| State drift         | Events after checkpoint coverage boundary              | Potentially stale task memory           |
| Unfinished-work age | Last activity since incomplete task state              | Resume priority                         |
| Verification gap    | Claimed completion without matching execution evidence | Unverified task outcome                 |

### Actual signals from the sessions

Repeated file reads

# 45/86

Reads beyond first per exact path, within each session

Checkpoint coverage gaps

# 17 / 37

Intervening events after final checkpoint coverage: ahead / simple

These numbers are potential signals, not proof of memory failures.

Repeated reads may be necessary because the file changed. A better metric is repeated reading of an unchanged file across separate task turns.

Checkpoint gaps are more direct: the summary is known not to cover later events.

For `ahead-by-design`, another strong signal is the sequence of reported test totals—27, 52, 61, 65, 75, and 85—alongside new user-reported failures after successive fixes. Increasing test counts demonstrated regression coverage, but not end-to-end reliability.

The memory system should recognize that distinction.

# 4. P0 analysis, one category at a time

## P0.1 — Known pitfalls

Question to reconstruct: What previously went wrong, why, and how do we avoid it?

### Evidence from `ahead-by-design`

The session offers a useful sequence of failure episodes.

| Observed failure                                   | Reconstructed cause                                                                         | Preventive knowledge                                 |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| `Unverified evidence: docs/README.md`              | Incorrect citation path; previous recovery addressed line drift but not misplaced filenames | Verify exact source identity before citation repair  |
| Missing dependency `dedup-reference-repointing.md` | Sibling filename interpreted relative to the wrong directory                                | Canonicalize decision references at response ingress |
| Decision ID collision                              | Model reused a filename for a distinct question                                             | Controller owns unique filename allocation           |
| `Model stopped: length`                            | Output-token allowance exhausted during grounding                                           | Use bounded, model-aware truncation recovery         |

These are grounded in user-reported errors and subsequent investigations. The precise original failing model outputs were not all retained, so some low-level causes remain reconstructed rather than directly replayed.

### How I'd reverse engineer pitfalls

1. Extract error observations from user messages, failed tool results, and test output.
2. Normalize errors into signatures, stripping incidental paths, IDs, and line numbers while preserving their meaning.
3. Associate each failure with the investigation, changed files, and stated cause.
4. Find tests that fail before a fix and pass afterward.
5. Look for subsequent failures that invalidate the claimed fix or reveal another edge case.

The resulting memory should be actionable rather than a chronology.

Extracted pitfall example

High-value

Scope: `ahead-by-design` · citation recovery

Trigger: Modifying citation validation or model-output recovery.

Recall: Incorrect source paths cannot be repaired by line-number adjustment alone. A path correction must be justified by an exact quote uniquely matching an inspected, permitted source. Ambiguous citations must not be silently accepted.

Why: The earlier partial correction was followed by a `docs/README.md` failure; the subsequent fix tightened source-path recovery.

Evidence: User error, agent investigation, code/test changes, and 0.1.3 commit.

Useful ranking features: repeated occurrence, number of distinct affected tasks, failed-fix history, severity, and how expensive the lesson would be to rediscover.

I would rank failed fixes that taught a general rule above generic command errors.

## P0.2 — Critical invariants

Question to reconstruct: What must remain true when code changes?

These differ from pitfalls. Pitfalls are learned from failure; invariants may be established before any failure.

### Evidence from the sessions

| Project            | Invariant candidate                                             | Evidence                                             |
| ------------------ | --------------------------------------------------------------- | ---------------------------------------------------- |
| `ahead-by-design`  | Runtime writes stay within permitted design artifacts           | Runtime contract described in checkpoint constraints |
| `ahead-by-design`  | Never report `no-gaps-found` from invalid or ambiguous evidence | Safety constraints, recovery design, tests           |
| `ahead-by-design`  | Preserve existing user-owned decisions and artifacts            | Explicit preservation rules and collision handling   |
| `simple-by-design` | One recursive design loop remains the entry point               | User requirement, revised plan, implementation       |
| `simple-by-design` | Language-specific standards are disclosed only when relevant    | User requirement and revised skill structure         |

### How I'd reverse engineer invariants

Start with explicit requirement language: must, only, never, preserve, cannot, and without.

Then look for corroboration:

- Is it an actual user requirement or merely an assistant suggestion?
- Is it encoded in a validation function or regression test?
- Does a commit preserve it while changing adjacent behavior?
- Was it later revised or superseded?

The most interesting invariants often appear during fixes.

For example, the `ahead-by-design` agent wanted to recover automatically from bad model output, but without weakening citation verification, dependency safety, or preservation of existing decisions.

That gives a more useful memory than simply saying "validate citations."

Extracted invariant example

Rule: Recovery logic may correct unambiguous formatting or identity mistakes, but must not turn unverified evidence into successful completion.

Applies to: Evidence validation, model-output repair, grounding, and completion reporting.

Validation: Require both implementation checks and regression coverage for rejected ambiguous inputs.

For ranking, I'd look at the number of code paths depending on the invariant, its explicitness, the consequences of violation, and whether a deterministic guard already exists.

Store the invariant even when tests enforce it, but retrieve it particularly when a task touches its enforcement boundary.

## P0.3 — Current task state

Question to reconstruct: What has actually been done, what remains, and what can safely happen next?

This is where event analytics is most valuable.

### Recommended task state machine

Each transition needs evidence. Failed or blocked states can branch from any stage.

A test pass is not a commit. A commit is not a push. A push is not a successful installation or live execution.

The sessions demonstrate why those distinctions matter.

### Reconstructed end-of-session state

| Project            | Supported final state                                                                      | Remaining uncertainty                                          |
| ------------------ | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| `ahead-by-design`  | Session reports version 0.1.6 committed and pushed; 85 tests and TypeScript checks passed  | No confirmed successful live Pi/provider rerun                 |
| `simple-by-design` | Consolidation committed as `ef64973`; README rewrite committed as `cba2657`; version 0.2.0 | Eval baseline and semantic discovery quality remain unverified |

The last checkpoints by themselves would have produced incorrect answers for the commit state.

### How I'd reverse engineer task state

Use the latest relevant user request as the objective, then reconcile all subsequent evidence in order.

Tool results establish operations. Git state and commit records establish persisted changes. Validation results establish what was tested. Checkpoints provide hypotheses for unfinished work, not authoritative current state.

For every task, store:

```
task: simple-by-design consolidation

implementation: completed
verification: partially_verified
committed: true
published: unknown

remaining:
  - design-loop evaluation baseline
  - semantic discovery quality validation

as_of: session_end
requires_repo_refresh: true
```

For analytics, useful metrics include percentage of tasks ending with uncommitted changes, incomplete validation, or unresolved blockers; how often a checkpoint's state disagrees with later events; and how much time/rediscovery occurs when resuming.

Crucial rule: Task memory is a snapshot, not a permanent fact. Refresh Git status, file hashes, and relevant tests before relying on it.

## P0.4 — Architectural decisions

Question to reconstruct: What choice was made, what alternatives were rejected, and why?

`simple-by-design` is the stronger example.

### Reconstructed decisions

| Decision                                 | Rationale recovered from history                                      |
| ---------------------------------------- | --------------------------------------------------------------------- |
| Use the prototype recursive loop         | Replace heavyweight preflight routing and approval gates              |
| Keep standards progressively disclosed   | Preserve needed guidance without loading it for every task            |
| Don't merge existing standards files     | Avoid creating conflicting authority or unnecessary reference changes |
| Rename `check.mjs` to `frontier.mjs`     | Clarify the design-frontier check versus implementation verification  |
| Keep `review.md`, but narrow its role    | Avoid duplicated gates while retaining useful review guidance         |
| Update eval definitions with the release | Old evaluation criteria would incorrectly assess the new workflow     |

These are not simply documentation facts. They have causal histories.

For instance, the plan initially proposed merging contract rules into defaults, then rejected that approach because it could create a second authority.

Only storing the final file layout loses the reason for that layout.

### How I'd reverse engineer decisions

Find events containing proposals, tradeoffs, revisions, and accepted choices. Link them to the relevant implementation and commit.

The critical extraction pattern is:

DECISION CHAIN

Problem → Proposed options → Selected option → Rationale → Implementation evidence → Subsequent revision

Example memory:

```
category: decision
subject: design frontier checker naming

chosen: scripts/frontier.mjs
rejected: retaining scripts/check.mjs

rationale: >
  Differentiate checking design frontier
  from verifying implementation.

related:
  - scripts/verify.sh
  - SKILL.md
  - README.md

status: implemented
release: 0.2.0
```

For analytics, track decision revisions, reversals, reasons for rejected approaches, and whether implementation actually followed the decision.

The strongest architectural memories are often negative decisions with durable rationale: why something deliberately wasn't built, merged, generalized, or automated.

## 5. Turning analytics into retrieval

I would avoid retrieving memories solely through semantic similarity.

Instead, combine deterministic context with semantic matching.

Suppose a new task is:

> Fix citation recovery in `src/repository.ts`.

The retrieval system should build a context fingerprint from the repository, target file, related symbols, task intent, and observed errors.

Then retrieve category-specific memories:

| P0 category | Relevant recall                                              |
| ----------- | ------------------------------------------------------------ |
| Pitfall     | Prior `docs/README.md` path-repair failure                   |
| Invariant   | No fabricated or ambiguous evidence accepted                 |
| Task state  | Most recent validation and unresolved live-testing gap       |
| Decision    | Preserve bounded recovery rather than loosening verification |

This is much better than loading a generic project summary.

A simple architecture:

Task context

Repo · paths · error · intent

Memory index

Scope · triggers · evidence

Rank + reconcile

Relevance · severity · confidence · freshness · supersession

Small P0 recall packet

Actionable facts with supporting event references

## 6. What I'd build first

I would prioritize failure episodes, decision chains, and task-state reconciliation over embeddings or more elaborate summary generation.

The initial pipeline:

1. Parse JSONL into normalized event, tool, task, and change tables.
2. Extract failure episodes and decision chains.
3. Import Pi's existing structured checkpoints as candidate facts.
4. Reconcile candidates against subsequent events and Git evidence.
5. Generate typed P0 memories with triggers, evidence, and validity.
6. Retrieve them for matching future tasks and measure whether they actually prevent repeated work.

For evaluating the system, I'd use session replay: at a chosen point, hide later events, extract memory from the earlier history, and test whether retrieval supplies information that would have prevented the subsequent failure or unnecessary rediscovery. Compare against a no-memory baseline.

Two sessions are enough to validate the extraction design, not to estimate general effectiveness.

My main conclusion: treat the raw session history as an event-sourced knowledge base. The four P0 categories are different projections over that history:

- Pitfalls: failure → diagnosis → correction.
- Invariants: constraint → enforcement → preservation.
- Task state: objective → operations → verified latest state.
- Decisions: alternatives → choice → rationale → implementation.

The decisive engineering challenge is not extracting plausible memories. It's maintaining evidence-backed memories that remain correct as the codebase and task progress change.

# Review of `remember`: evidence-driven recall

I would keep the stateless, on-demand design. The project already has much of the right infrastructure. I would add a small number of tools that reconstruct episodes, causal relationships, and historical evidence rather than introducing a persistent memory or task-status database.

The distinction matters:

- A memory database stores conclusions such as "task completed" or "this architecture is required."
- An evidence database reconstructs what was requested, investigated, changed, tested, rejected, and committed. The coding agent draws conclusions from that evidence.

For your goal, the second approach is preferable. It avoids stale conclusions and lets the agent revisit the primary evidence when the current goal changes.

## 1. What `remember` already does well

From the uploaded implementation:

| Existing capability          | Assessment                                                             |
| ---------------------------- | ---------------------------------------------------------------------- |
| `recall WORDS`               | Good starting point for finding relevant prior commits                 |
| `history TARGET`             | Useful target-specific history, including corrections and failed edits |
| `inspect TARGET`             | Combines source, relationships, Git, and session leads                 |
| `sessions.py context`        | Expands original recorded exchanges                                    |
| `sessions.py recurrence`     | Already identifies recurring failed tool results                       |
| `edit_hunks`, `edit_symbols` | Valuable historical edit evidence                                      |
| `commit_links`               | Associates session operations with Git commits, including confidence   |
| `query`                      | Read-only SQLite across source, Git, and sessions                      |
| `show REF`                   | Evidence expansion and continuation                                    |
| `coverage`                   | Makes incomplete investigations visible                                |

The implementation also has important safeguards: recorded requests are distinguished from agent claims, uncertain symbol mappings remain qualified, and session-derived query tables are invocation-local.

I would preserve the existing schema and provider architecture, extending it with derived views built from source evidence.

The main limitation is the shape of retrieval. `recall` currently ranks commits, and `history` ranks events or turns associated with a target. Neither primarily reconstructs the complete sequence that connects a user request to an attempted solution, a failure, a correction, and its eventual outcome.

That's the largest opportunity.

## 2. What I would add

Four tools, implemented as on-demand recipes over the existing providers.

| Priority | Proposed tool      | Purpose                                                                             |
| -------- | ------------------ | ----------------------------------------------------------------------------------- |
| P0       | `investigate GOAL` | Find and organize evidence relevant to a goal across the four P0 categories         |
| P0       | `trace REF`        | Reconstruct the chronological episode around an event, failure, decision, or commit |
| P0       | `activity TARGET`  | Show what was read, written, tested, and committed for a file or symbol             |
| P1       | `signals TARGET`   | Calculate useful patterns from the underlying evidence                              |

These are proposed commands, not existing ones.

### `investigate`: goal-directed evidence discovery

For example:

```
remember investigate "fix citation recovery"
```

It should return a compact evidence index:

| Category   | Discovered lead                                        | Expansion                      |
| ---------- | ------------------------------------------------------ | ------------------------------ |
| Pitfall    | Earlier citation repair failed on an incorrect path    | Trace failure and fix          |
| Invariant  | Unverified evidence must not be accepted               | Read constraints and tests     |
| Task state | Relevant fix was committed; live rerun not established | Examine validation and commits |
| Decision   | Recovery was deliberately bounded                      | Read discussion and rationale  |

Each lead should contain evidence references, matching basis, contradictions, and coverage.

The agent decides what each lead means. `remember` does not have to assert these as permanent memories.

### `trace`: reconstruct the episode

For a failure, return something like:

Citation path failure — reconstructed episode

1. User reports failure

   `Unverified evidence: docs/README.md`
2. Agent investigates

   Searches and reads citation verification code.
3. Agent modifies code

   Python shell commands change `src/repository.ts` and tests.
4. Agent verifies

   Runs regression tests and TypeScript checks.
5. Agent commits

   `33fa24b` — citation recovery in 0.1.3.
6. Later evidence

   Subsequent user requests report other recovery failures.

Each step links back to original messages, commands, outputs, and Git evidence.

The investigation should support both directions:

- Deepen: Expand a specific event into its command, result, patch, test output, or surrounding exchange.
- Widen: Include previous attempts, later regressions, related files, other sessions, and neighboring commits.

Chronological reconstruction should be the default presentation, but link events using explicit evidence relationships wherever possible. Temporal adjacency alone does not prove causation.

### `activity`: file-level historical operations

For example:

```
remember activity src/engine.ts
```

Return a table with this grain:

| Field          | Meaning                                            |
| -------------- | -------------------------------------------------- |
| `event_ref`    | Original evidence                                  |
| `turn`         | User request/agent turn                            |
| `timestamp`    | Recorded ordering                                  |
| `operation`    | read, write, edit, test, commit                    |
| `path`         | Normalized file                                    |
| `range`        | Read or edited lines, when known                   |
| `content_hash` | Source version observed, if recoverable            |
| `outcome`      | Success/failure/unknown                            |
| `basis`        | Direct tool observation, shell inference, Git diff |
| `commit_sha`   | Associated commit, when supported                  |

This is especially important for determining what information the agent had when making a decision.

### `signals`: analytical evidence

For example:

```
remember signals src/engine.ts
```

Return grouped evidence about recurring failures, successive fixes, repeated reads, conflicting decisions, and validation gaps.

No dashboard is necessary. A small set of SQL aggregations with drill-down references is sufficient.

## 3. What do the Pi sessions actually capture?

This is the most important implementation detail I found.

# 86

Direct `read` calls

# 9

Direct `write` calls

# 97

Bash calls

Across both sessions, 46 Bash calls contain inline Python heredocs. Many of those modify source files.

### What you can recover

| Operation          | Recorded evidence                                 | Limitation                                                               |
| ------------------ | ------------------------------------------------- | ------------------------------------------------------------------------ |
| Pi `read`          | Requested path, optional range, returned text     | Only what the tool returned                                              |
| Pi `write`         | Path and exact submitted content                  | Overwrites aren't necessarily represented as old/new diffs               |
| Pi `edit`          | Arguments and, when present, structured patch     | Not used directly in these examples                                      |
| Bash               | Full command and captured stdout/stderr           | Arbitrary file effects aren't normalized                                 |
| Git commit         | SHA, metadata, changed files, diff through `show` | Final committed changes, not every intermediate edit                     |
| `git_plan_context` | Structured changed-file list and diffs            | Snapshot of modifications, not proof of which command caused each change |

Yes, the data can show exactly what files the agent read and what content it received, especially for native Pi read calls. This enables the reconstruction of the agent's observed context.

But the current `edit_hunks` implementation misses much of the write history in these examples.

Why? It recognizes Pi's native `write` and `edit` tools, while most actual source changes occurred through Bash commands executing Python scripts.

### A particularly valuable existing source

Both sessions use `git_plan_context`:

- `ahead-by-design`: 10 calls
- `simple-by-design`: 2 calls

I inspected the outputs. They include structured file changes with fields such as `path`, `status`, `insertions`, `deletions`, `diff`, and `diffLines`.

I would add a parser for these Git planning snapshots before building a sophisticated new analytics engine.

That gives you much better visibility into source changes performed indirectly through Bash.

I'd also teach `commit_links` to recognize `git_commit_plan` results. Currently, its witness extractor is oriented around shell `git commit` invocations, whereas these sessions have seven `git_commit_plan` calls. Their returned SHAs should become first-class commit evidence, with failed operations kept distinct.

## 4. Are we already reconstructing the story chronologically?

Partially.

The underlying data has chronological information:

- Original event order and timestamps.
- Parent/branch relationships.
- User messages and agent messages.
- Tool call/result pairing.
- Git commit chronology.
- Existing turn IDs.

But the primary retrieval methods aren't organized as complete episodes.

| Current method | Primary ordering                        |
| -------------- | --------------------------------------- |
| `recall`       | Lexical relevance, then recency         |
| `history`      | Evidence/signal tiers, then recency     |
| `inspect`      | Source, selected history, relationships |
| `query`        | Whatever the SQL requests               |

This isn't necessarily wrong. Ranking is excellent for locating a starting point.

I would use relevance to find the episode, chronology to understand it, and evidence relationships to explain it.

The missing structure is a transient episode grouping:

```
User request
  ├─ Investigation and reads
  ├─ Proposed approaches
  ├─ File changes
  ├─ Verification
  ├─ Commit or abandonment
  └─ Later correction or regression
```

Create that grouping during the query, not as durable task state.

## 5. Where analytics fits

Yes, data analytics fits. I'd make it primarily a signal-discovery and prioritization layer, not a replacement for reading evidence.

The highest-value signals for the four P0 categories:

| P0                      | Analytics signal                                   | What to investigate                      |
| ----------------------- | -------------------------------------------------- | ---------------------------------------- |
| Known pitfalls          | Repeated error after apparent fix                  | Incomplete solution or missing edge case |
| Critical invariants     | Repeatedly preserved condition across changes      | Possible project-wide constraint         |
| Current task state      | Latest edit/test/commit/push sequence              | What was actually completed              |
| Architectural decisions | Proposal changed or rejected before implementation | Rationale and rejected alternatives      |

Two especially useful derived metrics:

Failure-after-fix rate: Count cases where the user reports the same normalized failure after a related fix was committed. Differentiate repeated causes from superficially similar error messages.

Read-to-change trajectory: Identify which files were read before a change, which changed afterward, and which tests were executed. This reveals the investigation path behind implementation.

Both should return expandable evidence rather than just a numeric score.

For example, a high repeated-error count should lead directly to the affected user requests, attempted fixes, changed code, and verification results.

## 6. What I'd change first in this repository

In implementation order:

1. Extend `pi-v1.json` and the ingestion helpers to recognize `git_plan_context` and `git_commit_plan` as structured operations. This immediately improves the fidelity of the two sample sessions.
2. Add an invocation-local `operations` view that joins tool calls with outcomes, file paths, read/write content evidence, Git snapshots, and commits. Include an explicit `basis` column so inferred Bash effects cannot masquerade as witnessed edits.
3. Implement `trace` using the existing turn, timestamp, parent, call/result, and commit relations. This is the foundation for understanding how requests evolve.
4. Implement `investigate` as a goal-based recipe that starts with `recall`, identifies relevant episodes, and returns four evidence groups with continuations. No long-term status records.
5. Add `signals` as derived SQLite views and test them against the two sessions. Avoid accumulating permanent metrics.

One packaging issue worth checking: the uploaded archive documents `scripts/sessions.py` and `scripts/rows.py`, but neither source file appears in the archive. Some legacy commands, including `recurrence`, therefore aren't directly executable from this copy as documented.

## 7. A useful end-state

Given:

```
remember investigate "improve citation verification"
```

The agent could receive:

- A previous failed citation-verification episode.
- Earlier changes and their diffs.
- The exact source the earlier agent read.
- The user request and subsequent corrections.
- Tests that were run, with their recorded results.
- Architectural constraints and decisions found in the discussion.
- Current source paths and the latest Git state.
- References to deepen or widen any finding.

The output would not need to say "here are four facts you must remember." It would say "here is the relevant evidence, how it connects, what is verified, and what remains uncertain."

That's a stronger fit for this project than persistent P0 memory records.

My biggest recommended shift is from commit-centric recall to episode-centric investigation, while retaining commit-centric search as a fast discovery entry point. The existing architecture supports that direction without abandoning the stateless approach.
