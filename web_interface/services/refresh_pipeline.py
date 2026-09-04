"""The refresh pipeline: one dependency registry, one run record, one planner.

A *refresh run* is what happens when a step of the dataset-assembly pipeline
finishes and the steps that depend on its data are started in turn. Any step can
be the **origin** of a run — a consolidation, a card the operator clicked, or
the "Refresh All Affected" button — and every run is planned up front so the
Dataset Assembly page can chart it the same way regardless of where it started.

Three things live here:

* **The registry** (:data:`STEPS`) — the single encoding of what depends on
  what. It replaced four literals that had to be kept in sync by comment
  (``_PIPELINE_STEPS_ORDER``, ``PIPELINE_STEPS_ORDER``, ``_FORK_PARENT`` /
  ``_FORK_LEAF_TASKS`` and the video-map worker's own downstream list), plus the
  mirror in ``data_management.js`` which now reads ``window.PIPELINE_REGISTRY``.
* **The run record** — ``process_stats["refresh_pipeline"]``, its own top-level
  key so it never collides with the per-step entries the task runner writes.
* **The planner** — :func:`plan_run` computes a run's shape from its origin;
  :func:`next_actions` decides, each time a step finishes, what to dispatch
  next and which remaining steps to prune.

Pruning is the "intelligent" half: a downstream step is dispatched only when an
upstream step reports that something actually changed (new embeddings written,
videos that moved niche, study datasets that were rebuilt). The rule
throughout is **prune only on a positive statement of no change** — a missing
signal means "unknown", and unknown always runs. A wasted refresh costs
minutes; a skipped one leaves a stale cache that nothing will notice.

Scheduling stays the out-tree the Cloud Tasks machinery already implements: a
linear spine (embeddings → video_map → recode) and one fan-out to the terminal
leaves, so there is no join to build. Multi-parent steps (timelines and
sessions both read the niche map) are expressed in their predicates, not in the
schedule.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Where the run record lives inside process_stats.json.
RUN_KEY = "refresh_pipeline"

#: Step tiers. ``origin_only`` steps can start a run but are never dispatched
#: as somebody else's downstream step (a consolidation is operator- or
#: supervisor-driven, never a consequence of another refresh).
TIER_ORIGIN_ONLY = "origin_only"
TIER_SPINE = "spine"
TIER_LEAF = "leaf"


@dataclass(frozen=True)
class Need:
    """A predicate's verdict: dispatch with these task_args, or prune."""

    task_args: dict | None
    reason: str = ""

    @property
    def run(self) -> bool:
        return self.task_args is not None


def _run(task_args: dict | None = None, reason: str = "") -> Need:
    return Need(dict(task_args or {}), reason)


def _skip(reason: str) -> Need:
    return Need(None, reason)


@dataclass(frozen=True)
class Step:
    """One node of the refresh graph.

    Args:
        name: Worker/process name — also the Cloud Task name and status key.
        label: Long label for the Gantt row ("Rebuilding semantic map").
        short_label: Short label for prose and the impact panel.
        parents: The steps whose OUTPUT this step reads. Documentation for the
            reader and the invariant the tests pin; the predicates are what
            actually gate dispatch.
        tier: ``origin_only``, ``spine`` or ``leaf`` — the schedule's shape.
        needs: Given a :class:`RunContext`, the task_args to dispatch with, or
            a prune verdict carrying the reason shown in the chart.
        self_chaining: The worker dispatches itself repeatedly and only its
            last link reaches the advance logic.
        stop_boundary: The unit of work this worker finishes before it acts on a
            graceful stop — "batch", "study", "collection". ``None`` means it
            never reads the cancel sentinel, so a graceful stop would do nothing
            and the UI must not offer one.
    """

    name: str
    label: str
    short_label: str
    parents: tuple[str, ...]
    tier: str
    needs: Callable[[RunContext], Need]
    self_chaining: bool = False
    stop_boundary: str | None = None


# --- Predicates ------------------------------------------------------------
#
# Each reads the accumulated results of the steps that already ran in THIS run
# (ctx.result(step)) plus the consolidation impact, and answers "is there new
# data for me?". They never look at the clock or at global staleness — that is
# the workers' own business (sessions_refresh re-checks its fingerprints even
# when we dispatch it).


def _needs_embeddings(ctx: RunContext) -> Need:
    if not ctx.ran("consolidate_enrichment"):
        return _skip("no consolidation in this run")
    new_annotations = ctx.impact_int("new_annotation_item_count")
    if new_annotations is not None and new_annotations <= 0:
        return _skip("no new annotations to embed")
    if not _embeddings_dispatchable():
        return _skip("the active embedding backend runs only on a local machine")
    return _run({})


