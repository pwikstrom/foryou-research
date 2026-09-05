"""Automatic per-collection enrichment: slice cutter, ledger and supervisor tick.

All storage, queue and process calls are monkeypatched — nothing touches disk,
GCS or the real workers. Pins:

1. Process B buys whole days newest-first, never splits one across the budget
   line, and skips days below ``min_day_items`` (the Correlations floor).
2. Process A samples whole days per month, honours ``a_day_cap``, and never
   picks a day B has already taken — no double-buying between processes.
3. ``sample_share`` splits the cycle budget, cursors advance monotonically, and
   re-planning from the same cursor is byte-identical (``stable_sample``
   determinism, independent of input row order).
4. Budget exhaustion / walked-off-history sets ``exhausted``.
5. ``annotation_eligible`` (the single shared predicate) refuses anything not
   provably ``scraped_ok & video_downloaded``: an unscraped id in the annotation
   queue is burnt permanently as ``annotated_fail``.
6. The supervisor tick is a strict priority chain — busy gate, drain, settle,
   handoff, plan — and dispatches at most one worker per tick.
"""

import pandas as pd
import pytest

import web_interface.services.collection_enrichment as ce


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(monkeypatch):
    """In-memory data_io: json files keyed by filename."""
    files: dict[str, object] = {}

    def load_json(storage_location="cache", filename="", **kwargs):
        return files.get(filename)

    def update_json(storage_location="cache", filename="", mutate=None,
                    default=None, **kwargs):
        files[filename] = mutate(files.get(filename, default))
        return files[filename]

    monkeypatch.setattr(ce.data_io, "load_json", load_json)
    monkeypatch.setattr(ce.data_io, "update_json", update_json)
    return files


def _activity(days: dict[str, int], cid="c1", platform="tiktok") -> pd.DataFrame:
    """One collection's activity: {'YYYY-MM-DD': n_items} -> load_activity shape."""
    rows = []
    for day, n in days.items():
        for i in range(n):
            rows.append({"item_id": f"{day}#{i}", "day": pd.Timestamp(day),
                         "source_platform": platform})
    return pd.DataFrame(rows)


def _status(item_ids, scraped=(), scrape_fail=(), downloaded=None,
            annotated=(), annotated_fail=()) -> pd.DataFrame:
    """enrichment_status in load_status's indexed shape."""
    ids = [str(i) for i in item_ids]
    scraped = set(scraped)
    downloaded = scraped if downloaded is None else set(downloaded)
    df = pd.DataFrame({
        "item_id": ids,
        "scraped_ok": [i in scraped for i in ids],
        "scrape_fail": [i in set(scrape_fail) for i in ids],
        "video_downloaded": [i in downloaded for i in ids],
        "annotated_ok": [i in set(annotated) for i in ids],
        "annotated_fail": [i in set(annotated_fail) for i in ids],
    })
    return df.set_index("item_id")


def _entry(**settings) -> dict:
    # A far-away annotation target by default, so tests exercising the slice
    # cutter aren't clamped by it; target-specific tests override it.
    return {"state": ce.STATE_RUNNING,
            "settings": {**ce.DEFAULT_SETTINGS,
                         "annotation_target": 1_000_000, **settings},
            "spent_items": 0}


# --------------------------------------------------------------------------- #
# Process B — whole recent days
# --------------------------------------------------------------------------- #

def test_b_takes_whole_days_newest_first_and_never_splits_one():
    activity = _activity({"2026-08-25": 30, "2026-08-26": 30, "2026-08-27": 30})
    # Budget of 50 B-items: day 27 (30) fits; adding day 26 would overflow, so
    # the cycle stops there rather than half-buying day 26.
    entry = _entry(cycle_items=50, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)

    days = {i.split("#")[0] for i in out["item_ids"]}
    assert days == {"2026-08-27"}
    assert len(out["item_ids"]) == 30          # the whole day, nothing more
    assert out["b_cursor"] == "2026-08-27"
    assert out["b"] == 30 and out["a"] == 0


def test_b_takes_one_oversized_day_whole_rather_than_splitting():
    activity = _activity({"2026-08-27": 80})
    entry = _entry(cycle_items=50, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    # A single day larger than the budget is still taken whole: a half-covered
    # sitting is worth nothing to Sessions.
    assert len(out["item_ids"]) == 80


def test_b_skips_days_below_the_correlations_floor():
    activity = _activity({"2026-08-26": 3, "2026-08-27": 30})
    entry = _entry(cycle_items=100, sample_share=0.0, min_day_items=10)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    days = {i.split("#")[0] for i in out["item_ids"]}
    assert days == {"2026-08-27"}
    # The tiny day is walked past — the cursor moves beyond it so the next
    # cycle does not reconsider it.
    assert out["b_cursor"] == "2026-08-26"


def test_b_resumes_from_the_cursor():
    activity = _activity({"2026-08-25": 20, "2026-08-26": 20, "2026-08-27": 20})
    entry = {**_entry(cycle_items=20, sample_share=0.0), "b_cursor": "2026-08-27"}
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    days = {i.split("#")[0] for i in out["item_ids"]}
    assert days == {"2026-08-26"}
    assert out["b_cursor"] == "2026-08-26"


def test_b_ignores_already_scraped_and_permanently_failed():
    activity = _activity({"2026-08-27": 20})
    ids = [f"2026-08-27#{i}" for i in range(20)]
    status = _status(ids, scraped=ids[:5], scrape_fail=ids[5:8])
    entry = _entry(cycle_items=100, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=status)
    assert set(out["item_ids"]) == set(ids[8:])   # neither scraped nor failed


# --------------------------------------------------------------------------- #
# Process A — sampled whole days across history
# --------------------------------------------------------------------------- #

def test_a_samples_whole_days_capped_and_skips_b_days():
    # Two months of history; B (budget 20) covers the newest day whole, then A
    # (budget 80) samples days per month excluding B's.
    days = {f"2026-08-{d:02d}": 20 for d in (10, 11, 12, 27)}
    days.update({f"2026-07-{d:02d}": 20 for d in (1, 2, 3)})
    activity = _activity(days)
    entry = _entry(cycle_items=100, sample_share=0.8,
                   a_days_per_month=2, a_day_cap=5)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)

    b_days = {i.split("#")[0] for i in out["item_ids"][:out["b"]]}
    a_items = out["item_ids"][out["b"]:]
    a_days = {}
    for iid in a_items:
        a_days.setdefault(iid.split("#")[0], []).append(iid)

    assert b_days == {"2026-08-27"}
    assert not (set(a_days) & b_days)              # A never re-buys B's day
    for day, items in a_days.items():
        assert len(items) <= 5                     # a_day_cap respected
    for month in {d[:7] for d in a_days}:
        assert len([d for d in a_days if d.startswith(month)]) <= 2


