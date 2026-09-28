# 0003. Scraper storm guards: abort on homogeneous failure runs

Date: 2026-07-16

## Context

A scraper classifies every failure as `permanent:<reason>` (pruned from the
queue and recorded as failed) or `transient:<reason>` (kept for a later run).
The circuit breaker already stopped a batch after a run of throttle verdicts
(`rate_limited` / `bot_check`). Two incidents showed that a broken session can
fail every item with some *other* single verdict:

- **2026-07-16, permanent side.** A flagged Instagram session answered 404
  for live posts. Each was classified `permanent:removed`, and the whole
  batch was pruned from the queue as if the posts were gone.
- **2026-08-10, transient side.** TikTok's new bot-challenge wall made yt-dlp
  fail every item with "No video formats found!", classified
  `transient:unknown`. That is neither a throttle verdict nor a permanent
  one, so it was invisible to both the circuit breaker and the
  permanent-storm guard, and the worker kept churning the queue at 0% yield.

## Decision

- **Permanent-storm guard** (`scrape._permanent_storm_threshold`, default 15,
  `[misc] scraper_permanent_storm_threshold`): N consecutive identical
  `permanent:<category>` results abort the batch like the circuit breaker,
  demote those ids to transient (kept queued, excluded from the
  failed-scrapes record), and stop Cloud Task self-chaining (batch attrs
  `permanent_storm_tripped` / `permanent_storm_category`).
- **Transient-storm guard** (`scrape._transient_storm_threshold`, default 25,
  `[misc] scraper_transient_storm_threshold`), added 2026-08-11: N
  consecutive identical `transient:<category>` results abort the batch, stop
  chaining, and raise the same persistent alert (`KIND_TRANSIENT_STORM`). No
  demotion is needed; the items are already transient and stay queued.
- A storm raises a persistent per-platform scraper alert
  (`fyp/scrape/scraper_alerts.py`) for a human to act on.

The trade-off is accepted: a genuinely dead, homogeneous queue run stays
queued and stops the worker. That is recoverable; false pruning is not.

## Consequences

- The guards and their thresholds are documented in
  [pipeline.md](../pipeline.md#2-scraping-fypscrape) and the config keys in
  [configuration.md](../configuration.md).
- A queue made only of retries is homogeneous by construction, which the
  guards read as a broken session; per-item evidence can exempt a verdict
  ([decision 0024](0024-corroborated-permanent-verdicts.md)).
- Media-leg verdicts count toward the guards too
  ([decision 0023](0023-media-leg-failures-batch-deadline-youtube-pacing.md)),
  and network-outage failures never do
  ([decision 0027](0027-network-outages-are-waited-out.md)).