def _needs_video_map(ctx: RunContext) -> Need:
    if not ctx.ran("embeddings_refresh"):
        return _skip("semantic embeddings did not run")
    embedded = ctx.result_int("embeddings_refresh", "embeddings_embedded_run")
    if embedded is not None and embedded <= 0:
        return _skip("no new embeddings were written")
    # Empty task_args on purpose: reset_labels stays False so niche names carry
    # over from the previous build.
    return _run({})


def _needs_recode(ctx: RunContext) -> Need:
    if ctx.map_moved():
        return _run({}, ctx.map_reason())
    if ctx.ran("consolidate_enrichment"):
        studies = ctx.impact_list("affected_study_names")
        if studies is None:
            # Consolidated but the impact is unreadable — refresh rather than guess.
            return _run({}, "the consolidation reported no impact detail")
        if studies:
            return _run({"studies": ",".join(studies)},
                        f"{len(studies)} study(ies) affected by the consolidation")
        return _skip("no study was affected")
    return _skip(ctx.no_change_reason())


def _needs_study_consumer(ctx: RunContext) -> Need:
    """meta_refresh_groups and pca_refresh: scoped to the studies recode rebuilt."""
    if not ctx.ran("recode_refresh_studies"):
        return _skip("study definitions did not run")
    changed = ctx.result_list("recode_refresh_studies", "studies_changed")
    if changed is None:
        return _run({}, "study definitions did not report which studies changed")
    if not changed:
        return _skip("no study dataset changed")
    return _run({"studies": ",".join(changed)}, f"{len(changed)} study dataset(s) changed")


def _needs_timelines(ctx: RunContext) -> Need:
    # Timelines joins the niche columns (run_timelines_refresh -> new_merge ->
    # _join_niche_columns), so a map that moved invalidates every collection's
    # cache, not only the ones the consolidation touched.
    if ctx.map_moved():
        return _run({}, ctx.map_reason())
    if ctx.ran("consolidate_enrichment"):
        collections = ctx.impact_list("affected_collection_ids")
        if collections is None:
            return _run({}, "the consolidation reported no impact detail")
        if collections:
            return _run({"collections": ",".join(collections)},
                        f"{len(collections)} collection(s) affected by the consolidation")
        return _skip("no collection was affected")
    return _skip(ctx.no_change_reason())


def _needs_sessions(ctx: RunContext) -> Need:
    # stale_only lets the worker re-check its own fingerprints and no-op; this
    # predicate only decides whether asking is worth a task at all.
    args = {"stale_only": True, "skip_if_busy": True}
    if ctx.map_moved():
        return _run(args, ctx.map_reason())
    embedded = ctx.result_int("embeddings_refresh", "embeddings_embedded_run")
    if ctx.ran("embeddings_refresh") and (embedded is None or embedded > 0):
        return _run(args, "new embeddings were written")
    if ctx.ran("consolidate_enrichment"):
        collections = ctx.impact_list("affected_collection_ids")
        new_annotations = ctx.impact_int("new_annotation_item_count")
        if collections is None or new_annotations is None:
            return _run(args, "the consolidation reported no impact detail")
        if collections or new_annotations > 0:
            return _run(args, "collections or annotations moved")
        return _skip("no collection or annotation moved")
    return _skip(ctx.no_change_reason())


def _embeddings_dispatchable() -> bool:
    """Whether the active embedding backend can serve a Cloud Task.

    On Cloud Run the pipeline dispatches tasks directly, bypassing the
    ``start_process`` guard, so a local-only backend would produce a task that
    can only fail. Skipping embeddings also skips the map: re-clustering the
    same vectors would change nothing.
    """
    if not os.environ.get("K_SERVICE"):
        return True
    try:
        from fyp.analysis.embedding_backends import active_backend_name, get_backend

        return bool(get_backend(active_backend_name()).cloud_run_capable)
    except Exception:
        return False