def test_a_quota_subtracts_already_scraped():
    activity = _activity({"2026-07-01": 20})
    ids = [f"2026-07-01#{i}" for i in range(20)]
    status = _status(ids, scraped=ids[:4])
    entry = _entry(cycle_items=100, sample_share=1.0,
                   a_days_per_month=2, a_day_cap=5)
    out = ce.plan_cycle("c1", entry, activity=activity, status=status)
    # Day already holds 4 scraped items; the cap of 5 leaves a quota of 1.
    assert len(out["item_ids"]) == 1


def test_sample_share_splits_the_budget():
    days = {f"2026-08-{d:02d}": 10 for d in range(1, 29)}
    days.update({f"2026-{m:02d}-15": 40 for m in range(1, 8)})
    activity = _activity(days)
    entry = _entry(cycle_items=100, sample_share=0.2,
                   a_days_per_month=1, a_day_cap=40, min_day_items=10)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    # b_budget = 80: whole 10-item August days, so exactly 80. a_budget = 20,
    # and A stops adding days once met — it may overshoot by at most the one
    # day that crossed the line (day cap 40), never trimming a day.
    assert out["b"] == 80
    assert 0 < out["a"] <= 20 - 1 + 40


def test_replan_from_same_cursor_is_deterministic_and_order_independent():
    days = {f"2026-0{m}-{d:02d}": 15 for m in (5, 6, 7) for d in (3, 9, 17, 24)}
    activity = _activity(days)
    entry = _entry(cycle_items=60, sample_share=0.5, a_days_per_month=1, a_day_cap=8)
    out1 = ce.plan_cycle("c1", entry, activity=activity, status=None)
    shuffled = activity.sample(frac=1.0, random_state=7).reset_index(drop=True)
    out2 = ce.plan_cycle("c1", entry, activity=shuffled, status=None)
    assert out1["item_ids"] == out2["item_ids"]
    assert (out1["a_cursor"], out1["b_cursor"]) == (out2["a_cursor"], out2["b_cursor"])


# --------------------------------------------------------------------------- #
# Target and exhaustion
# --------------------------------------------------------------------------- #

def test_met_target_yields_exhausted_and_no_items():
    activity = _activity({"2026-08-27": 30})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids[:10], annotated=ids[:10])
    entry = _entry(annotation_target=10)          # already at the target
    out = ce.plan_cycle("c1", entry, activity=activity, status=status)
    assert out["item_ids"] == [] and out["exhausted"] is True


def test_a_target_below_current_annotation_stops_every_cycle():
    """Successor of the 2026-08-31 budget incident: a goal set below the
    current state must clamp to no work, not to a negative slice."""
    activity = _activity({"2026-08-27": 30})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids[:20], annotated=ids[:20])
    entry = _entry(annotation_target=5, cycle_items=100)
    out = ce.plan_cycle("c1", entry, activity=activity, status=status)
    assert out["item_ids"] == [] and out["exhausted"] is True


def test_no_target_means_nothing_to_do():
    # 0 = unset. An armed plan must state its goal, or it would run to 100%.
    activity = _activity({"2026-08-27": 30})
    out = ce.plan_cycle("c1", _entry(annotation_target=0),
                        activity=activity, status=None)
    assert out["item_ids"] == [] and out["exhausted"] is True


def test_remaining_target_clamps_the_cycle_but_never_splits_a_day():
    activity = _activity({"2026-08-27": 30})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids[:4], annotated=ids[:4])
    # 4 annotated, target 10 → 6 of headroom. The whole-day rule outranks the
    # clamp for the FIRST day (a split day is worthless to Sessions), so the
    # 26 unscraped items are planned in full; the handoff's room clamp is what
    # holds annotation spend to the target.
    entry = _entry(annotation_target=10, cycle_items=100, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=status)
    assert len(out["item_ids"]) == 26

    # And a met target plans nothing at all, whole days or not.
    met = _entry(annotation_target=4, cycle_items=100, sample_share=0.0)
    out = ce.plan_cycle("c1", met, activity=activity, status=status)
    assert out["item_ids"] == [] and out["exhausted"] is True


def test_fully_enriched_history_is_exhausted():
    activity = _activity({"2026-08-26": 20, "2026-08-27": 20})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids)
    out = ce.plan_cycle("c1", _entry(), activity=activity, status=status)
    assert out["item_ids"] == [] and out["exhausted"] is True


def test_earliest_date_floors_both_processes():
    activity = _activity({"2026-06-15": 20, "2026-08-27": 20})
    entry = _entry(cycle_items=200, earliest_date="2026-08-01")
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert {i.split("#")[0] for i in out["item_ids"]} == {"2026-08-27"}


# --------------------------------------------------------------------------- #
# The shared annotation predicate
# --------------------------------------------------------------------------- #

def test_annotation_eligible_refuses_unscraped_and_undownloaded():
    ids = ["a", "b", "c", "d", "e"]
    status = _status(ids, scraped=["a", "b", "c", "d"], downloaded=["a", "b", "c"],
                     annotated=["b"], annotated_fail=["c"])
    # a: fine. b: already annotated. c: failed. d: no media. e: unscraped.
    assert ce.annotation_eligible(ids, status) == ["a"]


def test_annotation_eligible_retry_failed_and_duration_guard():
    ids = ["a", "b"]
    status = _status(ids, scraped=ids, downloaded=ids, annotated_fail=["a"])
    assert ce.annotation_eligible(ids, status, retry_failed=True,
                                  max_duration=600) == ["a", "b"]
    assert ce.annotation_eligible(ids, status, durations={"b": 900},
                                  retry_failed=True, max_duration=600) == ["a"]


def test_annotation_eligible_survives_pyarrow_missing_durations():
    # Study frames load with the pyarrow dtype backend, so a missing duration
    # is pd.NA, not NaN. Building a float64 Series straight from those raised
    # TypeError and failed the whole enqueue ("float() argument must be a
    # string or a real number, not 'NAType'"). Unknown durations are kept.
    ids = ["a", "b", "c"]
    status = _status(ids, scraped=ids, downloaded=ids)
    frame = pd.DataFrame(
        {"item_id": ids, "duration": [30.0, None, 900.0]}
    ).convert_dtypes(dtype_backend="pyarrow")
    durations = dict(zip(frame["item_id"], frame["duration"]))
    assert durations["b"] is pd.NA
    assert ce.annotation_eligible(ids, status, durations=durations,
                                  max_duration=600) == ["a", "b"]


