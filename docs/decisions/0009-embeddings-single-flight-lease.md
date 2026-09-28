# 0009. Single-flight lease for the embeddings refresh

Date: 2026-08-15

## Context

On 2026-08-14 a Cloud Tasks redelivery of a still-running embeddings batch
ran concurrently with the original. Both embedded the identical 10,175-item
backlog slice and appended twin shards to the embedding store. The
duplicates flowed into `video_map.parquet`, whose duplicated item ids crashed
every `sessions_refresh` batch and would have silently duplicated rows of
recoded study frames at the niche join.

## Decision

- `embeddings_refresh` claims a compare-and-swap lease file
  (`cache/embeddings_run_lease.json`, via `data_io.update_json`): link 0
  claims the run, closing the dispatch paths that bypass
  `process_manager`'s busy check, and each chain link claims its chunk, so a
  redelivered link exits instead of re-embedding. The lease goes stale after
  3600 s, so a crashed run never wedges refreshes.
- The writer re-checks the already-embedded ids just before writing a shard.
- The readers dedupe on item id, last occurrence wins (the dense sidecar's
  rule): the embedding loader, the video-map features read by the sessions
  build, and the niche-column join.

## Consequences

- A Cloud Tasks redelivery can never run two appenders against the shard
  store at once. The job framework is described in
  [web_interface.md](../web_interface.md#background-workers).