STEPS: list[Step] = [
    Step("consolidate_enrichment", "Consolidating enrichment data",
         "Consolidate enrichment data", (), TIER_ORIGIN_ONLY,
         lambda ctx: _skip("consolidation is never a downstream step")),
    Step("embeddings_refresh", "Refreshing semantic embeddings",
         "Semantic embeddings", ("consolidate_enrichment",), TIER_SPINE,
         _needs_embeddings, self_chaining=True, stop_boundary="batch"),
    Step("video_map_refresh", "Rebuilding semantic map",
         "Semantic map", ("embeddings_refresh",), TIER_SPINE,
         _needs_video_map),
    Step("recode_refresh_studies", "Refreshing study definitions",
         "Study definitions", ("consolidate_enrichment", "video_map_refresh"),
         TIER_SPINE, _needs_recode, stop_boundary="study"),
    Step("meta_refresh_groups", "Refreshing explore metadata",
         "Explore metadata", ("recode_refresh_studies",), TIER_LEAF,
         _needs_study_consumer, stop_boundary="study"),
    Step("pca_refresh", "Refreshing correlations",
         "Correlations", ("recode_refresh_studies",), TIER_LEAF,
         _needs_study_consumer, stop_boundary="study"),
    Step("timelines_refresh", "Refreshing timelines",
         "Timelines", ("consolidate_enrichment", "video_map_refresh"),
         TIER_LEAF, _needs_timelines, self_chaining=True,
         stop_boundary="collection"),
    Step("sessions_refresh", "Rebuilding session index",
         "Sessions",
         ("consolidate_enrichment", "embeddings_refresh", "video_map_refresh"),
         TIER_LEAF, _needs_sessions, self_chaining=True,
         stop_boundary="batch"),
]

BY_NAME: dict[str, Step] = {s.name: s for s in STEPS}
#: Every step, origin included, in dependency order.
STEP_ORDER: list[str] = [s.name for s in STEPS]
#: The dispatchable steps — what used to be ``PIPELINE_STEPS_ORDER``.
DOWNSTREAM_ORDER: list[str] = [s.name for s in STEPS if s.tier != TIER_ORIGIN_ONLY]
SPINE: tuple[str, ...] = tuple(s.name for s in STEPS if s.tier == TIER_SPINE)
LEAVES: tuple[str, ...] = tuple(s.name for s in STEPS if s.tier == TIER_LEAF)
LABELS: dict[str, str] = {s.name: s.label for s in STEPS}
SHORT_LABELS: dict[str, str] = {s.name: s.short_label for s in STEPS}


def registry_for_js() -> dict:
    """The registry as the page needs it (injected as ``window.PIPELINE_REGISTRY``)."""
    return {
        "order": list(STEP_ORDER),
        "downstream": list(DOWNSTREAM_ORDER),
        "labels": dict(LABELS),
        "short_labels": dict(SHORT_LABELS),
        "spine": list(SPINE),
        "leaves": list(LEAVES),
        # What each step would set off, so the start dialog can tell the
        # operator what a click actually commits to before they confirm.
        # Best-effort by definition: the run prunes a step whose upstream turns
        # out to have changed nothing, which the dialog says.
        "dependents": {s.name: dependents_of(s.name) for s in STEPS},
        # What a graceful stop would wait for, per worker. Absent = the worker
        # never reads the cancel sentinel, so the UI offers no graceful stop
        # rather than a button that silently does nothing.
        "stop_boundaries": {s.name: s.stop_boundary for s in STEPS if s.stop_boundary},
    }


def dependents_of(origin: str) -> list[str]:
    """Every step that transitively reads ``origin``'s output, in dispatch order.

    Args:
        origin: The step a run starts from.

    Returns:
        Dependent step names ordered by :data:`STEP_ORDER`. Empty for a leaf.
    """
    reached: set[str] = set()
    frontier = {origin}
    while frontier:
        nxt: set[str] = set()
        for step in STEPS:
            if step.name in reached or step.name == origin:
                continue
            if frontier & set(step.parents):
                reached.add(step.name)
                nxt.add(step.name)
        frontier = nxt
    return [n for n in STEP_ORDER if n in reached]


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


