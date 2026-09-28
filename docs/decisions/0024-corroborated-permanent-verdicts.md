# 0024. Corroborated permanent verdicts

Date: 2026-09-21

## Context

The storm guards ([decision 0003](0003-scraper-storm-guards.md)) assume that a
healthy queue produces heterogeneous outcomes, and read a run of identical
verdicts as a broken session. A queue made only of retries never is
heterogeneous: retrying only the failures distils it down to items that fail.
After the media-leg rule of [decision 0023](0023-media-leg-failures-batch-deadline-youtube-pacing.md),
a YouTube queue of genuinely dead videos tripped the permanent-storm guard on
every batch and could never drain.

The YouTube scraper also saved a video with no platform record as an empty
placeholder row instead of a failure.

## Decision

- A scraper may mark a permanent verdict **corroborated**
  (`attrs['verdict_corroborated']`, see `BaseScraper.fetch`) when it rests on
  per-item evidence independent of the error text. A corroborated verdict
  neither extends nor resets a storm run, and is pruned even when the guard
  trips.
- YouTube corroborates two cases, both read from the metadata leg, which adds
  the tv player client (the only one that states why a video will not play)
  and captures the reason yt-dlp otherwise swallows under
  `ignore_no_formats_error`:
  - the platform has no record of the video (no channel, no view count, no
    duration) **and** the stated reason is itself a removal. A video with no
    record is a failure, never an empty placeholder row;
  - the record is intact but YouTube refuses to play it here, naming a region
    whitelist or a rights claim. Its metadata is scraped, the media leg is
    skipped, and the id leaves the queue with its metadata-only row standing.
- A bare "Video unavailable" with the record intact is never corroborated:
  that is exactly what a throttled session returns.

## Consequences

- The same change introduced the zero-progress retry budget
  (`MAX_ZERO_PROGRESS_STRIKES`) and `BaseScraper.residential_ip_only`, under
  which Cloud Run declines to run the Instagram and YouTube scrapers.
- Documented in [pipeline.md](../pipeline.md#2-scraping-fypscrape).