def test_annotation_eligible_accepts_column_and_unnamed_index_shapes():
    ids = ["a", "b"]
    status = _status(ids, scraped=["a"], downloaded=["a"])
    as_column = status.reset_index()
    assert ce.annotation_eligible(ids, as_column) == ["a"]
    unnamed = status.copy()
    unnamed.index.name = None
    assert ce.annotation_eligible(ids, unnamed) == ["a"]


def test_handoff_respects_the_remaining_target(monkeypatch):
    activity = _activity({"2026-08-27": 10})
    ids = list(activity["item_id"])
    # 2 of the 10 already annotated; a target of 6 leaves room for 4 more.
    status = _status(ids, scraped=ids, downloaded=ids, annotated=ids[:2])
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)
    entry = {**_entry(annotation_target=6), "in_flight": ids}
    assert len(ce.handoff_scraped("c1", entry)["ready"]) == 4


def test_handoff_refuses_when_the_target_is_unset(monkeypatch):
    # A zeroed target mid-plan must not keep spending on the strength of items
    # queued under the earlier goal.
    activity = _activity({"2026-08-27": 5})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids, downloaded=ids)
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)
    entry = {**_entry(annotation_target=0), "in_flight": ids}
    assert ce.handoff_scraped("c1", entry)["ready"] == []


def test_handoff_always_sweeps_the_scraped_backlog(monkeypatch):
    # 2026-08-31 semantics change (user decision, reversing the 2026-08-28
    # in_flight scoping): annotating an already-scraped video is the cheapest
    # step toward the target, so the handoff always sweeps the collection's
    # scraped-but-unannotated set — bounded by the target, which is the
    # protection the old annotate_existing opt-in existed to provide.
    activity = _activity({"2026-08-27": 10})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids, downloaded=ids)
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)

    entry = _entry()                                # no in_flight recorded
    assert ce.handoff_scraped("c1", entry)["ready"] == ids

    # The target still bounds the sweep.
    entry = _entry(annotation_target=4)
    assert ce.handoff_scraped("c1", entry)["ready"] == ids[:4]

    # A stored annotate_existing key (pre-change ledger) changes nothing.
    entry = {**_entry(), "settings": {**_entry()["settings"],
                                      "annotate_existing": False}}
    assert ce.handoff_scraped("c1", entry)["ready"] == ids


def test_handoff_prunes_resolved_ids_from_in_flight(monkeypatch):
    activity = _activity({"2026-08-27": 6})
    ids = list(activity["item_id"])
    # a0: annotatable now; a1: already annotated; a2: scrape permanently
    # failed; a3: still awaiting a scrape outcome.
    status = _status(ids, scraped=ids[:2], downloaded=ids[:2],
                     annotated=[ids[1]], scrape_fail=[ids[2]])
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)
    entry = {**_entry(), "in_flight": ids[:4]}
    result = ce.handoff_scraped("c1", entry)
    assert result["ready"] == [ids[0]]
    assert result["in_flight"] == [ids[3]]


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def test_save_plan_merges_settings_and_delete_drops(store):
    ce.save_plan("c1", {"state": ce.STATE_RUNNING,
                        "settings": {"annotation_target": 500}})
    ce.save_plan("c1", {"settings": {"cycle_items": 100}})
    entry = store[ce.LEDGER_FILENAME]["c1"]
    assert entry["settings"]["annotation_target"] == 500  # survived the 2nd patch
    assert entry["settings"]["cycle_items"] == 100
    assert entry["settings"]["a_day_cap"] == 50         # defaults filled in

    ce.save_plan("c1", {"__delete__": True})
    assert "c1" not in store[ce.LEDGER_FILENAME]


def test_normalize_settings_clamps_nonsense():
    out = ce.normalize_settings({"annotation_target": -5, "cycle_items": "junk",
                                 "sample_share": 7, "earliest_date": "not-a-date"})
    assert out["annotation_target"] == 0
    assert out["cycle_items"] == ce.DEFAULT_SETTINGS["cycle_items"]
    assert out["sample_share"] == 1.0
    assert out["earliest_date"] is None


# --------------------------------------------------------------------------- #
# The supervisor tick — one action per tick, strict priority
# --------------------------------------------------------------------------- #