@dataclass
class RunContext:
    """What the predicates get to look at.

    ``results`` holds the ``process_stats`` entry of every step this run
    dispatched that has since reached a terminal state inside the run's window.
    Work that ran outside the run (a study save's sessions chain, a hand-started
    worker) is deliberately absent: it must not make a downstream step look
    needed, just as it is not drawn into the chart.
    """

    record: dict
    results: dict[str, dict] = field(default_factory=dict)
    impact: dict | None = None
    #: Steps whose work this run inherits instead of doing — the consolidation
    #: a replayed refresh is catching up on. They count as having run.
    inherited: set[str] = field(default_factory=set)

    def ran(self, step: str) -> bool:
        # Inherited steps ran before this run started (a replayed consolidation),
        # so they have no per-step results here — the impact on the record is
        # what they contributed, and that is what the predicates read next.
        if step in self.inherited:
            return True
        state = (self.record.get("steps", {}).get(step) or {}).get("state")
        return state in ("origin", "dispatched") and step in self.results

    def result(self, step: str) -> dict:
        return self.results.get(step) or {}

    def result_int(self, step: str, key: str) -> int | None:
        """An integer change signal, or None when the step did not report it."""
        value = self.result(step).get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def result_list(self, step: str, key: str) -> list | None:
        value = self.result(step).get(key)
        if value is None:
            return None
        return list(value) if isinstance(value, (list, tuple, set)) else None

    def impact_int(self, key: str) -> int | None:
        if not isinstance(self.impact, dict):
            return None
        value = self.impact.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def impact_list(self, key: str) -> list | None:
        if not isinstance(self.impact, dict):
            return None
        value = self.impact.get(key)
        if value is None:
            return None
        return list(value) if isinstance(value, (list, tuple, set)) else None

    def map_moved(self) -> bool:
        """True when the niche map was rebuilt into a different partition.

        A cold start (or a build that did not report) counts as moved — every
        video may have changed niche.
        """
        if not self.ran("video_map_refresh"):
            return False
        if self.result("video_map_refresh").get("map_cold_start"):
            return True
        changed = self.result_int("video_map_refresh", "map_niche_changed")
        return changed is None or changed > 0

    def map_reason(self) -> str:
        if self.result("video_map_refresh").get("map_cold_start"):
            return "the semantic map was rebuilt from scratch"
        changed = self.result_int("video_map_refresh", "map_niche_changed")
        if changed is None:
            return "the semantic map was rebuilt"
        return f"{changed:,} video(s) changed niche"

    def no_change_reason(self) -> str:
        """Why a step with no fresh input is being pruned."""
        if self.ran("video_map_refresh") and not self.map_moved():
            return "no video changed niche"
        if self.ran("embeddings_refresh"):
            embedded = self.result_int("embeddings_refresh", "embeddings_embedded_run")
            if embedded is not None and embedded <= 0:
                return "no new embeddings were written"
        return "nothing upstream changed"


# ---------------------------------------------------------------------------
# The run record
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def load_run(*, reload: bool = True) -> dict | None:
    """The current run record, or None when no run has ever been recorded.

    Args:
        reload: Re-read process_stats.json first. Always do this before a
            mutation — a stale in-memory copy has erased a fresh flag before.
    """
    from web_interface.process_manager import load_process_stats, process_stats

    if reload:
        load_process_stats()
    record = process_stats.get(RUN_KEY)
    return dict(record) if isinstance(record, dict) else None


def run_in_flight() -> bool:
    """Whether a refresh run is currently occupying the pipeline."""
    record = load_run()
    return bool(record and record.get("in_flight"))


def seed_run(record: dict) -> dict:
    """Persist a freshly planned run, replacing whatever came before."""
    from web_interface.process_manager import (
        load_process_stats,
        process_stats,
        save_process_stats,
    )

    load_process_stats()
    record = dict(record)
    record["updated_ts"] = _now()
    process_stats[RUN_KEY] = record
    _mirror_in_flight(bool(record.get("in_flight")))
    save_process_stats()
    return record


def mutate_run(fn: Callable[[dict], bool | None]) -> dict | None:
    """Apply ``fn`` to the run record under a reload-mutate-save window.

    ``fn`` may return ``False`` to abandon the write — used when a mutation
    turns out to belong to a run that has since been replaced.

    ``save_process_stats`` merges only the top-level keys that changed since the
    load, so a mutation that reloads immediately before writing cannot clobber a
    concurrent write to a *different* key. Two writers of the run record itself
    would still lose one update, which is why every writer is a point where
    exactly one task is advancing the run.
    """
    from web_interface.process_manager import (
        load_process_stats,
        process_stats,
        save_process_stats,
    )

    load_process_stats()
    record = process_stats.get(RUN_KEY)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    if fn(record) is False:
        return None  # the callee decided this write does not apply
    record["updated_ts"] = _now()
    process_stats[RUN_KEY] = record
    _mirror_in_flight(bool(record.get("in_flight")))
    save_process_stats()
    return record


def _mirror_in_flight(active: bool) -> None:
    """Keep the legacy ``consolidate_enrichment.pipeline_in_flight`` alias true.

    Read outside the pipeline by the semantic-space route, the enrichment
    supervisor's hard gate and the downstream-refresh service. Caller holds the
    reload window and does the save.
    """
    from web_interface.process_manager import process_stats

    entry = dict(process_stats.get("consolidate_enrichment", {}))
    if active:
        entry["pipeline_in_flight"] = True
    else:
        entry.pop("pipeline_in_flight", None)
    process_stats["consolidate_enrichment"] = entry


