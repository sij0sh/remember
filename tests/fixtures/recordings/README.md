# Recording fixtures

Synthetic JSONL only. Field shapes follow observed Pi events but contain no real
transcript, repository or session content. They exercise defensive ingestion and
do not prove support for an unobserved real payload variant.

- `pi-slice1.jsonl`: human and injected requests, a sibling lane, paired and
  missing results, a failed edit, an unknown tool, a successful structured plan
  snapshot, successful/failed structured commit results, and checkpoints whose
  coverage ends on the same or a sibling lane.
- `pi-trace.jsonl`: requirement, native edit/test/commit, a failed sibling
  branch, stale and covered checkpoints, a follow-up exchange, and a checkpoint
  whose coverage crosses exchanges. Synthetic only; exercises trace lanes,
  coverage gaps and per-dimension observations.