@pytest.fixture
def tick(monkeypatch, store):
    """A harness around run_enrichment_supervisor with the world stubbed out."""
    import web_interface.run_enrichment_supervisor as sup

    world = {
        "enabled": True, "busy": [], "in_flight": False,
        "scrape_queues": {}, "unconsolidated": None,
        "started": [], "plans": {},
        "handoff": {}, "cycle": None, "storm": None,
        # Per-lane state: platforms whose scraper runs, annotator running,
        # who blocks a consolidation, and the batch worker's claimed ids.
        "scrape_busy": set(), "annotate_busy": False,
        "consolidate_blockers": [], "claimed": set(),
        "finalize": None, "backstop": None,
    }

    monkeypatch.setattr(sup, "_admin_kill_switch", lambda: world["enabled"])
    monkeypatch.setattr(sup, "_hard_gate", lambda: list(world["busy"]))
    monkeypatch.setattr(sup, "_pipeline_in_flight", lambda: world["in_flight"])
    monkeypatch.setattr(sup, "_unconsolidated", lambda: world["unconsolidated"])
    monkeypatch.setattr(sup, "_annotator_process", lambda: "queue_annotator")
    monkeypatch.setattr(sup, "_scraper_blocked",
                        lambda platform: world["storm"])
    monkeypatch.setattr(sup, "_scrape_lane_busy",
                        lambda platform: platform in world["scrape_busy"])
    monkeypatch.setattr(sup, "_annotate_lane_busy",
                        lambda: bool(world["annotate_busy"]))
    monkeypatch.setattr(sup, "_in_flight_annotation_ids",
                        lambda: set(world["claimed"]))
    monkeypatch.setattr(sup, "_finalize",
                        lambda reporter, require_backstop=False:
                        world["backstop"] if require_backstop else world["finalize"])
    import web_interface.services.worker_status as ws
    monkeypatch.setattr(ws, "_workers_blocking_consolidate",
                        lambda: list(world["consolidate_blockers"]))
    monkeypatch.setattr(sup, "_start",
                        lambda name, task_args=None:
                        (world["started"].append((name, task_args or {})), (True, "ok"))[1])

    monkeypatch.setattr(ce, "armed_plans", lambda: dict(world["plans"]))
    monkeypatch.setattr(ce, "get_plan", lambda cid: world["plans"].get(cid))
    monkeypatch.setattr(ce, "handoff_scraped",
                        lambda cid, entry, **kw: {
                            "ready": world["handoff"].get(cid, []),
                            "in_flight": list(entry.get("in_flight") or [])})
    monkeypatch.setattr(ce, "load_activity",
                        lambda cid: _activity({"2026-08-27": 30}, cid=cid))
    if world["cycle"] is None:
        monkeypatch.setattr(ce, "plan_cycle",
                            lambda cid, entry, **kw: {
                                "item_ids": [f"{cid}-i{n}" for n in range(5)],
                                "a_cursor": "2026-07", "b_cursor": "2026-08-27",
                                "a": 1, "b": 4, "exhausted": False,
                                "platform": "tiktok"})

    class FakeQueues:
        @staticmethod
        def queue_lengths():
            return dict(world["scrape_queues"])

        @staticmethod
        def registered_platforms():
            return ["tiktok", "instagram", "youtube"]

        @staticmethod
        def append_to_scrape_queue(platform, items):
            world["scrape_queues"][platform] = \
                world["scrape_queues"].get(platform, 0) + len(items)
            return len(items)

    import fyp.scrape.scrape_queues as sq
    for fn in ("queue_lengths", "registered_platforms", "append_to_scrape_queue"):
        monkeypatch.setattr(sq, fn, getattr(FakeQueues, fn))

    class Reporter:
        def __init__(self):
            self.lines, self.data = [], []

        def log(self, msg):
            self.lines.append(msg)

        def update_progress(self, pct, msg=""):
            pass

        def emit_data(self, d):
            self.data.append(d)

    def run(**task_args):
        rep = Reporter()
        sup.run_enrichment_supervisor(rep, task_args)
        return rep

    world["run"] = run
    world["store"] = store
    return world


def test_tick_noops_when_disabled_or_busy_or_idle(tick):
    tick["enabled"] = False
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "disabled"

    tick["enabled"] = True
    tick["plans"] = {"c1": _entry()}
    tick["busy"] = ["consolidate_enrichment"]
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "busy"
    assert tick["started"] == []

    tick["busy"] = []
    tick["plans"] = {}
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "idle"


def test_tick_scrapes_while_the_annotator_is_in_flight(tick):
    """The lane split: a running annotator no longer freezes the loop —
    the next cycle's scrape runs inside the annotation window."""
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["scrape_queues"] = {"tiktok": 12}
    tick["annotate_busy"] = True
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "scrape"
    assert tick["started"] == [("queue_scraper_tiktok", {})]


def test_tick_skips_a_platform_whose_scraper_runs(tick):
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["scrape_queues"] = {"tiktok": 12}
    tick["scrape_busy"] = {"tiktok"}
    rep = tick["run"]()
    # The queue is being drained already; the tick falls through to planning,
    # which also skips the busy platform — nothing_to_do.
    assert rep.data[-1]["action"] == "nothing_to_do"
    assert tick["started"] == []


def test_tick_waits_to_consolidate_while_a_lane_is_busy(tick):
    """Results pending + a busy worker = waiting_consolidate, and the tick
    STOPS — falling through to handoff/plan on stale status would hand off
    from a world that has not seen the last batch."""
    tick["plans"] = {"c1": _entry()}
    tick["unconsolidated"] = "annotate"
    tick["consolidate_blockers"] = ["queue_annotator_batch"]
    tick["handoff"] = {"c1": ["x1"]}       # must NOT be reached
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "waiting_consolidate"
    assert tick["started"] == []
    assert tick["store"].get(ce.ANNOTATE_QUEUE_FILENAME) in (None, [])


def test_tick_drains_scrape_queue_first(tick):
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["scrape_queues"] = {"tiktok": 12}
    tick["unconsolidated"] = "scrape"     # would also match; drain must win
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "scrape"
    assert tick["started"] == [("queue_scraper_tiktok", {})]


def test_tick_ignores_queues_of_unarmed_platforms(tick):
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["scrape_queues"] = {"instagram": 40}    # manual admin work, not ours
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "plan"      # fell through to planning


def test_tick_storm_blocks_the_platform_plans(tick):
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["scrape_queues"] = {"tiktok": 12}
    tick["storm"] = "permanent_storm_tripped"
    tick["run"]()
    assert tick["started"] == []                 # scraper NOT restarted
    ledger = tick["store"][ce.LEDGER_FILENAME]
    assert ledger["c1"]["state"] == ce.STATE_BLOCKED


def test_tick_settles_core_only_after_either_worker(tick):
    """Every supervisor consolidation is core-only now — the downstream chain
    is deferred to finalize (the one-full-refresh-per-plan design).

    plan_deferred marks the debt as the LOOP's, which is what entitles finalize
    to spend it. An operator's own consolidate-without-refresh writes the same
    ledger entry without the flag and is left alone (2026-09-04: the supervisor
    spent a manual debt 3.5 min after it was created, overriding the operator's
    explicit choice).
    """
    tick["plans"] = {"c1": _entry()}
    tick["unconsolidated"] = "scrape"
    tick["run"]()
    assert tick["started"] == [
        ("consolidate_enrichment", {"auto_refresh": False, "plan_deferred": True})]

    tick["started"].clear()
    tick["unconsolidated"] = "annotate"
    tick["run"]()
    assert tick["started"] == [
        ("consolidate_enrichment", {"auto_refresh": False, "plan_deferred": True})]


def test_tick_handoff_is_the_boundary_move(tick):
    # Ticks fire only at terminal worker completions, so the handoff tick is
    # the cycle boundary and performs the whole move: start the annotator on
    # the backlog, then cut and start the next scrape slice so it runs inside
    # the annotation window.
    tick["plans"] = {"c1": {**_entry(), "spent_items": 10}}
    tick["handoff"] = {"c1": ["x1", "x2", "x3"]}
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "annotate"
    assert rep.data[-1]["handoff_queued"] == 3
    started = [n for n, _ in tick["started"]]
    assert started[0] == "queue_annotator"
    assert "queue_scraper_tiktok" in started       # the next slice, same tick
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["spent_items"] == 13
    assert entry["cycles"] == 1                    # the next slice was cut
    assert set(tick["store"][ce.ANNOTATE_QUEUE_FILENAME]) == {"x1", "x2", "x3"}