def set_in_flight(active: bool) -> None:
    """Flip the run's in-flight flag (and its legacy alias)."""
    def _apply(record: dict) -> None:
        if active:
            record["in_flight"] = True
        else:
            record["in_flight"] = False
    if mutate_run(_apply) is None:
        # No run record (a legacy chain, or a run that was cleared): keep the
        # alias honest anyway so the supervisor's gate does not stick.
        from web_interface.process_manager import (
            load_process_stats,
            save_process_stats,
        )
        load_process_stats()
        _mirror_in_flight(active)
        save_process_stats()


def clear_run() -> None:
    """Drop the run record entirely (a seeded run whose dispatch then failed)."""
    from web_interface.process_manager import (
        load_process_stats,
        process_stats,
        save_process_stats,
    )

    load_process_stats()
    process_stats.pop(RUN_KEY, None)
    _mirror_in_flight(False)
    save_process_stats()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_run(origin: str, *, kind: str = "card", started_by: str = "",
             mode: str = "refresh", origin_task_args: dict | None = None,
             impact: dict | None = None, provisional: bool = False,
             origin_ran: bool = True) -> dict:
    """Plan a refresh run that starts from ``origin``.

    Every step that transitively depends on the origin is ``planned``; steps
    that precede the origin are ``upstream`` ("not part of this run"); anything
    else is ``not_planned`` ("not needed"). Nothing is dispatched here — the
    caller starts the origin, and :func:`next_actions` takes over from the first
    completion.

    Args:
        origin: The step this run starts from.
        kind: ``card``, ``consolidate``, ``armed`` or ``refresh_downstream`` —
            what started it, which decides whose card shows the summary.
        started_by: Username for attribution in the chart header.
        mode: ``refresh``, or ``consolidate_only`` when the operator ran a
            consolidation with the refresh box unticked.
        origin_task_args: The origin's own task_args (e.g. force_full_rebuild).
        impact: A known consolidation impact, for runs that start from one.
        provisional: The plan is a dispatch-time forecast (the origin has not
            run yet and may yet report a narrower scope).
        origin_ran: False when the origin step itself is not part of this run,
            as for "Refresh All Affected", which replays a past consolidation.

    Returns:
        The run record. Not persisted — call :func:`seed_run`.
    """
    planned = dependents_of(origin) if mode != "consolidate_only" else []
    origin_index = STEP_ORDER.index(origin) if origin in STEP_ORDER else -1

    steps: dict[str, dict] = {}
    for index, name in enumerate(STEP_ORDER):
        if name == origin:
            steps[name] = {"state": "origin" if origin_ran else "upstream"}
        elif name in planned:
            steps[name] = {"state": "planned"}
        elif index < origin_index:
            steps[name] = {"state": "upstream"}
        else:
            steps[name] = {"state": "not_planned"}

    # A replayed run (the deferred debt, "Refresh All Affected") does not run
    # its origin — that consolidation already happened and its impact is handed
    # to us. The row reads "upstream" because it is not part of THIS run, but
    # the predicates must still see it as having run: they gate on "did my
    # upstream produce anything", and the answer is yes, just earlier.
    inherited = [] if origin_ran else [origin]

    return {
        "run_id": new_run_id(),
        "origin": origin,
        "origin_kind": kind,
        "origin_label": SHORT_LABELS.get(origin, origin),
        "started_by": started_by or "",
        "started_ts": _now(),
        "updated_ts": _now(),
        "mode": mode,
        "provisional": bool(provisional),
        "impact": dict(impact) if impact else None,
        "inherited": inherited,
        "origin_task_args": dict(origin_task_args or {}),
        "steps": steps,
        "stage_total": stage_total(steps),
        "fork_at": None,
        "fork": None,
        "in_flight": True,
        "finished_ts": None,
        "summary": None,
        "partial": False,
        "failed_at": None,
    }


def stage_total(steps: dict) -> int:
    """Stage count for the "step 2/4" framing: tree DEPTH, not task count.

    The origin, each planned spine step, and one shared stage for however many
    leaves fan out together.
    """
    live = {"origin", "planned", "dispatched"}
    spine = [n for n in SPINE if (steps.get(n) or {}).get("state") in live]
    leaves = [n for n in LEAVES if (steps.get(n) or {}).get("state") in live]
    origin_steps = [n for n, s in steps.items() if (s or {}).get("state") == "origin"]
    depth = len(origin_steps) + len([n for n in spine if n not in origin_steps])
    return depth + (1 if [n for n in leaves if n not in origin_steps] else 0)


