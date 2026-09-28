# 0008. Explicit dispatch deadlines for self-chaining workers

Date: 2026-08-09

## Context

Cloud Tasks' default HTTP dispatch deadline (600 s) is shorter than a heavy
batch link, and an attempt that times out *keeps running* while the queue's
retry starts a concurrent duplicate. On its first production run
(2026-08-09) `sessions_refresh` looped this way: link 0 compacts the dense
embedding sidecar, streams the whole `collection_id` column, then segments
the largest batch — past 600 s, so the task was re-dispatched from scratch
every 10 minutes. The worker declared its own 1800 s deadline, but that
governed only the links it dispatched itself; the *initial* dispatch came from
`process_manager.start_process`, which had no entry for it.
`timelines_refresh` and `embeddings_refresh` had the same gap, not yet
triggered.

## Decision

Every self-chaining refresh carries an explicit 1800 s deadline, and every
dispatch reads it from one place: the initial dispatch from `process_manager`
and each link a worker chains. The deadline is declared once per worker in
`WORKERS` (`web_interface/worker_registry.py`) and read through
`worker_registry.deadline_for()`.

## Consequences

- `tests/unit/test_dispatch_deadlines.py` pins the table.
- 1800 s is also the ceiling a Cloud Run batch plans against (the scrapers'
  batch deadline clamps to it on Cloud Run).
- The enrichment supervisor never self-chains, which keeps it clear of this
  trap entirely.
- The looping run recovered by itself, which exercised the sidecar's
  append-only design: attempt 1 built the dense store (56 s) and then blew
  the deadline; attempt 2 found it cached and finished link 0 in 104 s.