def test_tick_handoff_skips_ids_claimed_by_inflight_jobs(tick):
    """The double-pay regression pin: enrichment status cannot see the batch
    worker's claims, so the handoff must subtract them itself."""
    tick["plans"] = {"c1": {**_entry(), "spent_items": 0}}
    tick["handoff"] = {"c1": ["x1", "x2", "x3"]}
    tick["claimed"] = {"x1", "x3"}
    rep = tick["run"]()
    assert set(tick["store"][ce.ANNOTATE_QUEUE_FILENAME]) == {"x2"}
    assert rep.data[-1]["handoff_queued"] == 1
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["spent_items"] == 1               # only the re-queued item charged


def test_tick_plans_one_collection_and_advances_cursors(tick):
    tick["plans"] = {"c1": _entry(), "c2": _entry()}
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "plan"
    assert tick["scrape_queues"] == {"tiktok": 5}   # ONE collection served
    ledger = tick["store"][ce.LEDGER_FILENAME]
    served = [cid for cid in ("c1", "c2") if cid in ledger]
    assert len(served) == 1
    entry = ledger[served[0]]
    assert entry["b_cursor"] == "2026-08-27" and entry["a_cursor"] == "2026-07"
    assert entry["cycles"] == 1 and entry["stall_count"] == 1


def test_tick_completes_an_exhausted_plan(tick, monkeypatch):
    import web_interface.run_enrichment_supervisor as sup  # noqa: F401
    tick["plans"] = {"c1": _entry()}
    monkeypatch.setattr(ce, "plan_cycle",
                        lambda cid, entry, **kw: {
                            "item_ids": [], "a_cursor": None, "b_cursor": None,
                            "a": 0, "b": 0, "exhausted": True,
                            "platform": "tiktok"})
    tick["run"]()
    assert tick["store"][ce.LEDGER_FILENAME]["c1"]["state"] == ce.STATE_DONE


def test_tick_annotate_stall_guard_parks_plans(tick):
    """A queue that does not drain across runs must not loop the annotator.

    Found live: when annotation results cannot be refined (misconfigured
    backend), nothing is pruned from to_annotate.json, and without the guard
    every tick restarts the annotator on the same items forever.
    """
    tick["plans"] = {"c1": _entry()}
    tick["store"][ce.ANNOTATE_QUEUE_FILENAME] = ["x1", "x2", "x3"]

    rep = tick["run"]()
    assert rep.data[-1]["action"] == "annotate"      # first run: fine
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "annotate"      # strike one, still tries
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "annotate_stalled"
    assert tick["started"] == [("queue_annotator", {}), ("queue_annotator", {})]
    assert tick["store"][ce.LEDGER_FILENAME]["c1"]["state"] == ce.STATE_BLOCKED

    # A draining queue resets the guard instead of striking.
    tick["store"][ce.LEDGER_FILENAME]["c1"]["state"] = ce.STATE_RUNNING
    tick["plans"] = {"c1": tick["store"][ce.LEDGER_FILENAME]["c1"]}
    tick["store"][ce.ANNOTATE_QUEUE_FILENAME] = ["x1"]
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "annotate"


def test_tick_scrape_stall_guard_parks_platform_plans(tick):
    """A scrape queue that never shrinks (all-transient failures, no storm
    flag) must not have its scraper restarted forever."""
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["scrape_queues"] = {"tiktok": 68}

    rep = tick["run"]()
    assert rep.data[-1]["action"] == "scrape"
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "scrape"        # strike one, still tries
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "scrape_stalled"
    assert len(tick["started"]) == 2
    assert tick["store"][ce.LEDGER_FILENAME]["c1"]["state"] == ce.STATE_BLOCKED

    # A shrinking queue resets the guard.
    tick["plans"] = {"c1": {**tick["store"][ce.LEDGER_FILENAME]["c1"],
                            "state": ce.STATE_RUNNING, "platform": "tiktok"}}
    tick["scrape_queues"] = {"tiktok": 40}
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "scrape"


def test_auto_cycle_items_formula(monkeypatch, store):
    """min(target headroom − pending, one full set of concurrent jobs)."""
    import web_interface.run_enrichment_supervisor as sup
    from web_interface.run_queue_annotator_batch import (
        DEFAULT_BATCH_SIZE, MAX_CONCURRENT_JOBS,
    )
    cap = MAX_CONCURRENT_JOBS * DEFAULT_BATCH_SIZE

    activity = _activity({"2026-08-27": 30})
    ids = list(activity["item_id"].astype(str))
    entry = _entry(annotation_target=10_000, cycle_items_auto=True)

    # No pending, huge headroom: capped at one full job set.
    monkeypatch.setattr(sup, "_in_flight_annotation_ids", lambda: set())
    assert sup._auto_cycle_items(entry, activity, None) == cap

    # Small headroom wins over the cap.
    small = _entry(annotation_target=7, cycle_items_auto=True)
    assert sup._auto_cycle_items(small, activity, None) == 7

    # Pending work (queued + claimed) shrinks the headroom...
    store[ce.ANNOTATE_QUEUE_FILENAME] = ids[:3]
    monkeypatch.setattr(sup, "_in_flight_annotation_ids", lambda: set(ids[3:5]))
    assert sup._auto_cycle_items(small, activity, None) == 2
    # ...and pending items OUTSIDE the collection do not count.
    store[ce.ANNOTATE_QUEUE_FILENAME] = ["other-1", "other-2"]
    monkeypatch.setattr(sup, "_in_flight_annotation_ids", lambda: set())
    assert sup._auto_cycle_items(small, activity, None) == 7

    # Fully covered: 0 (the caller skips the slice, not the plan).
    store[ce.ANNOTATE_QUEUE_FILENAME] = ids[:7]
    assert sup._auto_cycle_items(small, activity, None) == 0


def test_tick_auto_mode_injects_the_effective_cycle_items(tick, monkeypatch):
    import web_interface.run_enrichment_supervisor as sup  # noqa: F401
    seen = {}

    def fake_cycle(cid, entry, **kw):
        seen["cycle_items"] = entry["settings"]["cycle_items"]
        return {"item_ids": ["i1"], "a_cursor": None, "b_cursor": "2026-08-27",
                "a": 0, "b": 1, "exhausted": False, "platform": "tiktok"}

    monkeypatch.setattr(ce, "plan_cycle", fake_cycle)
    monkeypatch.setattr(ce, "load_status", lambda ids: None)
    tick["plans"] = {"c1": _entry(annotation_target=500, cycle_items_auto=True,
                                  cycle_items=400)}
    tick["run"]()
    assert seen["cycle_items"] == 500          # headroom, not the manual 400
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["last_auto_cycle_items"] == 500