def build_context(record: dict) -> RunContext:
    """Gather the change signals the steps of this run have reported so far."""
    from web_interface.process_manager import process_stats

    started_dt = _parse_ts(record.get("started_ts"))
    results: dict[str, dict] = {}
    for name in STEP_ORDER:
        state = (record.get("steps", {}).get(name) or {}).get("state")
        if state not in ("origin", "dispatched"):
            continue
        entry = dict(process_stats.get(name) or {})
        end_dt = _parse_ts(entry.get("last_run_end_time"))
        if end_dt is None or (started_dt is not None and end_dt < started_dt):
            continue
        results[name] = entry

    impact = record.get("impact")
    consolidate = results.get("consolidate_enrichment") or {}
    if consolidate:
        # The worker's own union of this consolidation with any deferred debt
        # is the authoritative scope; consolidation_impact is the fallback.
        impact = consolidate.get("pipeline_impact") or consolidate.get(
            "consolidation_impact") or impact
    return RunContext(record=record, results=results,
                      impact=impact if isinstance(impact, dict) else None,
                      inherited=set(record.get("inherited") or ()))


#: What an empty task_args means for each step — i.e. what "no filter" widens to.
_UNSCOPED_MEANING = {
    "recode_refresh_studies": "all studies",
    "meta_refresh_groups": "all studies",
    "pca_refresh": "all studies",
    "timelines_refresh": "all collections",
    "sessions_refresh": "every stale collection",
}


def scope_note(step: str, task_args: dict | None) -> str:
    """How wide this dispatch actually is, in words.

    The consolidation's impact is only the FLOOR of a run's scope: a semantic
    map that moved videos between niches invalidates the niche columns of every
    study and every collection, not just the ones the consolidation touched, so
    those steps are dispatched unfiltered. Saying "8 studies affected" and then
    rebuilding 13 is correct behaviour reported dishonestly — this is what lets
    the chart state the scope it really ran with.
    """
    ta = task_args or {}
    for key, one, many in (("studies", "study", "studies"),
                           ("collections", "collection", "collections")):
        raw = ta.get(key)
        if raw:
            n = len([x for x in str(raw).split(",") if x.strip()])
            return f"{n} {one}" if n == 1 else f"{n} {many}"
    return _UNSCOPED_MEANING.get(step, "")


def next_actions(record: dict, ctx: RunContext | None = None) -> dict:
    """Decide what this run does next, given everything that has run so far.

    Walks the still-``planned`` steps in dependency order and asks each one's
    predicate whether its inputs actually moved. The first spine step that says
    yes is dispatched alone (it may change what the later steps see); when no
    spine step is left, every leaf that says yes fans out together.

    The record is NOT written here. The caller applies the returned prunes and
    the dispatch outcome in one write, so a run advances with a single
    read-modify-write.

    Args:
        record: The run record. Mutated in place only for pruning decisions, so
            that later predicates see the earlier ones.
        ctx: Prebuilt context; built from ``record`` when omitted.

    Returns:
        ``{"action": "spine"|"fork"|"finish", "step", "task_args", "leaves",
        "prunes"}`` — ``leaves`` a list of ``(name, task_args)``, ``prunes`` a
        ``{name: reason}`` map of steps this call decided to skip.
    """
    ctx = ctx or build_context(record)
    prunes: dict[str, str] = {}
    reasons: dict[str, str] = {}
    steps = record.setdefault("steps", {})

    def _state(name: str) -> str:
        return (steps.get(name) or {}).get("state") or "not_planned"

    for name in DOWNSTREAM_ORDER:
        if name not in SPINE or _state(name) != "planned":
            continue
        verdict = BY_NAME[name].needs(ctx)
        if verdict.run:
            return {"action": "spine", "step": name,
                    "task_args": verdict.task_args, "leaves": [],
                    "prunes": prunes, "reasons": {name: verdict.reason}}
        prunes[name] = verdict.reason
        steps[name] = {**(steps.get(name) or {}), "state": "pruned",
                       "reason": verdict.reason}

    leaves: list[tuple[str, dict]] = []
    for name in LEAVES:
        if _state(name) != "planned":
            continue
        verdict = BY_NAME[name].needs(ctx)
        if verdict.run:
            leaves.append((name, verdict.task_args))
            reasons[name] = verdict.reason
        else:
            prunes[name] = verdict.reason
            steps[name] = {**(steps.get(name) or {}), "state": "pruned",
                           "reason": verdict.reason}

    if leaves:
        return {"action": "fork", "step": None, "task_args": {},
                "leaves": leaves, "prunes": prunes, "reasons": reasons}
    return {"action": "finish", "step": None, "task_args": {},
            "leaves": [], "prunes": prunes, "reasons": reasons}


