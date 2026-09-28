# 0018. Enrichment slices: a size floor, whole sessions, and a derived sampling density

Date: 2026-09-08

## Context

The enrichment planner cuts each cycle's slice of a collection into the
scrape queue. Four problems showed up in the first week of use:

- **Shrinking slices (2026-09-08).** A cut sized to exactly the remaining
  shortfall came back short (scrape and annotation failures), so the next cut
  was smaller still. Slices shrank geometrically toward one-video cycles
  25 s apart — and a cycle's fixed cost is the same for one video as for two
  hundred.
- **Scattered items (2026-09-09).** Within a capped day the random daily
  sample took single items, which never add up to a viewing session anyone
  can analyse. Analysable sessions existed only inside the deep-dive window.
- **A second quantity knob (2026-09-09).** The random daily sample had its
  own "max days / month" setting, which could sit below what the annotation
  target needed, so the target was not reachable.
- **An estimate that disagreed with the planner (2026-09-09).** The panel's
  time and coverage estimate used its own rules; checking it against a Python
  re-implementation on a live collection showed it drifting from what the
  planner would actually cut.

## Decision

- A slice is never smaller than `MIN_CYCLE_ITEMS` (200) while the plan still
  needs anything. The handoff annotates whatever the plan's own slice
  scraped, so a plan may overshoot its target by at most that floor.
- Within a cut day (the spread's capped days and the deep dive's partial last
  day) the cutter takes **whole viewing sessions first** (`_pick_in_day`):
  the day's candidate sessions, including the one that crosses the cap, then
  single items up to the cap.
- How many days a month the spread samples is **derived, not set**
  (`collection_enrichment.spread_days_per_month`): the fewest days, the same
  in every month still ahead of the spread's cursor, whose capped videos
  cover the spread's share of what the target still needs. The target is the
  only quantity knob.
- The modal's estimate mirrors the planner's day pick: `progress()` ships
  each day's place in its month's salted draw (`daily.draw`), charges
  everything already scraped on a day against the cap as the planner's quota
  does, and uses the measured `last_yield` and the burnt-free backlog
  (`unique_awaiting`).

## Consequences

- A run's very last session can end part-annotated, exactly like its partial
  last day; a later target raise completes it first.
- The planner's rules are documented in
  [pipeline.md](../pipeline.md#automatic-per-collection-enrichment).