def test_normalize_settings_round_trips_cycle_items_auto():
    assert ce.normalize_settings({"cycle_items_auto": True})["cycle_items_auto"] is True
    assert ce.normalize_settings({"cycle_items_auto": False})["cycle_items_auto"] is False
    assert ce.normalize_settings({})["cycle_items_auto"] is False
    assert ce.DEFAULT_SETTINGS["cycle_items_auto"] is False


def test_tick_parks_a_stalled_plan(tick):
    tick["plans"] = {"c1": {**_entry(), "stall_count": ce.MAX_STALLS}}
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_BLOCKED
    assert tick["scrape_queues"] == {}           # nothing enqueued


def test_productive_handoff_clears_the_stall_counter_across_the_boundary_tick(tick, monkeypatch):
    """Regression pin for the 2026-09-04 false park.

    The handoff persisted ``stall_count: 0`` and the boundary tick's _plan,
    reading the in-memory entry snapshotted BEFORE the handoff, wrote
    ``stale + 1`` over it — so a healthy plan's counter climbed by one every
    cycle and the fourth productive cycle was parked with "no scrape progress
    in 3 cycles" (and, the fingerprint, ``stall_count: 0`` in the ledger).
    """
    # In prod get_plan reads the ledger the handoff just wrote; the fixture's
    # default stub returns the stale snapshot, which is exactly the bug's input.
    monkeypatch.setattr(ce, "get_plan",
                        lambda cid: (tick["store"].get(ce.LEDGER_FILENAME) or {}).get(cid))
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok", "stall_count": 0}}
    for cycle in range(ce.MAX_STALLS + 2):
        # Each cycle's scrape and annotation drained, as prod's logs showed.
        tick["scrape_queues"] = {}
        tick["store"].pop(ce.ANNOTATE_QUEUE_FILENAME, None)
        (tick["store"].get(ce.LEDGER_FILENAME) or {}).pop("__meta__", None)
        tick["handoff"] = {"c1": [f"ready-{cycle}-{n}" for n in range(3)]}   # productive
        rep = tick["run"]()
        entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
        assert entry.get("state") != ce.STATE_BLOCKED, f"parked on cycle {cycle + 1}: {entry}"
        assert entry["stall_count"] <= 1, entry          # reset by the handoff, +1 by the slice
        assert rep.data[-1]["action"] == "annotate"
        tick["plans"] = {"c1": {**_entry(), **entry, "state": ce.STATE_RUNNING}}


def test_tick_settles_results_owed_after_the_plan_stopped(tick):
    """The 85 annotations of 2026-09-04: a batch the loop started finished after
    its plan was parked, and with nothing armed no tick ever consolidated it —
    the analysis refresh an hour later ran without those results."""
    import web_interface.run_enrichment_supervisor as sup

    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["scrape_queues"] = {"tiktok": 12}
    tick["run"]()
    assert tick["started"] == [("queue_scraper_tiktok", {})]
    assert ce.get_meta(sup.SETTLE_OWED_KEY)             # the loop now owes a consolidation

    # The plan stops while the job runs; the job's results then await consolidation.
    tick["plans"] = {}
    tick["scrape_queues"] = {}
    tick["unconsolidated"] = "scrape"
    tick["started"].clear()
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "consolidate"
    assert tick["started"] == [("consolidate_enrichment",
                                {"auto_refresh": False, "plan_deferred": True})]
    assert ce.get_meta(sup.SETTLE_OWED_KEY) is None      # debt paid

    # Nothing left to fold in: a later no-plans tick is idle, not a second consolidation.
    tick["unconsolidated"] = None
    tick["started"].clear()
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "idle"
    assert tick["started"] == []


def test_settle_owed_is_forgotten_when_someone_consolidated_by_hand(tick):
    import web_interface.run_enrichment_supervisor as sup

    ce.set_meta(sup.SETTLE_OWED_KEY, {"after": "annotate"})
    tick["plans"] = {}
    tick["unconsolidated"] = None                     # an operator's consolidation covered it
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "idle"
    assert tick["started"] == []
    assert ce.get_meta(sup.SETTLE_OWED_KEY) is None


def test_settle_owed_waits_for_a_busy_worker(tick):
    import web_interface.run_enrichment_supervisor as sup

    ce.set_meta(sup.SETTLE_OWED_KEY, {"after": "scrape"})
    tick["plans"] = {}
    tick["unconsolidated"] = "scrape"
    tick["consolidate_blockers"] = ["queue_annotator_batch"]
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "waiting_consolidate"
    assert tick["started"] == []
    assert ce.get_meta(sup.SETTLE_OWED_KEY)             # still owed


def test_tick_starts_the_scraper_right_after_cutting_a_slice(tick):
    """The first cycle after Arm: a slice cut outside the boundary move used to
    wait for the next trigger — the hourly heartbeat, with nothing running —
    before anyone started the scraper (58 minutes from Arm to the first scrape
    on 2026-09-05)."""
    tick["plans"] = {"c1": _entry()}
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "plan"
    assert tick["started"] == [("queue_scraper_tiktok", {})]
    assert "started the scraper" in rep.data[-1]["message"]


def test_auto_plan_goes_idle_when_its_target_is_met(tick, monkeypatch):
    """An Auto plan whose target is met exactly (10,570/10,570 on 2026-09-05)
    took the "pending work covers the target" exit and stayed Running for
    ever; only plan_cycle's exhausted path marked plans done."""
    import web_interface.services.enrichment_journal as journal

    tick["plans"] = {"c1": {**_entry(annotation_target=100, cycle_items_auto=True),
                            "platform": "tiktok"}}
    monkeypatch.setattr(ce, "load_status", lambda ids: None)
    monkeypatch.setattr(ce, "_annotated_unique", lambda activity, status: 100)
    rep = tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_DONE and entry.get("finished_at")
    assert tick["started"] == []
    assert rep.data[-1]["action"] in ("nothing_to_do", "finalize")
    kinds = [e["kind"] for e in tick["store"][journal.JOURNAL_FILENAME]["events"]]
    assert "plan.done" in kinds