def next_stage_index(steps: dict) -> int:
    """The stage number the next dispatch occupies (origin is 1)."""
    live = {"origin", "dispatched"}
    return len([n for n in STEP_ORDER
                if (steps.get(n) or {}).get("state") in live]) + 1


def record_dispatch(run_id: str, dispatched: dict[str, dict], *,
                    prunes: dict[str, str] | None = None,
                    fork: dict | None = None,
                    fork_at: str | None = None) -> dict | None:
    """Record one advance of the run: what was pruned, what was dispatched.

    One write per advance, made by the single task that is moving the run
    forward, so there is no interleaving with another writer of this key.

    Args:
        run_id: The run this advance belongs to. A mismatch abandons the write
            rather than stamping a newer run with an older run's step states.
        dispatched: ``{step: extra fields}`` for steps just handed to the queue.
        prunes: ``{step: reason}`` for steps this advance decided to skip.
        fork: ``{"leaves": [...], "fork_ts": "..."}`` when this advance forked.
        fork_at: The step the fan-out came from (drives the chart's fork line).
    """
    def _apply(record: dict) -> bool | None:
        if record.get("run_id") != run_id:
            return False
        steps = record.setdefault("steps", {})
        for step, reason in (prunes or {}).items():
            prev = steps.get(step) or {}
            if prev.get("state") == "planned":
                steps[step] = {**prev, "state": "pruned", "reason": reason}
        stage_index = next_stage_index(steps)
        for step, extra in dispatched.items():
            prev = steps.get(step) or {}
            steps[step] = {**prev, "state": "dispatched",
                           "dispatched_ts": _now(), "stage_index": stage_index,
                           **(extra or {})}
        if fork is not None:
            record["fork"] = fork
        if fork_at is not None:
            record["fork_at"] = fork_at
        record["stage_total"] = stage_total(steps)
        record["in_flight"] = True
        record["provisional"] = False
        return None

    return mutate_run(_apply)


def summarize(record: dict) -> str:
    """One sentence for the card and the chart header.

    Names what the run refreshed, or says plainly that nothing needed it — the
    everyday outcome once pruning works, and the one the operator most needs
    stated rather than implied by an empty chart.
    """
    steps = record.get("steps") or {}
    ran = [n for n in DOWNSTREAM_ORDER
           if (steps.get(n) or {}).get("state") == "dispatched"]
    origin_label = record.get("origin_label") or SHORT_LABELS.get(
        record.get("origin", ""), record.get("origin", ""))

    if record.get("partial"):
        failed = record.get("failed_at") or ""
        failed_labels = ", ".join(
            SHORT_LABELS.get(f.strip(), f.strip()) for f in str(failed).split(",") if f.strip())
        if failed_labels:
            return f"Refresh run stopped at {failed_labels}."
        return "Refresh run did not finish."

    if not ran:
        # "Nothing needed refreshing" is a claim about the DATA, and it is only
        # true when the run actually looked. A consolidate-only run was told not
        # to look: its impact goes to the deferred ledger for the next refresh,
        # and saying nothing was needed would flatly contradict that.
        if record.get("mode") == "consolidate_only":
            return f"{origin_label} finished. Downstream refreshes were skipped."
        return f"{origin_label} finished. Nothing downstream needed refreshing."
    labels = [SHORT_LABELS.get(n, n).lower() for n in ran]
    if len(labels) == 1:
        listed = labels[0]
    else:
        listed = ", ".join(labels[:-1]) + f" and {labels[-1]}"
    pruned = [n for n in DOWNSTREAM_ORDER
              if (steps.get(n) or {}).get("state") == "pruned"]
    tail = f" {len(pruned)} step(s) were not needed." if pruned else ""
    return f"Refreshed {listed}.{tail}"


#: How long a dispatched-but-not-yet-started step may sit "queued" before it is
#: treated as lost. Equal to the Cloud Tasks queue's maxRetryDuration (3600 s):
#: with maxAttempts=4 and 60 s→600 s backoff, a task dropped by a 429 (no free
#: runner instance) is redelivered for up to an hour and then never again. Any
#: shorter grace declares a run dead while its next step is still on its way —
#: on 2026-09-04 the semantic map was delivered 23 minutes after dispatch and
#: the run had been swept at minute 10, twenty seconds before it woke.
QUEUED_DELIVERY_GRACE_SECONDS = 3600


