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
    }

    monkeypatch.setattr(sup, "_admin_kill_switch", lambda: world["enabled"])
    monkeypatch.setattr(sup, "_busy", lambda: list(world["busy"]))
    monkeypatch.setattr(sup, "_pipeline_in_flight", lambda: world["in_flight"])
    monkeypatch.setattr(sup, "_unconsolidated", lambda: world["unconsolidated"])
    monkeypatch.setattr(sup, "_annotator_process", lambda: "queue_annotator")
    monkeypatch.setattr(sup, "_scraper_blocked",
                        lambda platform: world["storm"])
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
    tick["busy"] = ["queue_annotator"]
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "busy"
    assert tick["started"] == []

    tick["busy"] = []
    tick["plans"] = {}
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "idle"


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


def test_tick_settles_scrape_light_and_annotate_full(tick):
    tick["plans"] = {"c1": _entry()}
    tick["unconsolidated"] = "scrape"
    tick["run"]()
    assert tick["started"] == [("consolidate_enrichment", {"auto_refresh": False})]

    tick["started"].clear()
    tick["unconsolidated"] = "annotate"
    tick["run"]()
    assert tick["started"] == [("consolidate_enrichment", {"auto_refresh": True})]


def test_tick_handoff_charges_budget_and_starts_the_annotator(tick):
    # Queue-and-start is one logical move: a tick that hands items to the
    # annotation queue drains it in the same tick, instead of leaving the
    # annotator to a later trigger (in steady state the heartbeat, up to an
    # hour away).
    tick["plans"] = {"c1": {**_entry(), "spent_items": 10}}
    tick["handoff"] = {"c1": ["x1", "x2", "x3"]}
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "annotate"
    assert rep.data[-1]["handoff_queued"] == 3
    assert [n for n, _ in tick["started"]] == ["queue_annotator"]
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["spent_items"] == 13
    assert entry["stall_count"] == 0
    assert set(tick["store"][ce.ANNOTATE_QUEUE_FILENAME]) == {"x1", "x2", "x3"}


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


def test_tick_parks_a_stalled_plan(tick):
    tick["plans"] = {"c1": {**_entry(), "stall_count": ce.MAX_STALLS}}
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_BLOCKED
    assert tick["scrape_queues"] == {}           # nothing enqueued


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
