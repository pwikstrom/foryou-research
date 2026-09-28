# 0014. Refresh runs: one dependency registry, pruning on stated no-change, consolidation scope wins

Date: 2026-09-03

## Context

The downstream refresh after a consolidation (embeddings → video map → study
recode → metadata / correlations / timelines / sessions) was wired through
four separate literals of the step graph, kept in sync by comment, and could
only be started by a consolidation. Every step ran whether or not its inputs
had changed.

Two follow-ups on 2026-09-04:

- **Scope.** When the semantic map moved videos between niches, the run was
  briefly widened to every study. A warm-started map rebuild moves a couple
  of percent of the corpus on almost every run, so the widening turned every
  run into a full refresh, and it was reverted.
- **Deferred refreshes.** A consolidation started with "Refresh caches
  afterwards" unticked records its impact as deferred. The enrichment
  supervisor, which defers its own refreshes the same way, spent an
  operator's manual deferral 3.5 minutes after it was created, with no
  browser poll watching.

## Decision

- One registry, `web_interface/services/refresh_pipeline.py`, declares the
  graph and the predicates that decide, at each completion, whether the next
  step has anything to do. Any step's card can start a run, which plans the
  same cascade of dependents.
- A step is pruned only on a positive statement of no change from upstream
  (`map_niche_changed` / `map_cold_start`, `studies_changed`,
  `embeddings_embedded_run`); a missing signal is unknown, and unknown always
  runs.
- A run is scoped to exactly what the consolidation touched, even when the
  map moved videos between niches. The only unscoped run is one started from
  the Semantic Map card, which has no consolidation impact to scope by.
- Deferred debt records whose it is (`from_plan`). The supervisor's own
  consolidations are tagged `plan_deferred` and its finalize spends only
  those; an operator's deferral waits for "Refresh All Affected".

## Consequences

- The run is recorded in `process_stats["refresh_pipeline"]` and drawn as a
  Gantt on Data Pipeline → Dataset Assembly, each step stating the scope it
  was dispatched with and why a skipped step was skipped.
- Described in [web_interface.md](../web_interface.md#process-ui-data-pipeline--dataset-assembly)
  and [pipeline.md](../pipeline.md#5-analysis--studies).