def awaiting_delivery(record: dict | None) -> str | None:
    """The name of an in-run step whose task is dispatched but not yet started,
    or None. A fresh ``queued`` stamp means the queue still owes us a delivery;
    the run is alive even though nothing is running and nothing has completed.
    """
    from web_interface.task_status import read_task_status

    if not record:
        return None
    started_dt = _parse_ts(record.get("started_ts"))
    now = datetime.now(UTC)
    for name in STEP_ORDER:
        state = (record.get("steps", {}).get(name) or {}).get("state")
        if state != "dispatched":
            continue
        try:
            st = read_task_status(name) or {}
        except Exception:
            continue
        if (st.get("state") or "").lower() != "queued":
            continue
        stamped = _parse_ts(st.get("updated_at"))
        if stamped is None or (started_dt is not None and stamped < started_dt):
            continue
        if (now - stamped).total_seconds() <= QUEUED_DELIVERY_GRACE_SECONDS:
            return name
    return None


def last_activity_ts(record: dict | None) -> str | None:
    """The most recent sign of life from this run, for the abandoned-run check.

    NOT simply ``updated_ts``: the record is written when a step is dispatched
    and then left alone while that step works, so a step running longer than the
    abandonment window leaves the record "stale" the whole time it is busy. The
    moment it completes — before the task runner's advance lands — the run looks
    exactly like one whose server died: flag set, nothing running, record
    untouched for over a minute. That race killed a real run twice on 2026-09-04.

    The activity signal is each in-run step's own STATUS FILE ``updated_at``,
    not its ``process_stats`` end time. The status file is single-writer and
    written by the worker the instant it completes; ``process_stats`` is written
    a moment later by the runner and read lazily by the hub, whose in-memory
    copy can still hold the PREVIOUS run's end time — which is how the first
    version of this fix read a stale value, ignored it, and let the sweep fire
    anyway. ``process_stats`` remains a fallback only.
    """
    from web_interface.process_manager import process_stats
    from web_interface.task_status import read_task_status

    if not record:
        return None
    started_dt = _parse_ts(record.get("started_ts"))
    latest = _parse_ts(record.get("updated_ts")) or started_dt

    def _consider(ts: str | None) -> None:
        nonlocal latest
        dt = _parse_ts(ts)
        if dt is None:
            return
        # Only this run's own work counts; an older run's timestamps must not
        # keep a dead run looking alive.
        if started_dt is not None and dt < started_dt:
            return
        if latest is None or dt > latest:
            latest = dt

    for name in STEP_ORDER:
        state = (record.get("steps", {}).get(name) or {}).get("state")
        if state not in ("origin", "dispatched"):
            continue
        try:
            st = read_task_status(name) or {}
        except Exception:
            st = {}
        _consider(st.get("updated_at"))
        _consider(st.get("end_time"))
        _consider((process_stats.get(name) or {}).get("last_run_end_time"))
    return latest.isoformat() if latest else None


def run_refreshed_anything(record: dict | None) -> bool:
    """Did this run actually dispatch a downstream step?

    False for a consolidate-only run and for one whose every step was pruned —
    in both cases nothing downstream was rebuilt, so whatever needed refreshing
    still does.
    """
    steps = (record or {}).get("steps") or {}
    return any((steps.get(n) or {}).get("state") == "dispatched"
               for n in DOWNSTREAM_ORDER)


def finish_run(*, partial: bool = False, failed_at: str | None = None,
               reason: str | None = None, prunes: dict[str, str] | None = None,
               run_id: str | None = None) -> dict | None:
    """Close the run: clear in-flight, stamp the summary, drop the fork record.

    Idempotent — the fan-out barrier can reach it more than once when leaves
    finish together.
    """
    def _apply(record: dict) -> bool | None:
        if run_id is not None and record.get("run_id") != run_id:
            return False
        steps = record.setdefault("steps", {})
        for step, why in (prunes or {}).items():
            prev = steps.get(step) or {}
            if prev.get("state") == "planned":
                steps[step] = {**prev, "state": "pruned", "reason": why}
        record["in_flight"] = False
        record["fork"] = None
        record["partial"] = bool(partial)
        record["failed_at"] = failed_at
        if reason:
            record["reason"] = reason
        record["finished_ts"] = _now()
        record["provisional"] = False
        record["summary"] = summarize(record)
        # Anything still waiting when the run ends never ran: planned steps
        # become "skipped" (the amber state), which is a different statement
        # from "pruned" (deliberately not needed).
        for name, step in list((record.get("steps") or {}).items()):
            if (step or {}).get("state") == "planned":
                record["steps"][name] = {**step, "state": "skipped"}
        record["stage_total"] = stage_total(record.get("steps") or {})
        return None

    return mutate_run(_apply)
