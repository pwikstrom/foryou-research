# 0013. Scope the sessions refresh to the collections an enrichment change touches

Date: 2026-09-03

## Context

The sessions refresh already re-segmented only the collections whose
coverage windows or in-window play counts had changed. Enrichment changes
(new embeddings, new annotations) were different: the per-collection
fingerprint comes from the activity file, which carries no enrichment
columns, so the build could not tell which collections an enrichment change
touched, and any change rebuilt every covered collection. A single
annotation batch of 50 items cost a ~8 minute full rebuild.

## Decision

Scope an enrichment change when locality can be proven
(`session_explorer.enrichment_change_scope`):

- The embedding shards are append-only. If every shard the previous build
  recorded is still present byte-identical, the store has only grown; the
  vectors past the previous build's count and the annotation rows past its
  `inference_ts` watermark name exactly the changed items.
- Only the collections holding those items join the refresh, as a merge.
- Otherwise fall back to a full rebuild, which resets the baseline: a
  rewritten or missing shard, a build predating the recorded shard set or
  watermark, or more vectors appended since the last full build than
  `[sessions] rebaseline_fraction` (default 0.05) of the corpus at that
  build.

## Consequences

- `inference_ts` is an epoch in **seconds**; it is read from Arrow, not via
  pandas, to keep the unit intact.
- The rebaseline fraction is the drift budget for the corpus mean that
  segmentation centres on; past it every collection is re-baselined on the
  current mean.
- The behaviour is documented in
  [pipeline.md](../pipeline.md#sessions-refresh).
