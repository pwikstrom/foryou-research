# 0025. Instagram: anonymous first, the logged-in session spent sparingly

Date: 2026-09-23

## Context

Instagram extraction has two paths, and each has broken on its own:

- **2026-07:** attaching the session cookies broke every extraction. yt-dlp
  routes every cookie-bearing extraction through Instagram's authenticated
  web API, which at times answered 404 for web sessions.
- **2026-09:** anonymous extraction alone left 65 of 75 queued posts
  unfetchable, because Instagram hides many posts from logged-out viewers.

Once cookies were used for hidden posts, a third failure appeared on
2026-09-23: Instagram logs a session out when it is used too freely — after
~14 gated posts in about a minute.

## Decision

- Go **anonymous first**, and retry with the session cookies only for a post
  Instagram hides from logged-out viewers. This keeps the account's
  footprint to the posts that need it and survives either path breaking.
- Space logged-in requests `[misc] scraper_instagram_auth_interval` seconds
  apart across threads (default 20).
- The media leg downloads from the info dict the metadata leg already
  extracted, so a gated post costs one logged-in call, not two.
- A logged-out session shows up as Instagram's login page where the API's
  JSON should be. The scraper classifies it `session_expired` and makes no
  further logged-in request that run; the orchestrator stops the run without
  charging any retry budget and raises a scraper alert asking for a fresh
  login in Chrome.

## Consequences

- Follow-gated posts stay permanently `private`; the donated enrichment seed
  still surfaces their caption and author.
- Documented in [pipeline.md](../pipeline.md#2-scraping-fypscrape) and the
  `fyp/scrape/instagram_dl.py` module docstring.
