# Runtime contracts

## investigate

- `GOAL` must have descriptive words of 3 or more letters. Do not use only general verbs. If you omit `GOAL`, the tool gives an orientation of the repository.
- `--path` adds one `signals PATH` expansion per path, max 3.
- `scope.archive_horizon` is the oldest commit or session that the tool scans. A "not found" result covers only this horizon.

## inspect

- `inspect TARGET` runs a fixed recipe: find one target, then read source, packet leads, relationships and history, then do one session-lead expansion.
- If the target is ambiguous or not found, the tool returns candidates. It does not read history.
- With intent `change`, the tool uses file granularity for impact. With `bug` or `review`, it examines both directions at the target's granularity.
- Results are evidence only. They are not a root cause or a safety decision.
- `--plan` lists the stages. It does not read data.

## trace

- `trace REF` starts at one human exchange. Or it starts at the witness exchanges of a commit. The tool keeps these two groups apart. A shared path is `related_context`. It is not a contribution.
- Branches are separate lanes. The tool never merges them by timestamp.
- These are different observations: test pass, commit, push, live confirmation. A checkpoint is only a claim with a known coverage limit.
- The tool keeps unknown results and unsupported checkpoint shapes visible.

- `section: "link"` rows: candidate episode links, each with a `trace` continuation.

## Tables

- Transient, per call: `message_segments`, `episodes`, `episode_links`, `file_observations`, `change_observations`.
- Leads: `match_bases`, `matched`, `via` replace `relevance`. Commit observations add `object_resolved`, `reachable_from_head`; only a resolved object is `git_fact`.

## show

- `show` accepts these refs: snapshot source, node or edge refs; cite tokens; event JSON refs; full commit SHAs (`git:` is optional); and `continue:` tokens.
- An expanded session event has `selected: true`. Rows near it are previews, not the anchor.

## SQL

- One read-only `SELECT` or `WITH` for each call.
- The tool rejects these: data changes, ATTACH, PRAGMA, extension load, and more than one statement.
- A query loads only the tables it names.
- `--plan` lists the tables and does not load providers. The tool still reads the `--input` files to get their schema.
- `--param NAME=VALUE` binds `:NAME`, `$NAME` or `@NAME` as a string.
- `--input NAME=FILE` adds object rows from a JSON array, a JSONL file, or a current result envelope (`-` is stdin). The built-in names are reserved.
- `--type TABLE.COLUMN=TYPE` supplies the type for a column that is missing or all null.
- Use CTEs for intermediate tables. DDL is unsupported.
- SQL reads all bounded input before paging: `--limit` changes rows delivered, not aggregates.

## Budgets

| Scope | Time | Files | Recordings, events per harness | Commits | Input rows |
| --- | --- | --- | --- | --- | --- |
| standard | 30 s | 20,000 | 25, 25,000 | 200 | 100,000 |
| deep | 120 s | 50,000 | 100, 100,000 | 2,000 | 200,000 |

- Maximum size for one file: 1 MiB.
- The tool checks deadlines at provider and file boundaries. Git and SQLite get the remaining time. A single parse cannot be stopped.
- The SQLite heap has a maximum of 512 MiB. The tool stops with an error, not a partial result.
- If a standard result is partial, one `coverage` continuation reruns at the deep level. A recipe never widens scope by itself.

## Results

- `coverage` shows how complete the evidence is for each provider. `partial` means that a gap, or a possibly incomplete external input, can change the answer.
- `delivery` describes only the rows that the tool delivers. Paging and byte caps set `complete: false`. They do not make the evidence partial.
- `next` contains executable refs. `coverage` increases a computation limit. `delivery` reads the next page. `expansion` reads more evidence. The order is coverage, delivery, expansion.
- If one row is larger than `--max-bytes`, the tool gives one delivery ref sized to fit.
- If the metadata alone overflows, these items stay: `views`, the status, and that ref. `next_omitted` names the item that the tool dropped.
- `views` counts apply to each page.

## Continuations

- A continuation contains the absolute scope, the filters, the presentation and the budget.
- A source query locks the snapshot. Transient inputs reload with a fingerprint check. A changed input stops with an error; pages do not move.
- Tokens are data, not shell commands. They never contain transcript text.
- A stdin continuation needs the same rows again through a pipe. A terminal stdin stops with an error, not a wait.
- `next[].argv` is quoted for the current platform (POSIX or Windows argv).
- PowerShell 5.1 removes `"` from native arguments. Use PowerShell 7 or later, or write `\"`.

## Exit codes

- `0`: the result is complete. This includes evidence that is explicitly partial.
- `2`: the input is not valid, or the tool failed.
- `130`: Ctrl-C. The tool gives `"error": "cancelled"`.
- Truncation counts as delivery. It does not make the evidence partial.
- The tool caches one source snapshot per workspace plus its lookup index. It never keeps results or transcript text.
