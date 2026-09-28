# 0015. The enrichment loop and the shared queues

Date: 2026-09-05

## Context

The automatic per-collection enrichment loop (the supervisor,
`web_interface/run_enrichment_supervisor.py` +
`web_interface/services/collection_enrichment.py`) drives the same global
scrape queues, annotation queue and queue workers that people use by hand.
Its first days in use (2026-09-04) exposed three ways the two collided, and a
fourth surfaced on 2026-09-09:

1. **The drain step serves the platform queue, whoever filled it.** `_drain`
   filters by platform, not by who queued the items, so arming a plan
   adopted a colleague's *Build scrape queue* and ran it first.
2. **A stale stall counter parked a healthy plan.** The handoff earlier in a
   boundary tick resets the plan's `stall_count` and prunes its `in_flight`
   list; `_plan` then wrote back a snapshot of the entry taken before the
   handoff, restoring the stale values, and a healthy plan was parked on its
   fourth cycle.
3. **Results of a job the loop started could be left unconsolidated.** A
   plan parked or finished while its job was still running left results that
   no later tick would fold in.
4. **A plan closed before its last batch was annotated** (2026-09-09). The
   planner closed a plan with nothing more to scrape in the same tick that
   handed its last batch to the annotator, so the history read "Idle" before
   "Annotator started" and the panel said "Idle · annotating now" for the
   whole batch.

## Decision

1. Before *Arm*, *Resume* or *Run a cycle now*, the Edit Collections panel
   asks (`queue_preview`) when a queue holds videos that are not the
   collection's own, offering to drain them first or empty them.
2. `_plan` reloads the plan entry immediately before its read-modify-write,
   so every productive handoff's reset of the stall counter stands.
3. A job the loop started owes a consolidation (`__meta__.settle_owed`, set
   when the loop starts a scraper or annotator, cleared when it
   consolidates). The no-plans path settles that debt before the quiet
   finalize, and a worker completion dispatches a tick while the loop owes a
   settle or its own deferred refresh (`process_routes.loop_owes_work`), not
   only while a plan is armed. The Dataset Assembly banner reads the same
   flag and says the loop has the consolidation in hand.
4. A plan with nothing more to scrape stays Running while its own videos are
   still queued for, or inside, an annotation job (`entry["finishing"]`, one
   `plan.finishing` history line, bounded by `FINISHING_MAX_H`). The owed
   consolidation's completion ticks the loop, the pending count reaches zero,
   and the plan closes (`plan.done`) with the quiet finalize in the same
   tick.

Every decision the loop makes is written to the enrichment history
(`services/enrichment_journal.py`, `cache/enrichment_journal.json`, a bounded
ring), including queue builds, empties and drains with the split between the
armed plans' own slices and everything else.

## Consequences

- The loop is documented in
  [pipeline.md](../pipeline.md#automatic-per-collection-enrichment).
- The general lesson of fact 2: a read-modify-write of shared state reloads
  the state immediately before the write, not earlier in the tick.