def test_auto_plan_waits_while_pending_work_covers_the_target(tick, monkeypatch):
    """The other reason auto sizing returns 0 — queued/claimed annotations
    already reach the target — must NOT close the plan."""
    tick["plans"] = {"c1": {**_entry(annotation_target=100, cycle_items_auto=True),
                            "platform": "tiktok"}}
    monkeypatch.setattr(ce, "load_status", lambda ids: None)
    monkeypatch.setattr(ce, "_annotated_unique", lambda activity, status: 90)
    tick["claimed"] = {f"2026-08-27#{n}" for n in range(10)}   # in-flight, covers the gap
    rep = tick["run"]()
    # Nothing to cut and nothing to close: the plan is left exactly as it was.
    entry = (tick["store"].get(ce.LEDGER_FILENAME) or {}).get("c1") or tick["plans"]["c1"]
    assert entry.get("state") == ce.STATE_RUNNING
    assert tick["started"] == []
    assert rep.data[-1]["action"] == "nothing_to_do"


def test_journal_drain_split_reads_the_ledger_not_the_snapshot(tick, monkeypatch):
    """The boundary tick's scraper start must count the slice _plan just wrote
    as the plan's own — the first live run reported its own 155 videos as
    "queued elsewhere (drained first)"."""
    import fyp.scrape.scrape_queues as sq
    import web_interface.services.enrichment_journal as journal

    monkeypatch.setattr(ce, "get_plan",
                        lambda cid: (tick["store"].get(ce.LEDGER_FILENAME) or {}).get(cid))
    monkeypatch.setattr(sq, "load_scrape_queue",
                        lambda platform: [f"c1-i{n}" for n in range(5)])   # the fixture's slice
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["handoff"] = {"c1": ["x1"]}
    tick["run"]()
    drains = [e for e in tick["store"][journal.JOURNAL_FILENAME]["events"]
              if e["kind"] == "queue.drained" and e.get("platform") == "tiktok"]
    assert drains and drains[-1]["detail"]["plan_items"] == 5
    assert drains[-1]["detail"]["other_items"] == 0
    assert "queued elsewhere" not in drains[-1]["message"]


def test_tick_writes_the_enrichment_history(tick):
    """The boundary move leaves its story in the journal: the handoff, the
    next slice, and the worker starts with the queue split."""
    import web_interface.services.enrichment_journal as journal

    tick["plans"] = {"c1": {**_entry(), "spent_items": 10, "platform": "tiktok"}}
    tick["handoff"] = {"c1": ["x1", "x2"]}
    tick["run"]()
    events = (tick["store"].get(journal.JOURNAL_FILENAME) or {}).get("events") or []
    kinds = [e["kind"] for e in events]
    assert "handoff" in kinds and "slice.queued" in kinds
    assert kinds.count("queue.drained") == 2          # the annotator, then the scraper
    handoff = next(e for e in events if e["kind"] == "handoff")
    assert handoff["collection_id"] == "c1" and handoff["detail"]["queued"] == 2
    parked = [e for e in events if e["kind"] == "plan.blocked"]
    assert not parked


# --------------------------------------------------------------------------- #
# Tick reporting — a no-op tick has to be visible in the modal
# --------------------------------------------------------------------------- #

def test_last_tick_reports_the_supervisors_outcome(monkeypatch):
    """The panel's only window onto a Cloud Run tick.

    Regression pin for the 2026-08-31 report that "Run a cycle now does
    nothing": the tick had in fact run and correctly decided nothing_to_do
    (the plan's lifetime budget was already spent), but the dispatched-task
    path had no way to say so, so the loop read as broken.
    """
    import web_interface.task_status as ts

    monkeypatch.setattr(ts, "read_task_status", lambda name: {
        "state": "completed",
        "start_time": "2026-08-31T05:26:50+00:00",
        "updated_at": "2026-08-31T05:26:53+00:00",
        "progress": {"percent": 100, "message": "Completed"},
        "data": {"action": "nothing_to_do"},
        "error": None,
    } if name == ce.SUPERVISOR_TASK else None)

    tick = ce.last_tick()
    assert tick["action"] == "nothing_to_do"
    assert tick["state"] == "completed"
    assert tick["start_time"] == "2026-08-31T05:26:50+00:00"


def test_last_tick_is_empty_and_never_raises_without_a_status_file(monkeypatch):
    import web_interface.task_status as ts

    monkeypatch.setattr(ts, "read_task_status", lambda name: None)
    assert ce.last_tick() == {}

    def _boom(name):
        raise RuntimeError("GCS down")

    monkeypatch.setattr(ts, "read_task_status", _boom)
    assert ce.last_tick() == {}


# --------------------------------------------------------------------------- #
# Progress — two denominators, and the budget window they imply
# --------------------------------------------------------------------------- #

def test_progress_counts_videos_and_video_days_separately(monkeypatch):
    """A video watched on three days is three video-days but ONE purchase.

    The panel quotes coverage per video, because that is what a budget buys;
    the day-shaped figures still need the per-(video, day) count.
    """
    rows = [{"item_id": "v1", "day": pd.Timestamp(d), "source_platform": "tiktok"}
            for d in ("2026-08-25", "2026-08-26", "2026-08-27")]
    rows += [{"item_id": "v2", "day": pd.Timestamp("2026-08-27"),
              "source_platform": "tiktok"}]
    activity = pd.DataFrame(rows)
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status",
                        lambda ids=None: _status(["v1", "v2"], scraped=["v1", "v2"],
                                                 annotated=["v1"]))

    out = ce.progress("c1", {**_entry(annotation_target=100), "spent_items": 10})
    assert out["total_items"] == 4        # video-days
    assert out["unique_items"] == 2       # videos
    assert out["scraped_items"] == 4 and out["unique_scraped"] == 2
    assert out["annotated_items"] == 3 and out["unique_annotated"] == 1

    # Target window: below the annotated count a target is already met, above
    # everything not permanently failed it can never be reached.
    assert out["annotation_target"] == 100
    assert out["target_floor"] == 1
    assert out["target_ceiling"] == 2


