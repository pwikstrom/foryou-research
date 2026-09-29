"""The one table of background workers and everything each one is wired to.

Every worker is a ``web_interface/workers/run_<name>.py`` module with a
``run_<name>(reporter, task_args)`` entry point. It runs two ways:

* **locally** — ``process_manager.start_process`` spawns the module as a
  subprocess, ``python -m <module>`` (:func:`worker_module`);
* **on Cloud Run** — ``start_process`` dispatches a Cloud Task, and the task
  runner's ``/internal/run-task/<name>`` calls the entry point
  (:func:`load_task_functions`).

Everything the two paths need to know about a worker — its module, entry
point, Cloud Tasks dispatch deadline, whether the queue may retry it, and
which launch surfaces offer it — is declared once, in :data:`WORKERS`. The
registries that used to repeat these facts in five modules are derived from it.
Guard: ``tests/unit/test_worker_registry.py``.

Import-light by design: this module never imports a worker module at load time.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass

from fyp.scrape import scrape_queues

# Cloud Tasks rejects any HTTP-target dispatchDeadline outside [15s, 30m] with
# a 400 at task-creation time — the task is never queued at all, so a worker
# given 3600s fails every dispatch. process_manager._dispatch_cloud_task clamps to this as a last
# line of defence; no deadline below may ever need it.
CLOUD_TASKS_MAX_DISPATCH_DEADLINE = 1800


@dataclass(frozen=True)
class WorkerSpec:
    """How one background worker is launched and dispatched.

    Attributes:
        name: Process name (the status key, the Cloud Task name, the UI key).
        module: Dotted path of the worker's ``run_<name>`` module.
        deadline: Cloud Tasks dispatch deadline in seconds, or None for the
            queue's 600s default. A property of the worker, not of whoever
            launches it (see ``process_manager.dispatch_deadline_for``).
        retry_safe: The Cloud Tasks queue may retry a failed attempt (the
            app returns 503). Only pure recomputations qualify — see the
            rationale above :data:`WORKERS`.
        tracked: Has a slot in ``process_manager.processes`` (local status,
            logs, the UI's worker cards).
        generic_start: Startable through the generic ``POST /api/start/<name>``
            card endpoint; the rest have dedicated endpoints.
        pipeline_step: A step the local refresh-run driver spawns
            (``process_manager.local_pipeline_module_map``).
    """

    name: str
    module: str
    deadline: int | None = None
    retry_safe: bool = False
    tracked: bool = True
    generic_start: bool = False
    pipeline_step: bool = False

    @property
    def function_name(self) -> str:
        """Entry-point function name: the module's own name (``run_<name>``)."""
        return self.module.rsplit(".", 1)[1]


def _w(name: str, **kwargs) -> WorkerSpec:
    return WorkerSpec(name=name, module=f"web_interface.workers.run_{name}", **kwargs)


# Deadlines: corpus-scale sweeps run well past Cloud Tasks' 600s default.
# pca_refresh regenerates every study's recoded frame + the group-stats sweep
# (~26 min at 12 studies); recode_refresh_studies is ~7 min. The self-chaining
# workers (scrapers, annotators, sessions / timelines / embeddings refresh) pass
# the same deadline to each next link via deadline_for(); a batch link can
# exceed even 1800s (44 min observed), so sessions_refresh's initial link is
# setup-only and its links claim their successor via CAS before chaining
# (run_sessions_refresh._claim_chain_dispatch). Every refresh-graph step needs
# an explicit deadline; none may rely on the default. A consolidation is
# normally ~2 min, but a force rebuild and the weekly shadow verification are
# not (~13 min; the shadow check has run 772-816s, answering after a 600s queue
# deadline had already given up and re-delivered). Big annotator batches would
# need more than 3600s, which Cloud Tasks rejects; the ceiling is its deadline.
# tests/unit/test_dispatch_deadlines.py pins these against the workers.
#
# Retry safety: tasks the QUEUE may retry after a failed attempt are pure
# recomputations that rewrite their artifacts from source data, so a partial
# run leaves nothing to reconcile. Everything else deliberately stays
# single-attempt, and its failure goes straight to the task-failures ledger:
#   queue_scraper_* / queue_annotator  — the queue prune is the claim; a retry
#       either re-scrapes/re-annotates (real money) or loses the batch, and
#       circuit-breaker / permanent-storm aborts must never be retried.
#   queue_annotator_batch              — a retried submit could submit (and
#       pay for) the same Gemini batch job twice.
#   consolidate_enrichment             — a retry would double-fire the
#       downstream refresh pipeline.
#   collection_delete                  — destructive and partially-applied.
#   ingest_refresh                     — ledger-guarded but partial writes.
#   embeddings_refresh                 — NOT idempotent: shards are uuid-named
#       appends, so a retried live link would write a duplicate shard
#       (a twin shard); a retry would also re-spend
#       embedding credits.
#   ab_eval                            — has its own 409 concurrency gate.
#   ops_report                         — a retry would re-send the email.
# enrichment_supervisor IS safe: every tick re-reads the queues from scratch and
# dispatches at most one worker, which start_process refuses if already running.
_MAX = CLOUD_TASKS_MAX_DISPATCH_DEADLINE


def _scraper_specs() -> list[WorkerSpec]:
    """One scraper worker per platform in the scrape contract (queue_scraper_<p>)."""
    return [
        WorkerSpec(
            name=f"queue_scraper_{platform}",
            module="web_interface.workers.run_queue_scraper",
            deadline=_MAX,
            generic_start=True,
        )
        for platform in scrape_queues.registered_platforms()
    ]


WORKERS: dict[str, WorkerSpec] = {
    spec.name: spec
    for spec in [
        *_scraper_specs(),
        _w("queue_annotator", deadline=_MAX, generic_start=True),
        _w("queue_annotator_batch", deadline=_MAX, generic_start=True),
        _w(
            "meta_refresh_groups",
            deadline=_MAX,
            retry_safe=True,
            generic_start=True,
            pipeline_step=True,
        ),
        _w(
            "timelines_refresh",
            deadline=_MAX,
            retry_safe=True,
            generic_start=True,
            pipeline_step=True,
        ),
        _w(
            "recode_refresh_studies",
            deadline=_MAX,
            retry_safe=True,
            generic_start=True,
            pipeline_step=True,
        ),
        _w("pca_refresh", deadline=_MAX, retry_safe=True, generic_start=True, pipeline_step=True),
        _w("consolidate_enrichment", deadline=_MAX, generic_start=True),
        _w("study_refresh", retry_safe=True),
        _w("ingest_refresh"),
        _w("aio_fetch", retry_safe=True),
        _w("collection_metadata_refresh", retry_safe=True),
        _w("collection_delete"),
        _w("sequence_refresh", retry_safe=True),
        _w(
            "sessions_refresh",
            deadline=_MAX,
            retry_safe=True,
            generic_start=True,
            pipeline_step=True,
        ),
        _w("embeddings_refresh", deadline=_MAX, generic_start=True, pipeline_step=True),
        _w(
            "video_map_refresh",
            deadline=_MAX,
            retry_safe=True,
            generic_start=True,
            pipeline_step=True,
        ),
        _w("retokenise_hashtags", retry_safe=True),
        _w("ab_eval"),
        _w("ops_report"),
        _w("enrichment_supervisor", retry_safe=True),
        # Dispatchable (an admin benchmark) but not a UI worker.
        _w("benchmark_parquet_read", retry_safe=True, tracked=False),
    ]
}

# Cloud Task names that resolve to a worker without being one. The bare
# 'queue_scraper' predates the per-platform split: a chain dispatched before it
# still runs (the worker defaults to the contract's default platform). It is
# never dispatched afresh, so it is not in WORKERS.
_TASK_ALIASES: dict[str, str] = {"queue_scraper": "web_interface.workers.run_queue_scraper"}

SCRAPER_PROCESS_NAMES: list[str] = [n for n in WORKERS if n.startswith("queue_scraper_")]


def deadline_for(name: str) -> int | None:
    """Cloud Tasks dispatch deadline for task ``name`` (None: the 600s default).

    The one source for every dispatch: the initial one (start_process, the
    refresh pipeline) and each self-chaining worker's next link.
    """
    spec = WORKERS.get(name)
    if spec is not None:
        return spec.deadline
    if name.startswith("queue_scraper_"):
        return CLOUD_TASKS_MAX_DISPATCH_DEADLINE
    return None


def worker_module(name: str) -> str:
    """Module that local mode runs, ``python -m``, for worker ``name``. KeyError if unknown."""
    return WORKERS[name].module


def load_task_functions() -> dict[str, Callable]:
    """Import every worker module and return ``{task name: entry point}``.

    Used by the Cloud Tasks runtime; imports all workers at once, as the
    runtime always has, so no request pays for a cold import mid-flight.
    """
    functions: dict[str, Callable] = {}
    for spec in WORKERS.values():
        functions[spec.name] = getattr(importlib.import_module(spec.module), spec.function_name)
    for alias, module in _TASK_ALIASES.items():
        functions[alias] = getattr(importlib.import_module(module), module.rsplit(".", 1)[1])
    return functions
