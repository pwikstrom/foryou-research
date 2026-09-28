# 0022. One engagement vocabulary; inferred links are named on the row

Date: 2026-09-17

## Context

Engagement rows (likes, bookmarks, comments, shares) are folded onto the play
row of the item they concern, and that folded token is the only engagement
signal that reaches a study. Two things made the signal hard to use:

- **Links were inferred silently.** Some links are inferences: a like whose
  only logged view of the item is days earlier is attached by a
  nearest-play fallback, and a TikTok comment, which names no video,
  borrows the id of the last activity within 180 s. Nothing on the row said so.
- **Platforms used different words** (2026-09-22). TikTok bookmarks were
  stored as `fave` — the same value as a like — so a like and a bookmark
  could not be told apart, and other platforms' saves and comments were not
  all read.

## Decision

- Every inferred link is named on its row (2026-09-17): `link_method`, a base
  activity-contract column. A lead play carries `adjacent`, `nearest_play` or
  `adjacent,nearest_play`; a TikTok comment whose video id came from the
  forward fill carries `ffill_180s`; everything else is null. The contract
  description documents the inference, and the column lets an analysis
  exclude it.
- One vocabulary across platforms (2026-09-22), owned by
  `fyp/core/utils.py`: `fave` = a like, `save` = a bookmark, `comment`,
  `share` (including reposts), `follow`. Every ingester declares what it
  emits in `emitted_activity_types`.
- Stored data is migrated: `scripts/migrate_engagement_vocabulary.py`
  (testable half in `fyp/ingest/migrations/engagement_vocabulary.py`) retags
  stored TikTok bookmarks from `fave` to `save`.

## Consequences

- `tests/unit/test_ingest_activity_vocabulary.py` checks each class's
  declaration against its section maps, and `process()` writes a ledger note
  on any file whose rows fall outside it.
- The vocabulary and per-platform mappings are in
  [pipeline.md](../pipeline.md#activity-vocabulary-and-engagement-linking);
  the rules a new platform follows are in
  [extending.md](../extending.md#activity-types-and-engagement).
