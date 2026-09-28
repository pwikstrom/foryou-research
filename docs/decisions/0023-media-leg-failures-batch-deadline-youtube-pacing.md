# 0023. Media-leg failures stay queued; the batch deadline keeps finished work; YouTube is paced

Date: 2026-09-19

## Context

A local YouTube drain on 2026-09-18 went wrong in three connected ways.

**The session was throttled.** The drain ran 2–4 concurrent downloads with a
1.5 s delay over one signed-in session and was soft-blocked after ~700 media
pulls in 34 minutes. YouTube throttles the *session*, not the request.

**The batch deadline threw away finished work.** When the batch passed its
deadline, the handler wrote every unfinished item off as `timeout` and then
blocked on the thread pool's exit anyway: it ran 341 downloads for 11 more
minutes and discarded all of them, pushing the already-throttled session
further into the storm. The deadline estimate itself was 3–6× optimistic for
YouTube, because it counted waves by the thread-pool size, which is
deliberately larger than the real (throttled) concurrency.

**A permanent verdict on the media leg pruned live videos.** The rate-limited
session answered a bare "Video unavailable" for the stream fetch of 187 live
videos whose metadata had scraped. The classifier read it as `removed`, and
the rule of the time — keep an item queued for a media retry only when the
media error was transient — wrote them as scrape-ok rows with no media and
pruned 181 of them from the queue for good. The permanent-storm guard did
trip, but its log line reported "0 demoted": media-leg verdicts are results
(the metadata did scrape), not failures, so the demotion loop never saw the
164 rows that had tripped it.

## Decision

- **Media-leg failures stay queued whatever their category.** A permanent
  verdict on the media leg is not trusted on its own. Retries are bounded by
  a media-retry budget instead (`scrape_queues.charge_media_retry`,
  `MAX_MEDIA_RETRY_STRIKES` = 3 healthy runs, sidecar
  `scrape_media_retry_strikes_<platform>.json`); aborted batches never
  charge, and an exhausted item is pruned with its metadata-only row
  standing (no failed-scrapes entry — the metadata did scrape).
- **The storm guards count media-leg verdicts,** and the permanent-storm
  guard's log line reports both populations (demoted failures and media-leg
  rows).
- **The batch deadline keeps finished work.** Waves are counted at the
  throttle ceiling, not the pool size. On timeout the batch sets
  `abort_event`, so un-started workers return `batch_aborted` (transient,
  stay queued), and in-flight downloads get one per-item ceiling to land;
  their rows are kept (`batch_deadline_hit` attr). The 1800 s clamp applies
  only on Cloud Run; a local drain is bounded by
  `[misc] scraper_local_batch_deadline_seconds` (default 4 h).
- **YouTube is paced per session:** concurrency capped at 2, 5 s between
  items per worker (held inside the throttle slot), batches capped at 250
  (`BaseScraper.max_batch_size`, honoured by both `scraper_loop_from_list`
  and `run_queue_scraper`), each overridable under `[misc]`.

## Consequences

- Behaviour and keys are documented in
  [pipeline.md](../pipeline.md#2-scraping-fypscrape) and
  [configuration.md](../configuration.md).
- A queue of nothing but media retries is homogeneous by construction; the
  follow-up is [decision 0024](0024-corroborated-permanent-verdicts.md).