def test_progress_ceiling_excludes_the_permanently_failed(monkeypatch):
    """A video that failed for good is neither done nor still-to-do.

    Answers the operator's "how can that many remain?" — the ceiling counts
    only videos that can actually still be processed, so burnt annotation
    failures and permanently failed scrapes are out of the arithmetic.
    """
    activity = _activity({"2026-08-27": 5})
    ids = list(activity["item_id"])          # v0..v4
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    # v0 annotated; v1 burnt (annotated_fail); v2 permanently unscrapeable;
    # v3, v4 still processable.
    monkeypatch.setattr(ce, "load_status",
                        lambda i=None: _status(ids, scraped=ids[:2],
                                               scrape_fail=[ids[2]],
                                               annotated=[ids[0]],
                                               annotated_fail=[ids[1]]))

    out = ce.progress("c1", {**_entry(annotation_target=100), "spent_items": 7})
    assert out["unique_annotated"] == 1
    assert out["unique_failed"] == 2
    assert out["target_ceiling"] == 5 - 2    # everything that can still exist annotated


def test_progress_budget_window_is_zero_width_when_nothing_is_left(monkeypatch):
    activity = _activity({"2026-08-27": 3})
    ids = list(activity["item_id"])
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status",
                        lambda i=None: _status(ids, scraped=ids, annotated=ids))

    out = ce.progress("c1", {**_entry(), "spent_items": 4000})
    assert out["unique_annotated"] == 3
    assert out["target_floor"] == out["target_ceiling"] == 3


def test_progress_daily_series_stacks_per_active_day(monkeypatch):
    activity = _activity({"2026-08-26": 4, "2026-08-27": 3})
    ids = list(activity["item_id"])
    d26 = [i for i in ids if i.startswith("2026-08-26")]
    d27 = [i for i in ids if i.startswith("2026-08-27")]
    # Day 26: 2 annotated, 1 awaiting, 1 unscraped. Day 27: 1 failed, 2 unscraped.
    status = _status(ids, scraped=d26[:3], annotated=d26[:2],
                     scrape_fail=[d27[0]])
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)

    daily = ce.progress("c1", _entry())["daily"]
    assert daily["dates"] == ["2026-08-26", "2026-08-27"]
    assert daily["annotated"] == [2, 0]
    assert daily["awaiting"] == [1, 0]
    assert daily["failed"] == [0, 1]
    assert daily["total"] == [4, 3]


def test_normalize_settings_bounds_the_spread_knobs():
    out = ce.normalize_settings({"a_day_cap": 3, "a_days_per_month": 99})
    assert out["a_day_cap"] == 10          # never below the analysis floor
    assert out["a_days_per_month"] == 31
    assert ce.normalize_settings({"a_day_cap": 5000})["a_day_cap"] == 1000
    assert ce.DEFAULT_SETTINGS["sample_share"] == 0.5


# --------------------------------------------------------------------------- #
# The panel's buttons must stay wired
# --------------------------------------------------------------------------- #

def test_enrichment_panel_buttons_keep_their_handlers():
    """Regression pin for the 2026-08-31 prod incident: a tooltip rewrite
    replaced each button from its data-tooltip through </button>, silently
    deleting the onclick between them. Arm/Save/Run then did nothing at all —
    no request, no error — and the panel looked broken with no trace anywhere.
    """
    import html.parser
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "web_interface" / "templates"
           / "tabs" / "dm" / "edit_collections.html").read_text()

    class Buttons(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.by_id = {}

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if d.get("id"):
                self.by_id[d["id"]] = d

    parser = Buttons()
    parser.feed(src)
    expected = {
        "dm-enrich-arm-btn": "dmEnrichToggleArmed",
        "dm-enrich-save-btn": "dmEnrichSave",
        "dm-enrich-tick-btn": "dmEnrichTick",
        "dm-enrich-advanced-toggle": "dmEnrichToggleAdvanced",
        "dm-enrich-history-toggle": "dmEnrichHistoryToggle",
    }
    for element_id, handler in expected.items():
        attrs = parser.by_id.get(element_id)
        assert attrs is not None, f"{element_id} missing from the template"
        assert handler in (attrs.get("onclick") or ""), \
            f"{element_id} lost its onclick ({handler})"

    # The Arm/Save/Run tooltips live on WRAPPER spans, not the buttons: a
    # disabled button eats its own hover tooltip in most browsers, and the
    # button-state logic disables Save and Run precisely when the explanation
    # is most needed. A tooltip moved back onto the button would go silent in
    # exactly those states.
    for wrap_id in ("dm-enrich-arm-wrap", "dm-enrich-save-wrap",
                    "dm-enrich-tick-wrap"):
        attrs = parser.by_id.get(wrap_id)
        assert attrs is not None, f"{wrap_id} missing from the template"
        assert attrs.get("data-tooltip"), f"{wrap_id} lost its data-tooltip"
        assert "meta-tooltip" in (attrs.get("class") or ""), \
            f"{wrap_id} lost the meta-tooltip class"
    for btn_id in ("dm-enrich-arm-btn", "dm-enrich-save-btn",
                   "dm-enrich-tick-btn"):
        attrs = parser.by_id[btn_id]
        assert not attrs.get("data-tooltip"), \
            f"{btn_id} must not carry the tooltip — it sits on the wrapper span"


# --------------------------------------------------------------------------- #
# Live activity for the status strip
# --------------------------------------------------------------------------- #

def test_activity_reports_the_running_worker():
    """activity() answers "what is happening now" from the worker task
    statuses — the running worker's kind, name and own progress line — and
    prefers the plan's scraper over the shared workers when both run."""
    from unittest.mock import patch

    statuses = {
        "queue_annotator_batch": {
            "state": "running", "start_time": "2026-09-01T00:00:00+00:00",
            "progress": {"message": "Batch 1 of 2 (45%)"},
        },
        "queue_scraper_tiktok": {"state": "running", "progress": {}},
    }
    running = {"queue_annotator_batch"}
    with patch("web_interface.services.worker_status._is_worker_running",
               side_effect=lambda n: n in running), \
         patch("web_interface.task_status.read_task_status",
               side_effect=lambda n: statuses.get(n)):
        out = ce.activity("tiktok")
        assert out["kind"] == "annotating"
        assert out["worker"] == "queue_annotator_batch"
        assert out["message"] == "Batch 1 of 2 (45%)"

        running = {"queue_scraper_tiktok", "queue_annotator_batch"}
        out = ce.activity("tiktok")
        assert out["kind"] == "scraping", \
            "the plan's own scraper outranks the shared workers"
        assert out["message"] is None

        # Without a platform (no plan yet) only shared workers are visible.
        running = {"queue_scraper_tiktok"}
        assert ce.activity(None)["kind"] == "waiting"

    with patch("web_interface.services.worker_status._is_worker_running",
               return_value=False):
        out = ce.activity("tiktok")
    assert out == {"kind": "waiting", "worker": None, "message": None,
                   "started_at": None}
