# 0028. Off-feed signals annotate plays; episodes are still detected on all plays

Date: 2026-10-13

## Context

A watch history lists every video the donor played, whether the For You
feed served it or the donor opened it from a creator's profile, a search
result or a shared link, and it does not say which. Runs of similar videos
found by the Sessions tab's detector therefore mix two things: runs the feed
assembled, and runs the donor assembled by browsing one creator's page.
Analyses built on the Hub had begun to separate the two with their own copy
of a heuristic, so the Hub and those analyses could disagree.

## Decision

- **One implementation, in the Hub.** `fyp/analysis/sessions/offfeed.py`
  marks each play with a bitmask of three signals:
  - a search or a follow by the donor in the `offfeed_window_s` seconds
    before the play (default 120, inclusive at both ends; `followed_by`, another
    account's action, never counts);
  - the same creator as the previous play in the same session (TikTok states
    the feed "generally won't show two videos in a row" from one creator);
  - membership of a run of at least `offfeed_min_run` (default 3) plays by one
    creator whose posting times are strictly increasing or decreasing, which
    is the order of a profile page.

  The module is pure numpy/pandas, so analysis code outside the Hub imports
  it instead of copying it.
- **Annotate, don't filter.** The sessions build stores the bitmask on
  `sessions_plays.parquet` (`offfeed_signals`) and aggregates it on episodes
  (`n_offfeed`, `offfeed_share`, `member_offfeed`) and sessions. Focused
  session episodes are still detected on all plays; the Sessions tab shows how
  many of an episode's videos are likely off-feed and by which signal. An
  analysis that wants feed-only episodes removes the flagged plays and runs
  the detector again.
- **Undercount, never overclaim.** The creator signals need scraped creator
  handles and posting times; an unknown value never matches. The UI says
  "likely off-feed" and lists the signals rather than naming a source.

## Consequences

- A change to `offfeed_window_s` or `offfeed_min_run` changes the build
  parameters recorded in `sessions_meta.json`, so the next refresh is a full
  rebuild. The signals are baked in at build time.
- Signals (b) and (c) can change when a video is scraped without being
  annotated. The refresh planner tracks annotation and embedding changes, not
  scrape-only changes, so the flags of such a session stay as they were until
  its collection is next rebuilt.
- The deploy that introduced the columns changes the schema of three
  artifacts; the planner compares every artifact's columns and escalates to a
  full rebuild.
