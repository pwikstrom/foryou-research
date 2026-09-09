"""Automatic per-collection enrichment: slice cutter, ledger and supervisor tick.

All storage, queue and process calls are monkeypatched — nothing touches disk,
GCS or the real workers. Pins:

1. Process B takes whole days newest-first, never splits one across the budget
   line, and takes quiet days too — ``min_day_items`` (the Correlations floor)
   limits the spread only, so any deep-dive share can reach the whole collection.
2. Process A samples whole days per month, honours ``a_day_cap``, skips days
   below the floor, and never picks a day B has already taken — no
   double-buying between processes.
3. ``sample_share`` splits the cycle budget, cursors advance monotonically, and
   re-planning from the same cursor is byte-identical (``stable_sample``
   determinism, independent of input row order).
4. Budget exhaustion / walked-off-history sets ``exhausted``.
5. ``annotation_eligible`` (the single shared predicate) refuses anything not
   provably ``scraped_ok & video_downloaded``: an unscraped id in the annotation
   queue is burnt permanently as ``annotated_fail``.
6. The supervisor tick is a strict priority chain — busy gate, drain, settle,
   handoff, plan — and dispatches at most one worker per tick.
7. Within a cut day (the spread's capped days, the deep dive's partial last
   day) whole viewing sessions come first — the day's candidate sessions
   (``session_min_plays`` and up) in a salted order of their own, the one
   crossing the cap taken whole, then single items up to the cap; a sitting
   that runs past midnight is taken whole from the day it started; rows
   without a session fall back to the item draw. ``progress()`` reports the
   sessions a collection has, how many are candidates and how many are
   analysis-ready (every item annotated or failed for good).
"""

import pandas as pd
import pytest

import web_interface.services.collection_enrichment as ce


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def no_slice_floor(monkeypatch):
    """The sizing tests pin the cutter's arithmetic on small numbers; the
    floor that ends a real plan in one cycle would swamp them. The floor's
    own tests set it back. The Sessions tab's play floor lives in the admin
    store: pinned to the shipped default here, so no test reads a
    developer's own override."""
    monkeypatch.setattr(ce, "MIN_CYCLE_ITEMS", 1)
    monkeypatch.setattr(ce, "session_min_plays", lambda: ce.DEFAULT_SESSION_MIN_PLAYS)


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


def _activity(days: dict, cid="c1", platform="tiktok") -> pd.DataFrame:
    """One collection's activity: {'YYYY-MM-DD': n_items} -> load_activity shape.

    A day's value may instead be a list of play counts, one per viewing
    session on that day: the rows then carry load_activity's ``session``
    (the session's start — hourly from 08:00, so the k-th session's key is
    ``<day>T<08+k>:00:00``) and ``session_plays`` columns, which the
    within-day cut samples by. Plain ints leave the columns out, so the
    older pins run the item-level path they were written against; in a
    mixed frame an int day's rows carry no session at all.
    """
    rows = []
    with_sessions = any(isinstance(n, (list, tuple)) for n in days.values())
    for day, n in days.items():
        sizes = list(n) if isinstance(n, (list, tuple)) else [n]
        i = 0
        for k, size in enumerate(sizes):
            for _ in range(size):
                row = {"item_id": f"{day}#{i}", "day": pd.Timestamp(day),
                       "source_platform": platform}
                if with_sessions:
                    row["session"] = (f"{day}T{8 + k:02d}:00:00"
                                      if isinstance(n, (list, tuple)) else None)
                    row["session_plays"] = size if isinstance(n, (list, tuple)) else 0
                rows.append(row)
                i += 1
    return pd.DataFrame(rows)


def _session_key(day: str, k: int) -> str:
    """The key :func:`_activity` gives the k-th session of a day."""
    return f"{day}T{8 + k:02d}:00:00"


def _session_items(activity: pd.DataFrame, key: str) -> set[str]:
    return set(activity.loc[activity["session"] == key, "item_id"])


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
    # Manual cycle sizing unless a test says otherwise: these tests pin the
    # slice cutter's arithmetic against an explicit cycle_items, and the Auto
    # path (now the shipped default) sizes the cycle from the target instead.
    # The spread's days per month is derived from the target in production;
    # the cutter tests pin it directly on the entry, as the supervisor stores it.
    entry = {"state": ce.STATE_RUNNING,
             "settings": {**ce.DEFAULT_SETTINGS, "cycle_items_auto": False,
                          "annotation_target": 1_000_000, **settings},
             "spent_items": 0}
    if "a_days_per_month" in settings:
        entry["spread_days_per_month"] = entry["settings"].pop("a_days_per_month")
    return entry


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


def test_b_walks_on_with_the_spreads_unused_share():
    """2026-09-05 replay: the spread's cursor was past the first month, so its
    half of every cycle was thrown away and the deep dive took one day per
    cycle (155 of an allowed 391). The deep dive now spends what the spread
    cannot: 155 + 155 + 126 = 436 of 455."""
    activity = _activity({"2026-05-06": 124, "2026-05-07": 126,
                          "2026-05-08": 155, "2026-05-09": 155})
    entry = {**_entry(cycle_items=455, sample_share=0.5), "a_cursor": "2026-05"}
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    days = {i.split("#")[0] for i in out["item_ids"]}
    assert days == {"2026-05-09", "2026-05-08", "2026-05-07"}
    assert out["b"] == 436 and out["a"] == 0
    assert out["b_cursor"] == "2026-05-07"
    assert out["last_slice"] is False and out["partial_day"] is None


def test_a_spends_the_deep_dives_unused_share_when_b_is_exhausted():
    """The other direction: with the deep dive walked off the history, the
    spread gets the whole budget (its own per-day cap still applies)."""
    activity = _activity({"2026-06-05": 80, "2026-06-20": 80, "2026-07-10": 80})
    entry = {**_entry(cycle_items=60, sample_share=0.5, a_days_per_month=2,
                      a_day_cap=50),
             "b_cursor": "2026-06-01"}                  # B has nothing left
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert out["b"] == 0
    # Its own 30 would stop after one 50-item day; the full 60 reaches a second.
    assert out["a"] == 100


def test_a_zero_share_stays_disabled_under_reallocation():
    activity = _activity({"2026-08-27": 20, "2026-07-10": 80})
    entry = _entry(cycle_items=100, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert out["a"] == 0 and out["b"] == 100


def test_last_slice_buys_part_of_a_day_and_keeps_the_cursor_on_it():
    """The plan's last step: 23 short of the target with 155-video days ahead
    used to cost a whole day (124 scraped, 85 orphaned). Only what is needed
    is cut, and the cursor stays on the day so a later target raise completes
    it first."""
    activity = _activity({"2026-05-08": 155, "2026-05-09": 155})
    entry = _entry(annotation_target=23, cycle_items=2000, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert len(out["item_ids"]) == 23
    assert {i.split("#")[0] for i in out["item_ids"]} == {"2026-05-09"}
    assert out["last_slice"] is True and out["partial_day"] == "2026-05-09"
    assert out["b_cursor"] is None                       # not walked past the day

    # The target is raised: the rest of that day comes first.
    done = out["item_ids"]
    status = _status([f"2026-05-09#{n}" for n in range(155)] + [f"2026-05-08#{n}" for n in range(155)],
                     scraped=done, annotated=done)
    again = ce.plan_cycle("c1", _entry(annotation_target=400, cycle_items=2000, sample_share=0.0),
                          activity=activity, status=status)
    first_day = {i.split("#")[0] for i in again["item_ids"][:132]}
    assert first_day == {"2026-05-09"} and len(again["item_ids"]) == 132 + 155
    assert again["partial_day"] is None


def test_mid_plan_slices_still_take_whole_days_only():
    """The partial-day rule is for the last slice only: a cycle bounded by
    cycle_items rather than the target must not split a day."""
    activity = _activity({"2026-05-08": 155, "2026-05-09": 155})
    entry = _entry(annotation_target=100_000, cycle_items=200, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert len(out["item_ids"]) == 155 and out["partial_day"] is None
    assert out["last_slice"] is False


def test_plan_cycle_inflates_the_target_clamp_by_the_yield():
    activity = _activity({"2026-05-09": 155})
    entry = _entry(annotation_target=23, cycle_items=2000, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None, expected_yield=0.85)
    assert len(out["item_ids"]) == 28 and out["yield"] == 0.85
    # And is deterministic: the same cursor and yield cut the same ids.
    again = ce.plan_cycle("c1", entry, activity=activity, status=None, expected_yield=0.85)
    assert again["item_ids"] == out["item_ids"]


def test_b_takes_quiet_days_below_the_correlations_floor_too():
    # A one-video day is still a viewing session. The floor used to stop the
    # deep dive as well as the spread, so a light viewer's collection was
    # mostly out of reach (2026-09-08: 251 videos over 124 days, 4 days of
    # 10+ → the plan bought 46 and went Idle "with nothing left").
    activity = _activity({"2026-08-26": 3, "2026-08-27": 30})
    entry = _entry(cycle_items=100, sample_share=0.0, min_day_items=10)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    days = {i.split("#")[0] for i in out["item_ids"]}
    assert days == {"2026-08-26", "2026-08-27"}
    assert len(out["item_ids"]) == 33
    assert out["b_cursor"] == "2026-08-26"


def test_a_sparse_collection_is_fully_reachable_by_the_deep_dive():
    # The prod shape: median one video per day, a handful of busier days.
    days = {f"2026-0{m}-{d:02d}": (12 if d in (7, 10) else 1)
            for m in (3, 4, 5) for d in range(1, 29)}
    activity = _activity(days)
    entry = _entry(cycle_items=400, sample_share=0.45)
    picked: set[str] = set()
    for _ in range(10):
        status = _status(activity["item_id"], scraped=picked, annotated=picked)
        out = ce.plan_cycle("c1", entry, activity=activity, status=status,
                            expected_yield=1.0)
        if out["exhausted"]:
            break
        picked.update(out["item_ids"])
        entry = {**entry, "a_cursor": out["a_cursor"], "b_cursor": out["b_cursor"]}
    # Every video is reached: the spread contributes only the busy days it is
    # allowed, the deep dive walks everything else, quiet days included.
    assert picked == set(activity["item_id"])
    assert out["exhausted"]


def test_a_alone_still_skips_days_below_the_floor():
    activity = _activity({"2026-08-26": 3, "2026-08-27": 30})
    entry = _entry(cycle_items=100, sample_share=1.0, a_days_per_month=5,
                   a_day_cap=50, min_day_items=10)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    days = {i.split("#")[0] for i in out["item_ids"]}
    assert days == {"2026-08-27"}


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
    # Two months of history; B (share 20) covers the newest day whole, then A
    # (share 80) samples days per month excluding B's — but A's own limits
    # (2 days/month, 5 per day) let it spend only 20 before it walks off the
    # end of the history, and what the spread cannot spend the deep dive walks
    # on with (2026-09-05). A's picks on days B then holds whole are dropped.
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

    assert "2026-08-27" in b_days and b_days <= {"2026-08-27", "2026-08-12",
                                                 "2026-08-11", "2026-08-10"}
    assert out["b"] == 80                            # B spent the spread's leftover
    assert not (set(a_days) & b_days)              # A never re-buys B's day
    assert a_days and all(d.startswith("2026-07") for d in a_days)
    for day, items in a_days.items():
        assert len(items) <= 5                     # a_day_cap respected
    for month in {d[:7] for d in a_days}:
        assert len([d for d in a_days if d.startswith(month)]) <= 2
    assert len(out["item_ids"]) == 80 + sum(len(v) for v in a_days.values())


def test_a_keeps_its_share_when_it_can_spend_it():
    """Reallocation only moves budget a process cannot spend: with months to
    spare, the spread keeps its 80 and the deep dive its 20."""
    days = {f"2026-0{m}-{d:02d}": 40 for m in (3, 4, 5, 6, 7, 8) for d in (5, 15, 25)}
    activity = _activity(days)
    entry = _entry(cycle_items=100, sample_share=0.8,
                   a_days_per_month=2, a_day_cap=20)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert out["b"] == 40                            # one 40-video day, B's oversized first
    assert out["a"] == 80                            # 2 months x 2 days x 20


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


def test_remaining_target_clamps_the_last_slice_to_part_of_a_day():
    activity = _activity({"2026-08-27": 30})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids[:4], annotated=ids[:4])
    # 4 annotated, target 10 → 6 of headroom, and cycle_items (100) is not
    # what bounds the slice: this is the plan's LAST slice, the one place a
    # day may be bought in part (2026-09-05: a whole 124-video day was scraped
    # to annotate 23). The cursor stays on the day.
    entry = _entry(annotation_target=10, cycle_items=100, sample_share=0.0)
    out = ce.plan_cycle("c1", entry, activity=activity, status=status)
    assert len(out["item_ids"]) == 6
    assert out["last_slice"] is True and out["partial_day"] == "2026-08-27"
    assert out["b_cursor"] is None

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


def test_the_spreads_density_is_derived_not_a_setting():
    """Two quantity knobs (a target and a days-per-month limit) had to agree or
    one won silently — on 2026-09-09 a 4,400 target sat above what 9 x 40 over
    six months could buy, and the plan idled short by construction. The cap
    stays (it says what one sampled day is worth); the density follows from
    the target. A stored value from an old ledger entry is dropped."""
    assert "a_days_per_month" not in ce.DEFAULT_SETTINGS
    assert ce.DEFAULT_SETTINGS["a_day_cap"] == 50
    assert "a_days_per_month" not in ce.normalize_settings({"a_days_per_month": 9})


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
    # The batch-size hold and the run-record seeding are exercised by their
    # own tests; every older tick test wants a 3-item handoff to start the
    # annotator at once, and the bare consolidate task_args it always asserted.
    monkeypatch.setattr(sup, "MIN_ANNOTATE_BATCH", 1)
    monkeypatch.setattr(sup, "_seed_consolidation_run", lambda task_args: None)
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
    # The spread-density derivation reads enrichment status; none here.
    monkeypatch.setattr(ce, "load_status", lambda ids: None)
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
    # The next slice is cut and its scraper started FIRST, so the annotation
    # lane — which runs last — knows more scrapes are coming when it decides
    # whether a small queue waits; here the fixture's batch floor is 1.
    assert started == ["queue_scraper_tiktok", "queue_annotator"]
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
    tick["scrape_busy"] = {"tiktok"}      # no new slice this tick: the annotator alone

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
    """min(target headroom − pending, ONE annotation job) — the loop serialises
    on consolidation, so a slice larger than the scraper can feed during one
    job's turnaround only delays the first annotation."""
    import web_interface.run_enrichment_supervisor as sup
    from web_interface.run_queue_annotator_batch import DEFAULT_BATCH_SIZE
    cap = DEFAULT_BATCH_SIZE

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


def test_auto_cycle_items_is_sized_for_the_expected_yield(monkeypatch, store):
    """Cutting exactly the headroom always left a shortfall (scrapes and
    annotations each lose a share) that cost a whole extra cycle — 2026-09-05's
    cycle 4 existed to cover 23 videos. The cut is inflated by the yield; the
    cap still applies."""
    import web_interface.run_enrichment_supervisor as sup
    from web_interface.run_queue_annotator_batch import DEFAULT_BATCH_SIZE

    activity = _activity({"2026-08-27": 30})
    monkeypatch.setattr(sup, "_in_flight_annotation_ids", lambda: set())
    small = _entry(annotation_target=23, cycle_items_auto=True)
    assert sup._auto_cycle_items(small, activity, None, expected_yield=0.85) == 28
    assert sup._auto_cycle_items(small, activity, None, expected_yield=1.0) == 23
    huge = _entry(annotation_target=10_000, cycle_items_auto=True)
    assert sup._auto_cycle_items(huge, activity, None, expected_yield=0.5) == DEFAULT_BATCH_SIZE
    # Nonsense yields fall back to the raw headroom rather than exploding the cut.
    assert sup._auto_cycle_items(small, activity, None, expected_yield=0) == 23
    # The safety margin (scrapes are cheap; a shortfall costs a whole cycle).
    assert sup._auto_cycle_items(small, activity, None, expected_yield=0.85,
                                 margin=0.05) == 29
    # Pending work counts toward the target when the caller passes it.
    assert sup._auto_cycle_items(small, activity, None, pending=20) == 3


def test_handoff_allows_for_the_annotations_that_will_fail(monkeypatch):
    """Handing off exactly the shortfall left the plan a few dozen short every
    time (~2% of annotations fail) and cost a whole extra cycle. The clamp
    allows for that share; the over-buy is bounded by it."""
    activity = _activity({"2026-08-27": 40})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids, downloaded=ids, annotated=ids[:30])
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)
    entry = _entry(annotation_target=35)                 # 5 short, 10 eligible
    assert len(ce.handoff_scraped("c1", entry)["ready"]) == 5
    assert len(ce.handoff_scraped("c1", entry, annotation_yield=0.98)["ready"]) == 6
    assert len(ce.handoff_scraped("c1", entry, annotation_yield=0.5)["ready"]) == 10


def test_the_last_slice_is_never_smaller_than_the_floor(monkeypatch, store):
    """2026-09-08: sized to exactly the shortfall, the tail of a plan shrank
    134 → 51 → … → 3 → 1 → 1 → 1 videos, a full scrape-consolidate-tick cycle
    each. While anything is still needed the cut is at least the floor; the
    plan may overshoot its target by that much and ends in one cycle."""
    import web_interface.run_enrichment_supervisor as sup
    monkeypatch.setattr(ce, "MIN_CYCLE_ITEMS", 200)

    activity = _activity({"2026-05-01": 300, "2026-05-02": 300})
    monkeypatch.setattr(sup, "_in_flight_annotation_ids", lambda: set())
    three_short = _entry(annotation_target=3, cycle_items_auto=True)
    assert sup._auto_cycle_items(three_short, activity, None, expected_yield=0.85) == 200
    # The floor never turns "covered" into a cut.
    assert sup._auto_cycle_items(three_short, activity, None, pending=3) == 0
    # A manual plan keeps its own, smaller, cycle size as the ceiling.
    out = ce.plan_cycle("c1", _entry(annotation_target=3, cycle_items=2000, sample_share=0.0),
                        activity=activity, status=None, expected_yield=0.85)
    assert len(out["item_ids"]) == 200 and out["last_slice"] is True
    quarter_days = _activity({"2026-05-01": 25, "2026-05-02": 25, "2026-05-03": 25})
    small = ce.plan_cycle("c1", _entry(annotation_target=3, cycle_items=50, sample_share=0.0),
                          activity=quarter_days, status=None)
    assert len(small["item_ids"]) == 50
    # And the cut is still bounded by what the collection has left.
    activity = _activity({"2026-05-01": 40})
    out = ce.plan_cycle("c1", _entry(annotation_target=3, cycle_items=2000, sample_share=0.0),
                        activity=activity, status=None)
    assert len(out["item_ids"]) == 40


def test_handoff_annotates_what_the_plan_itself_scraped(monkeypatch):
    """The floor's overshoot must not be scraped for nothing: the plan's own
    slice passes the handoff whatever the target still needs, while the
    backlog sweep (videos scraped by anything else) stays bounded by it."""
    monkeypatch.setattr(ce, "MIN_CYCLE_ITEMS", 200)
    activity = _activity({"2026-08-27": 40})
    ids = list(activity["item_id"])
    status = _status(ids, scraped=ids, downloaded=ids, annotated=ids[:30])
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)
    # 5 short; the plan's slice was ids[32:40] (8 videos), backlog ids[30:32].
    entry = {**_entry(annotation_target=35), "in_flight": ids[32:40]}
    ready = ce.handoff_scraped("c1", entry)["ready"]
    assert set(ids[32:40]) <= set(ready) and len(ready) == 8
    # Room left over after the plan's own goes to the backlog.
    entry = {**_entry(annotation_target=39), "in_flight": ids[32:40]}
    ready = ce.handoff_scraped("c1", entry)["ready"]
    assert len(ready) == 9 and set(ids[32:40]) <= set(ready)
    # The overshoot is bounded by the floor: a target lowered mid-plan does
    # not annotate a whole in-flight slice.
    monkeypatch.setattr(ce, "MIN_CYCLE_ITEMS", 6)
    entry = {**_entry(annotation_target=35), "in_flight": ids[32:40]}
    assert len(ce.handoff_scraped("c1", entry)["ready"]) == 6
    # No target: nothing, own slice or not.
    entry = {**_entry(annotation_target=0), "in_flight": ids[32:40]}
    assert ce.handoff_scraped("c1", entry)["ready"] == []


def test_plan_cycle_counts_pending_annotations_toward_the_target():
    """2026-09-05, 14:06: 581 videos were queued for annotation and 25 more
    were needed, but the clamp read the target as 606 away and cut a whole
    103-video day for them. With the pending work counted, this is the last
    slice and only what is needed is cut."""
    activity = _activity({"2026-05-01": 103, "2026-05-02": 120})
    entry = _entry(annotation_target=606, cycle_items=2000, sample_share=0.0)
    whole = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert len(whole["item_ids"]) == 223 and whole["last_slice"] is True   # 606 > 223: everything
    tail = ce.plan_cycle("c1", entry, activity=activity, status=None, pending=581)
    assert len(tail["item_ids"]) == 25 and tail["partial_day"] == "2026-05-02"
    # And the margin inflates that cut a little.
    padded = ce.plan_cycle("c1", entry, activity=activity, status=None, pending=581, margin=0.2)
    assert len(padded["item_ids"]) == 30


def test_small_handoff_waits_for_the_next_scrape(tick, monkeypatch):
    """A Gemini batch job costs ~8 minutes however small it is. A handoff
    below the batch floor waits while the next slice is being scraped, so
    the two go in one job — the annotator is NOT started, the scraper is."""
    import web_interface.run_enrichment_supervisor as sup
    import web_interface.services.enrichment_journal as journal

    monkeypatch.setattr(sup, "MIN_ANNOTATE_BATCH", 500)
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["handoff"] = {"c1": ["x1", "x2", "x3"]}
    rep = tick["run"]()
    assert [n for n, _ in tick["started"]] == ["queue_scraper_tiktok"]
    assert rep.data[-1]["action"] == "handoff"       # holding is a footnote, not the headline
    assert "Holding 3" in rep.data[-1]["message"]
    assert ce.get_meta(sup.ANNOTATE_HELD_KEY)["queued"] == 3
    kinds = [e["kind"] for e in tick["store"][journal.JOURNAL_FILENAME]["events"]]
    assert kinds.count("annotate.held") == 1
    # A second tick while the scrape is still running holds again — silently.
    tick["scrape_busy"] = {"tiktok"}
    tick["handoff"] = {}
    tick["run"]()
    kinds = [e["kind"] for e in tick["store"][journal.JOURNAL_FILENAME]["events"]]
    assert kinds.count("annotate.held") == 1
    assert [n for n, _ in tick["started"]] == ["queue_scraper_tiktok"]


def test_held_ticks_never_strike_the_stall_guard(tick, monkeypatch):
    """A held queue is waiting on purpose; its unchanged length is not a stall.

    2026-09-08: the tail of a plan cut one-video slices, three of which scraped
    nothing, so the held queue read 384 three ticks running. The guard was
    evaluated before the hold and parked both plans although no annotator had
    run. The guard may only count runs the annotator was actually started for.
    """
    import web_interface.run_enrichment_supervisor as sup

    monkeypatch.setattr(sup, "MIN_ANNOTATE_BATCH", 500)
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["store"][ce.ANNOTATE_QUEUE_FILENAME] = ["x1", "x2", "x3"]
    tick["scrape_queues"] = {"tiktok": 1}            # more is coming, one video at a time
    tick["scrape_busy"] = {"tiktok"}
    for _ in range(4):
        rep = tick["run"]()
        assert rep.data[-1]["action"] != "annotate_stalled", rep.data[-1]
        parked = (tick["store"].get(ce.LEDGER_FILENAME) or {}).get("c1") or {}
        assert parked.get("state") != ce.STATE_BLOCKED, parked
    assert ce.get_meta("annotate_guard") is None
    assert tick["started"] == []

    # Strikes from before the hold do not carry into the run after it: with
    # the hold over (nothing more coming), the first run starts clean and the
    # guard needs two more identical runs before it parks.
    ce.set_meta("annotate_guard", {"len": 3, "strikes": 1})
    tick["run"]()                                     # held again: clears the strikes
    assert ce.get_meta("annotate_guard") is None
    tick["scrape_queues"] = {}
    tick["scrape_busy"] = set()
    monkeypatch.setattr(ce, "plan_cycle",
                        lambda cid, entry, **kw: {"item_ids": [], "a_cursor": None,
                                                  "b_cursor": None, "a": 0, "b": 0,
                                                  "exhausted": True, "platform": "tiktok"})
    rep = tick["run"]()
    assert rep.data[-1]["action"] == "annotate"
    assert [n for n, _ in tick["started"]] == ["queue_annotator"]


def test_held_queue_starts_when_nothing_more_is_coming(tick, monkeypatch):
    """The plan's tail: nothing left to scrape, so the small queue goes now."""
    import web_interface.run_enrichment_supervisor as sup

    monkeypatch.setattr(sup, "MIN_ANNOTATE_BATCH", 500)
    monkeypatch.setattr(ce, "plan_cycle",
                        lambda cid, entry, **kw: {"item_ids": [], "a_cursor": None,
                                                  "b_cursor": None, "a": 0, "b": 0,
                                                  "exhausted": True, "platform": "tiktok"})
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["store"][ce.ANNOTATE_QUEUE_FILENAME] = ["x1", "x2", "x3"]
    rep = tick["run"]()
    assert ("queue_annotator", {}) in tick["started"]
    assert rep.data[-1]["action"] == "annotate"
    assert ce.get_meta(sup.ANNOTATE_HELD_KEY) is None


def test_held_queue_starts_after_the_maximum_hold(tick, monkeypatch):
    import web_interface.run_enrichment_supervisor as sup
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(sup, "MIN_ANNOTATE_BATCH", 500)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=sup.MAX_ANNOTATE_HOLD_MIN + 5)).isoformat()
    ce.set_meta(sup.ANNOTATE_HELD_KEY, {"since": stale, "queued": 3})
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok"}}
    tick["store"][ce.ANNOTATE_QUEUE_FILENAME] = ["x1", "x2", "x3"]
    tick["scrape_queues"] = {"tiktok": 12}            # more IS coming...
    tick["run"]()
    assert ("queue_annotator", {}) in tick["started"]  # ...but it has waited long enough
    assert ce.get_meta(sup.ANNOTATE_HELD_KEY) is None


def test_loop_consolidations_get_a_run_record(monkeypatch):
    """The Refresh Pipeline chart draws the run record; the loop's own
    consolidations were started bare and the chart kept showing the run
    before (2026-09-05, 14:44)."""
    import web_interface.run_enrichment_supervisor as sup
    import web_interface.services.refresh_pipeline as rp

    seen = {}
    monkeypatch.setattr(rp, "seed_run", lambda record: seen.update(record) or record)
    monkeypatch.setattr(rp, "clear_run", lambda: seen.update({"cleared": True}))
    started = []
    monkeypatch.setattr(sup, "_start",
                        lambda name, task_args=None: (started.append((name, task_args)), (True, "ok"))[1])

    ok, _ = sup._start_consolidation({"auto_refresh": False, "plan_deferred": True})
    assert ok and seen["mode"] == "consolidate_only" and seen["origin"] == "consolidate_enrichment"
    assert seen["started_by"] == "automatic enrichment" and seen["in_flight"] is True
    name, task_args = started[0]
    assert name == "consolidate_enrichment"
    assert task_args["pipeline_run_id"] == seen["run_id"] and task_args["plan_deferred"] is True

    # A refused dispatch clears the record rather than leaving it in flight.
    monkeypatch.setattr(sup, "_start", lambda name, task_args=None: (False, "refused"))
    ok, _ = sup._start_consolidation({"auto_refresh": False, "plan_deferred": True})
    assert not ok and seen.get("cleared") is True


def test_expected_yield_is_measured_from_the_plans_history(store):
    """scrape OK/attempted x annotation OK/attempted over the last runs the
    plan shares; the default until there is enough history."""
    import web_interface.run_enrichment_supervisor as sup
    import web_interface.services.enrichment_journal as journal

    assert sup._expected_yield("c1", "tiktok") == sup.DEFAULT_EXPECTED_YIELD
    journal.record("scrape.finished", "x", platform="tiktok", ok=88, permanent=10, given_up=2)
    journal.record("annotate.finished", "x", ok=98, fail=2)
    assert abs(sup._expected_yield("c1", "tiktok") - 0.88 * 0.98) < 1e-9
    # Another platform's scrapes are not this plan's evidence.
    journal.record("scrape.finished", "x", platform="instagram", ok=1, permanent=99)
    assert abs(sup._expected_yield("c1", "tiktok") - 0.88 * 0.98) < 1e-9
    # A catastrophic run cannot drive the cut above 2x the headroom.
    for _ in range(3):
        journal.record("scrape.finished", "x", platform="tiktok", ok=10, permanent=90)
    assert sup._expected_yield("c1", "tiktok") == 0.5


def test_tick_auto_mode_injects_the_effective_cycle_items(tick, monkeypatch):
    import web_interface.run_enrichment_supervisor as sup  # noqa: F401
    seen = {}

    def fake_cycle(cid, entry, **kw):
        seen["cycle_items"] = entry["settings"]["cycle_items"]
        return {"item_ids": ["i1"], "a_cursor": None, "b_cursor": "2026-08-27",
                "a": 0, "b": 1, "exhausted": False, "platform": "tiktok"}

    monkeypatch.setattr(ce, "plan_cycle", fake_cycle)
    monkeypatch.setattr(ce, "load_status", lambda ids: None)
    monkeypatch.setattr(sup, "_expected_yield", lambda cid, platform: 1.0)
    monkeypatch.setattr(sup, "CUT_MARGIN", 0.0)   # the margin has its own test
    tick["plans"] = {"c1": _entry(annotation_target=500, cycle_items_auto=True,
                                  cycle_items=400)}
    tick["run"]()
    assert seen["cycle_items"] == 500          # headroom, not the manual 400
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["last_auto_cycle_items"] == 500


def test_normalize_settings_always_sizes_cycles_automatically():
    """The manual items-per-cycle knob is gone (2026-09-09): a saved False,
    from a plan armed before, is ignored on the next save."""
    assert ce.normalize_settings({"cycle_items_auto": True})["cycle_items_auto"] is True
    assert ce.normalize_settings({"cycle_items_auto": False})["cycle_items_auto"] is True
    # Auto is the default a new plan starts with: the panel shows the server
    # defaults for a collection with no plan, so this is what the RA sees.
    assert ce.normalize_settings({})["cycle_items_auto"] is True
    assert ce.DEFAULT_SETTINGS["cycle_items_auto"] is True


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
    plan = {**_entry(), "platform": "tiktok"}
    # The ledger holds the plan, as in prod: an entry the handoff's save had to
    # create from scratch would carry the server defaults (Auto, no target)
    # instead of this plan's own settings.
    ce.save_plan("c1", plan)
    tick["plans"] = {"c1": plan}
    tick["handoff"] = {"c1": ["x1"]}
    tick["run"]()
    drains = [e for e in tick["store"][journal.JOURNAL_FILENAME]["events"]
              if e["kind"] == "queue.drained" and e.get("platform") == "tiktok"]
    assert drains and drains[-1]["detail"]["plan_items"] == 5
    assert drains[-1]["detail"]["other_items"] == 0
    assert "queued elsewhere" not in drains[-1]["message"]


def test_worker_completion_ticks_the_loop_while_it_owes_work(store, monkeypatch):
    """The trigger, not the tick: with nothing armed, a worker's completion
    used to dispatch no tick at all — so the settle_owed path never ran until
    the hourly heartbeat, and a plan's last batch sat unconsolidated on the
    Dataset Assembly page for the rest of the hour (2026-09-05, 12:44)."""
    import web_interface.routes.process_routes as pr
    import web_interface.run_enrichment_supervisor as sup
    import web_interface.services.downstream_refresh as dr

    dispatched = []
    monkeypatch.setattr("web_interface.process_manager._dispatch_cloud_task",
                        lambda name, args, **kw: (dispatched.append(name), (True, "ok"))[1])
    monkeypatch.setattr(ce, "armed_plans", lambda: {})
    monkeypatch.setattr(dr, "get_deferred_impact", lambda: None)

    pr._tick_enrichment_supervisor("queue_annotator_batch")
    assert dispatched == []                              # nothing armed, nothing owed

    ce.set_meta(sup.SETTLE_OWED_KEY, {"after": "annotate"})
    pr._tick_enrichment_supervisor("queue_annotator_batch")
    assert dispatched == ["enrichment_supervisor"]      # owed a consolidation
    assert pr.loop_owes_work()["settle"] is True

    ce.set_meta(sup.SETTLE_OWED_KEY, None)
    monkeypatch.setattr(dr, "get_deferred_impact",
                        lambda: {"from_plan": True, "deferred_since": "2026-09-05T02:34:13+00:00"})
    pr._tick_enrichment_supervisor("consolidate_enrichment")
    assert dispatched == ["enrichment_supervisor"] * 2  # owed its deferred refresh
    assert pr.loop_owes_work()["refresh"] is True

    # An operator's own deferral is not the loop's to spend — no tick for it.
    monkeypatch.setattr(dr, "get_deferred_impact", lambda: {"from_plan": False})
    pr._tick_enrichment_supervisor("consolidate_enrichment")
    assert len(dispatched) == 2


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
    out = ce.normalize_settings({"a_day_cap": 3})
    assert out["a_day_cap"] == 10          # never below the analysis floor
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
        "dm-enrich-tick-btn": "dmEnrichTick",
        "dm-enrich-history-toggle": "dmEnrichHistoryToggle",
        # The modal's own controls, which live in the same template and are
        # just as easy to strand. Save is the BULK edit's apply button (a
        # single collection autosaves and never shows it), and the persona is
        # a disclosure that renders on first expand.
        "save-collection-btn": "dm_saveAnnotation",
        "delete-collection-btn": "dm_deleteCollection",
        "edit-collection-details-toggle": "dmToggleCollectionDetails",
    }
    for element_id, handler in expected.items():
        attrs = parser.by_id.get(element_id)
        assert attrs is not None, f"{element_id} missing from the template"
        assert handler in (attrs.get("onclick") or ""), \
            f"{element_id} lost its onclick ({handler})"

    # The Arm/Run tooltips live on WRAPPER spans, not the buttons: a disabled
    # button eats its own hover tooltip in most browsers, and Run is disabled
    # precisely when the explanation is most needed (no plan, target met). A
    # tooltip moved back onto the button would go silent in exactly those
    # states.
    for wrap_id in ("dm-enrich-arm-wrap", "dm-enrich-tick-wrap"):
        attrs = parser.by_id.get(wrap_id)
        assert attrs is not None, f"{wrap_id} missing from the template"
        assert attrs.get("data-tooltip"), f"{wrap_id} lost its data-tooltip"
        assert "meta-tooltip" in (attrs.get("class") or ""), \
            f"{wrap_id} lost the meta-tooltip class"
    for btn_id in ("dm-enrich-arm-btn", "dm-enrich-tick-btn"):
        attrs = parser.by_id[btn_id]
        assert not attrs.get("data-tooltip"), \
            f"{btn_id} must not carry the tooltip — it sits on the wrapper span"

    # Every plan setting is visible, always: the balance and the spread limits
    # decide what a run can ever reach, so a disclosure hid the one explanation
    # for a target the plan could not meet.
    assert "dm-enrich-advanced" not in src, \
        "the plan settings must not go back behind a disclosure"

    # Display ID is the one field that cannot write on every keystroke, so it
    # commits on blur and on Enter. Losing either handler leaves an edit that
    # looks saved and is not.
    disp = parser.by_id["edit-collection-display-id"]
    assert "dmDisplayIdInput" in (disp.get("oninput") or "")
    assert "dmDisplayIdCommit" in (disp.get("onblur") or "")
    assert "Enter" in (disp.get("onkeydown") or "")

    js = (Path(__file__).resolve().parents[2] / "web_interface" / "static" / "js"
          / "data_management.js").read_text()
    for fn in ("function dmDisplayIdInput", "function dmDisplayIdCommit",
               "function _dmAutoSaveCollection", "function dmEnrichAutoSaveNow"):
        assert fn in js, f"{fn} is gone — the modal has no Save button to fall back on"

    # Arming an Idle plan whose target is already met does nothing: the
    # supervisor closes it again on its first cycle, having reset both cursors
    # and moved the run's starting line on the way. The button therefore names
    # the operator's actual next step and goes disabled, and the wrapper
    # tooltip says why — "Arm again" only warned, and did not stop the click.
    assert "'Raise the target to arm'" in js, \
        "the Arm button no longer names the next step when the target is met"
    assert "function dmEnrichArmTooltip" in js, \
        "a disabled Arm button with no reason on its wrapper is a dead end"
    # "again" is the warning word, and only Needs attention still earns it:
    # an Idle plan with headroom left restarts its walk from the newest day,
    # which is an Arm like any other.
    assert "dmEnrichState === 'blocked' ? 'Arm again'" in js, \
        "Arm again must be the Needs-attention label alone"

    # The analysis-ready sessions figure: a span beside the ready days, the
    # estimate's whole-session day take, and the readout's third clause.
    assert "dm-enrich-ready-sessions" in parser.by_id, \
        "the headline lost its analysis-ready sessions span"
    for needle in ("function _dmEnrichSessionsByDay", "function _dmEnrichDayTake",
                   "'analysis-ready sessions'", "analysis-ready session${"):
        assert needle in js, f"{needle} is gone from the modal script"
    assert "no gap longer than 15 minutes" in src, \
        "the headline tooltip no longer says what a viewing session is"


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


def _exhausted(monkeypatch):
    monkeypatch.setattr(ce, "plan_cycle",
                        lambda cid, entry, **kw: {
                            "item_ids": [], "a_cursor": "2026-03", "b_cursor": None,
                            "a": 0, "b": 0, "exhausted": True,
                            "platform": "tiktok"})


def test_an_exhausted_plan_stays_running_until_its_last_batch_settles(tick, monkeypatch):
    """user_data_tiktok_7, 2026-09-09: the boundary tick handed 1,055 videos to
    the annotator and closed the plan in the same breath, so the history read
    "Idle" two seconds before "Annotator started". The plan now waits, Running,
    until those videos are annotated and consolidated."""
    import web_interface.run_enrichment_supervisor as sup
    import web_interface.services.enrichment_journal as journal

    plan = {**_entry(), "platform": "tiktok"}
    # The fixture reads plans from `tick["plans"]` and writes patches to the
    # store; seed the store too so the merged ledger entry is whole.
    tick["store"][ce.LEDGER_FILENAME] = {"c1": dict(plan)}
    tick["plans"] = {"c1": plan}
    tick["handoff"] = {"c1": ["2026-08-27#0", "2026-08-27#1", "2026-08-27#2"]}
    _exhausted(monkeypatch)
    rep = tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_RUNNING
    assert entry[sup.FINISHING_KEY]["pending"] == 3
    assert [n for n, _ in tick["started"]] == ["queue_annotator"]
    kinds = [e["kind"] for e in tick["store"][journal.JOURNAL_FILENAME]["events"]]
    assert "plan.finishing" in kinds and "plan.done" not in kinds
    assert rep.data[-1]["action"] == "annotate"

    # A second tick while the job runs neither closes the plan nor repeats the line.
    tick["plans"] = {"c1": entry}
    tick["handoff"] = {}
    tick["annotate_busy"] = True
    tick["started"].clear()
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_RUNNING
    kinds = [e["kind"] for e in tick["store"][journal.JOURNAL_FILENAME]["events"]]
    assert kinds.count("plan.finishing") == 1

    # The batch is annotated and consolidated: the queue is empty, nothing is
    # claimed — the plan closes on this tick, and the hold is cleared.
    tick["store"][ce.ANNOTATE_QUEUE_FILENAME] = []
    tick["annotate_busy"] = False
    tick["plans"] = {"c1": entry}
    rep = tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_DONE and entry.get("finished_at")
    assert entry.get(sup.FINISHING_KEY) is None
    kinds = [e["kind"] for e in tick["store"][journal.JOURNAL_FILENAME]["events"]]
    assert kinds[-1] == "plan.done"
    assert tick["started"] == []


def test_a_met_target_also_waits_for_the_videos_in_flight(tick, monkeypatch):
    """The Auto target-met exit closes the plan the same way: not while any of
    the collection's videos are inside an annotation job."""
    import web_interface.run_enrichment_supervisor as sup

    tick["plans"] = {"c1": {**_entry(annotation_target=100, cycle_items_auto=True),
                            "platform": "tiktok"}}
    monkeypatch.setattr(ce, "load_status", lambda ids: None)
    monkeypatch.setattr(ce, "_annotated_unique", lambda activity, status: 100)
    tick["store"][ce.LEDGER_FILENAME] = {"c1": dict(tick["plans"]["c1"])}
    tick["claimed"] = {"2026-08-27#0", "2026-08-27#1"}
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_RUNNING
    assert entry[sup.FINISHING_KEY]["pending"] == 2

    tick["claimed"] = set()
    tick["plans"] = {"c1": entry}
    tick["run"]()
    assert tick["store"][ce.LEDGER_FILENAME]["c1"]["state"] == ce.STATE_DONE


def test_a_finishing_plan_closes_after_the_bound(tick, monkeypatch):
    """A claim file a crashed annotator left behind must not hold a finished
    plan open for ever."""
    import web_interface.run_enrichment_supervisor as sup
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc)
             - timedelta(hours=sup.FINISHING_MAX_H + 1)).isoformat()
    tick["plans"] = {"c1": {**_entry(), "platform": "tiktok",
                            sup.FINISHING_KEY: {"since": stale, "pending": 2}}}
    tick["claimed"] = {"2026-08-27#0", "2026-08-27#1"}
    _exhausted(monkeypatch)
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_DONE
    assert entry.get(sup.FINISHING_KEY) is None


def test_a_raised_target_puts_a_finishing_plan_back_to_work(tick, monkeypatch):
    import web_interface.run_enrichment_supervisor as sup

    plan = {**_entry(), "platform": "tiktok",
            sup.FINISHING_KEY: {"since": ce.now_iso(), "pending": 2}}
    tick["store"][ce.LEDGER_FILENAME] = {"c1": dict(plan)}
    tick["plans"] = {"c1": plan}
    tick["run"]()                       # the fixture's plan_cycle cuts 5 items
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_RUNNING and entry["cycles"] == 1
    assert entry.get(sup.FINISHING_KEY) is None


# --------------------------------------------------------------------------- #
# The spread's density is derived from the target
# --------------------------------------------------------------------------- #

def _six_months(per_day=40, days=(3, 9, 17, 24, 28)):
    return _activity({f"2026-{m:02d}-{d:02d}": per_day for m in range(3, 9) for d in days})


def test_spread_days_is_the_fewest_uniform_density_that_covers_its_share():
    """Six months of 40-video days, cap 40, only spread: 600 videos need three
    days a month (2 x 6 x 40 = 480 falls short; 3 x 6 x 40 = 720 covers it)."""
    entry = _entry(annotation_target=600, sample_share=1.0, a_day_cap=40)
    out = ce.spread_days_per_month("c1", entry, activity=_six_months(), status=None)
    assert out == {"days": 3, "videos": 600, "capacity": 720, "months": 6,
                   "exhausted": False}


def test_spread_days_measures_only_its_own_share_and_the_cut_needed():
    """Half the balance and an 80% yield: 600 x 0.5 / 0.8 = 375 must be cut
    by the spread — two days a month (480) cover it."""
    entry = _entry(annotation_target=600, sample_share=0.5, a_day_cap=40)
    out = ce.spread_days_per_month("c1", entry, activity=_six_months(), status=None,
                                   expected_yield=0.8)
    assert out["videos"] == 375 and out["days"] == 2


def test_spread_days_caps_at_the_history_and_says_so():
    entry = _entry(annotation_target=5_000, sample_share=1.0, a_day_cap=40)
    out = ce.spread_days_per_month("c1", entry, activity=_six_months(), status=None)
    assert out["days"] == 5 and out["capacity"] == 1_200 and out["exhausted"]


def test_spread_days_is_zero_with_no_spread_share_or_no_target():
    activity = _six_months()
    assert ce.spread_days_per_month("c1", _entry(sample_share=0.0), activity=activity,
                                    status=None)["days"] == 0
    assert ce.spread_days_per_month("c1", _entry(annotation_target=0), activity=activity,
                                    status=None)["days"] == 0


def test_spread_days_looks_only_at_the_months_still_ahead_of_the_cursor():
    """Mid-walk (cursor at 2026-06) only March-May remain: 300 videos over
    three months need three days a month, not two over six."""
    entry = {**_entry(annotation_target=300, sample_share=1.0, a_day_cap=40),
             "a_cursor": "2026-06"}
    out = ce.spread_days_per_month("c1", entry, activity=_six_months(), status=None)
    assert out["months"] == 3 and out["days"] == 3


def test_spread_days_skips_days_under_the_floor_and_subtracts_scraped():
    activity = _activity({"2026-07-01": 20, "2026-07-02": 5, "2026-07-03": 20})
    ids = [f"2026-07-01#{i}" for i in range(20)]
    status = _status(ids, scraped=ids[:15])
    entry = _entry(annotation_target=25, sample_share=1.0, a_day_cap=20)
    out = ce.spread_days_per_month("c1", entry, activity=activity, status=status)
    # The 5-video day never qualifies; day 1 has 5 of cap left, day 3 has 20:
    # one day (the better-ranked of the two) cannot be relied on for 25.
    assert out["days"] == 2 and out["capacity"] == 25


def test_a_higher_density_is_a_superset_of_a_lower_one():
    """stable_sample draws a prefix of stable_rank, so raising the density adds
    days to a month rather than swapping them."""
    days = [pd.Timestamp(f"2026-07-{d:02d}") for d in range(1, 20)]
    two = ce.stable_sample(days, 2, salt="c1:2026-07")
    five = ce.stable_sample(days, 5, salt="c1:2026-07")
    assert five[:2] == two
    assert ce.stable_rank(days, salt="c1:2026-07")[:5] == five


def test_plan_cycle_derives_the_density_when_none_is_stored():
    """A direct call (or a plan cut before the supervisor stored a density)
    derives it from the same inputs and reports what it used."""
    entry = _entry(cycle_items=1_000, annotation_target=600, sample_share=1.0,
                   a_day_cap=40)
    out = ce.plan_cycle("c1", entry, activity=_six_months(), status=None)
    assert out["spread_days"] == 3
    # 3 days x 40 in the newest month, walked until the 600-cut budget is met.
    assert out["a"] >= 600 - 40 and all(i.startswith("2026-0") for i in out["item_ids"])


def test_plan_cycle_honours_a_stored_density():
    entry = _entry(cycle_items=1_000, annotation_target=600, sample_share=1.0,
                   a_day_cap=40, a_days_per_month=1)
    out = ce.plan_cycle("c1", entry, activity=_six_months(), status=None)
    assert out["spread_days"] == 1 and out["a"] == 6 * 40


def test_tick_stores_the_density_once_per_walk_and_rederives_on_a_new_target(tick, monkeypatch):
    """The supervisor derives at the start of a walk and again when an input
    changes; between those the stored density holds so the sampling stays
    uniform across the history."""
    monkeypatch.setattr(ce, "load_activity", lambda cid: _six_months(per_day=10))
    plan = {**_entry(annotation_target=100, sample_share=0.5, a_day_cap=10),
            "platform": "tiktok"}
    tick["store"][ce.LEDGER_FILENAME] = {"c1": dict(plan)}
    tick["plans"] = {"c1": plan}
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["spread_days_per_month"] >= 1
    assert entry["spread_days_basis"]["target"] == 100
    first = entry["spread_days_per_month"]

    # Mid-walk with the same inputs: nothing is re-derived. (The fake queue
    # keeps what the first tick cut; empty it so the tick reaches the planner.)
    entry["a_cursor"] = "2026-07"
    entry["spread_days_per_month"] = 99          # a sentinel the derivation would never produce
    tick["plans"] = {"c1": entry}
    tick["scrape_queues"] = {}
    tick["run"]()
    assert tick["store"][ce.LEDGER_FILENAME]["c1"]["spread_days_per_month"] == 99

    # A raised target re-derives for the months still ahead.
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    entry["settings"] = {**entry["settings"], "annotation_target": 1_000}
    tick["plans"] = {"c1": entry}
    tick["scrape_queues"] = {}
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["spread_days_per_month"] != 99
    assert entry["spread_days_basis"]["target"] == 1_000
    # Ten times the target over the four months still ahead: denser than the
    # first derivation, up to the densest those months allow (5 days each).
    assert 5 >= entry["spread_days_per_month"] > first


def test_the_supervisor_sizes_every_cycle_automatically(tick, monkeypatch):
    """A ledger entry that still says cycle_items_auto=False (armed before
    the knob went) is sized like any other: the cutter receives the automatic
    size, not the stored cycle_items."""
    seen = {}

    def fake_cycle(cid, entry, **kw):
        seen["cycle_items"] = entry["settings"]["cycle_items"]
        return {"item_ids": ["x1"], "a_cursor": None, "b_cursor": "2026-08-27",
                "a": 0, "b": 1, "exhausted": False, "platform": "tiktok"}

    monkeypatch.setattr(ce, "plan_cycle", fake_cycle)
    tick["plans"] = {"c1": {**_entry(annotation_target=150, cycle_items=7),
                            "platform": "tiktok"}}
    tick["run"]()
    # 150 to annotate at the default 85% yield plus the 5% margin: 186 to cut.
    assert seen["cycle_items"] == 186


# --------------------------------------------------------------------------- #
# The time estimate's measured timings, and a finished run's frozen meter
# --------------------------------------------------------------------------- #

def _journal_doc(events):
    import web_interface.services.enrichment_journal as journal
    return {"version": journal.VERSION, "events": events}


def test_expected_timing_falls_back_to_the_defaults(store):
    out = ce.expected_timing("c1", "tiktok")
    assert {k: out[k] for k in ce.DEFAULT_TIMING} == ce.DEFAULT_TIMING
    assert out["measured"] == {"scrape": False, "annotate": False, "consolidate": False}


def test_expected_timing_is_measured_from_the_collections_runs(store):
    """user_data_tiktok_7 on 2026-09-09: 1,289 scraped in 18 min, 1,055
    annotated in 20 min, consolidations of 1 and 2.5 min. The tiny 4-video
    retry batch is ignored (it says nothing about the rate)."""
    import web_interface.services.enrichment_journal as journal
    ev = [
        {"ts": "2026-09-09T04:15:52+00:00", "kind": "queue.drained", "platform": "tiktok",
         "detail": {"queued": 1289}},
        {"ts": "2026-09-09T04:33:52+00:00", "kind": "scrape.finished", "platform": "tiktok",
         "detail": {"worker": "queue_scraper_tiktok", "ok": 1056, "permanent": 228, "transient": 5}},
        {"ts": "2026-09-09T04:33:55+00:00", "kind": "queue.drained", "platform": "tiktok",
         "detail": {"queued": 4}},
        {"ts": "2026-09-09T04:34:11+00:00", "kind": "scrape.finished", "platform": "tiktok",
         "detail": {"worker": "queue_scraper_tiktok", "ok": 0, "permanent": 0, "transient": 4}},
        {"ts": "2026-09-09T04:34:29+00:00", "kind": "refresh.finished",
         "detail": {"origin": "Consolidate enrichment data", "studies": 0,
                    "started_ts": "2026-09-09T04:33:29+00:00"}},
        {"ts": "2026-09-09T04:34:39+00:00", "kind": "queue.drained",
         "detail": {"worker": "queue_annotator_batch", "queued": 1055}},
        {"ts": "2026-09-09T04:54:39+00:00", "kind": "annotate.finished",
         "detail": {"worker": "queue_annotator_batch", "ok": 1040, "fail": 15}},
        {"ts": "2026-09-09T04:56:52+00:00", "kind": "refresh.finished",
         "detail": {"origin": "Consolidate enrichment data", "studies": 0,
                    "started_ts": "2026-09-09T04:54:22+00:00"}},
        # The full downstream refresh is not a consolidation.
        {"ts": "2026-09-09T05:09:00+00:00", "kind": "refresh.finished",
         "detail": {"origin": "Consolidate enrichment data", "studies": 14,
                    "started_ts": "2026-09-09T04:56:48+00:00"}},
    ]
    store[journal.JOURNAL_FILENAME] = _journal_doc(ev)
    out = ce.expected_timing("c1", "tiktok")
    assert out["measured"] == {"scrape": True, "annotate": True, "consolidate": True}
    assert out["scrape_per_min"] == round(1289 / 18, 1)
    assert out["annotate_fixed_min"] == round(20 - 1055 * 0.01, 1)   # 9.5
    assert out["consolidate_min"] == 2.5                               # median of 1.0, 2.5


def test_closing_a_plan_stamps_where_the_run_ended(tick, monkeypatch):
    """The meter of a finished run must read the run's own target and end
    count — it used to slide with the target slider afterwards."""
    tick["plans"] = {"c1": {**_entry(annotation_target=100, cycle_items_auto=True),
                            "platform": "tiktok"}}
    monkeypatch.setattr(ce, "load_status", lambda ids: None)
    monkeypatch.setattr(ce, "_annotated_unique", lambda activity, status: 100)
    tick["run"]()
    entry = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert entry["state"] == ce.STATE_DONE
    assert entry["run_end_target"] == 100 and entry["run_end_annotated"] == 100
    assert entry["run_finished_at"]
    prog = {k: v for k, v in ce.progress("c1", entry).items() if k.startswith("run_")}
    assert prog["run_end_target"] == 100 and prog["run_end_annotated"] == 100


# --------------------------------------------------------------------------- #
# Whole viewing sessions within a cut day
# --------------------------------------------------------------------------- #

def _a_only(**settings) -> dict:
    """A plan with only the random daily sample, one wide month, the cap
    given by the test."""
    return _entry(sample_share=1.0, cycle_items=1000, a_days_per_month=5, **settings)


def _session_order(cid: str, day: str, keys) -> list:
    """The salted order the spread takes a day's sessions in."""
    return ce.stable_rank(list(keys), salt=f"{cid}:{day}:sessions")


def test_spread_takes_whole_sessions_and_the_one_crossing_the_cap():
    """Cap 20 on a day of two sittings (15 and 12 plays): the first is taken
    whole, the second crosses the cap and is taken whole too — 27 items, no
    sitting cut in half by the cut itself."""
    day = "2026-05-09"
    activity = _activity({day: [15, 12]})
    out = ce.plan_cycle("c1", _a_only(a_day_cap=20), activity=activity, status=None)
    assert len(out["item_ids"]) == 27 and out["a"] == 27
    assert out["sessions"] == 2


def test_spread_takes_sessions_in_their_own_salted_order():
    """Cap 12: exactly one sitting is taken whole — whichever the salted
    ranking puts first — and nothing else, because that sitting alone meets
    the cap (12 plays) or overshoots it (15)."""
    day = "2026-05-09"
    activity = _activity({day: [15, 12]})
    out = ce.plan_cycle("c1", _a_only(a_day_cap=12), activity=activity, status=None)
    first = _session_order("c1", day, [_session_key(day, 0), _session_key(day, 1)])[0]
    assert set(out["item_ids"]) == _session_items(activity, first)
    assert out["sessions"] == 1


def test_sittings_under_the_floor_only_ever_fill_the_cap():
    """A 5-play sitting is not a candidate (floor 10): its items arrive only
    as single-item fill after the candidates, never as a session."""
    day = "2026-05-09"
    activity = _activity({day: [12, 5, 30]})
    small = _session_items(activity, _session_key(day, 1))
    # Cap 12: the first candidate (12 or 30) meets the cap alone; no fill.
    out = ce.plan_cycle("c1", _a_only(a_day_cap=12), activity=activity, status=None)
    assert not (set(out["item_ids"]) & small)
    assert out["sessions"] == 1
    # The floor itself is a parameter: at 5 the small sitting is a session.
    out5 = ce.plan_cycle("c1", _a_only(a_day_cap=60), activity=activity, status=None,
                         session_min_plays=5)
    assert out5["sessions"] == 3 and len(out5["item_ids"]) == 47


def test_single_items_fill_the_cap_after_the_sessions():
    """One candidate sitting of 12 plus three sittings of 3, cap 18: the
    candidate whole, then six single items from the small ones."""
    day = "2026-05-09"
    activity = _activity({day: [12, 3, 3, 3]})
    out = ce.plan_cycle("c1", _a_only(a_day_cap=18), activity=activity, status=None)
    assert len(out["item_ids"]) == 18
    assert _session_items(activity, _session_key(day, 0)) <= set(out["item_ids"])
    assert out["sessions"] == 1
    # The fill is the same salted item draw as before, over what is left.
    rest = sorted(set(activity["item_id"]) - _session_items(activity, _session_key(day, 0)))
    fill = ce.stable_sample(rest, 6, salt=f"c1:{day}")
    assert set(out["item_ids"]) - _session_items(activity, _session_key(day, 0)) == set(fill)


def test_rows_without_a_session_take_the_item_draw_as_before():
    day = "2026-05-09"
    activity = _activity({day: 30})
    out = ce.plan_cycle("c1", _a_only(a_day_cap=10), activity=activity, status=None)
    assert out["item_ids"] == ce.stable_sample(sorted(activity["item_id"]), 10, salt=f"c1:{day}")
    assert out["sessions"] == 0


def test_raising_the_cap_adds_sessions_and_never_swaps_them():
    day = "2026-05-09"
    activity = _activity({day: [15, 12, 20]})
    low = set(ce.plan_cycle("c1", _a_only(a_day_cap=12), activity=activity, status=None)["item_ids"])
    high = set(ce.plan_cycle("c1", _a_only(a_day_cap=30), activity=activity, status=None)["item_ids"])
    assert low < high


def test_a_sitting_past_midnight_is_taken_whole_from_the_day_it_started():
    """A 16-play sitting starting 23:50 on the 9th (12 items that day, 4 on
    the 10th) plus a 10-play sitting on the 10th, cap 10, both days sampled:
    the straddler comes whole with its next-day items, the 10th's own
    sitting comes whole, and nothing is counted twice."""
    key9, key10 = "2026-05-09T23:50:00", "2026-05-10T10:00:00"
    rows = []
    for i in range(12):
        rows.append({"item_id": f"s9#{i}", "day": pd.Timestamp("2026-05-09"),
                     "source_platform": "tiktok", "session": key9, "session_plays": 16})
    for i in range(12, 16):
        rows.append({"item_id": f"s9#{i}", "day": pd.Timestamp("2026-05-10"),
                     "source_platform": "tiktok", "session": key9, "session_plays": 16})
    for i in range(10):
        rows.append({"item_id": f"s10#{i}", "day": pd.Timestamp("2026-05-10"),
                     "source_platform": "tiktok", "session": key10, "session_plays": 10})
    activity = pd.DataFrame(rows)
    out = ce.plan_cycle("c1", _a_only(a_day_cap=10), activity=activity, status=None)
    ids = out["item_ids"]
    assert len(ids) == len(set(ids)) == 26 and out["a"] == 26
    assert out["sessions"] == 2


def test_a_sitting_starting_before_the_earliest_date_is_not_a_candidate():
    """The earliest date floors the DAYS; a sitting that started the evening
    before it is not a candidate (its in-window items go item by item), and
    nothing from the floored day is taken at all."""
    key8, key9 = "2026-05-08T23:50:00", "2026-05-09T10:00:00"
    rows = []
    for i in range(6):
        rows.append({"item_id": f"s8#{i}", "day": pd.Timestamp("2026-05-08"),
                     "source_platform": "tiktok", "session": key8, "session_plays": 12})
    for i in range(6, 12):
        rows.append({"item_id": f"s8#{i}", "day": pd.Timestamp("2026-05-09"),
                     "source_platform": "tiktok", "session": key8, "session_plays": 12})
    for i in range(10):
        rows.append({"item_id": f"s9#{i}", "day": pd.Timestamp("2026-05-09"),
                     "source_platform": "tiktok", "session": key9, "session_plays": 10})
    activity = pd.DataFrame(rows)
    out = ce.plan_cycle("c1", _a_only(a_day_cap=10, earliest_date="2026-05-09"),
                        activity=activity, status=None)
    assert set(out["item_ids"]) == {f"s9#{i}" for i in range(10)}
    assert out["sessions"] == 1


def test_deep_dive_partial_last_day_takes_the_newest_sitting_whole():
    """The plan's last slice needs 5 more of a day with sittings of 12 (08:00)
    and 10 (09:00): the newest sitting is taken whole — 10, not 5 — and the
    cursor stays on the day."""
    day = "2026-05-09"
    activity = _activity({day: [12, 10]})
    entry = _entry(sample_share=0.0, cycle_items=100, annotation_target=5)
    out = ce.plan_cycle("c1", entry, activity=activity, status=None)
    assert out["last_slice"] is True and out["partial_day"] == day
    assert set(out["item_ids"]) == _session_items(activity, _session_key(day, 1))
    assert out["sessions"] == 1 and out["b_cursor"] is None


def test_deep_dive_whole_days_count_their_sittings():
    day = "2026-05-09"
    activity = _activity({day: [12, 12, 3]})
    out = ce.plan_cycle("c1", _entry(sample_share=0.0, cycle_items=100),
                        activity=activity, status=None)
    assert len(out["item_ids"]) == 27 and out["sessions"] == 2


def test_session_cut_is_deterministic_under_shuffled_rows():
    days = {f"2026-0{m}-{d:02d}": [9, 12, 15] for m in (5, 6) for d in (3, 9, 17)}
    activity = _activity(days)
    entry = _entry(cycle_items=80, sample_share=0.5, a_days_per_month=2, a_day_cap=14)
    out1 = ce.plan_cycle("c1", entry, activity=activity, status=None)
    shuffled = activity.sample(frac=1.0, random_state=11).reset_index(drop=True)
    out2 = ce.plan_cycle("c1", entry, activity=shuffled, status=None)
    assert out1["item_ids"] == out2["item_ids"] and out1["sessions"] == out2["sessions"]


def test_activity_rows_collapse_to_item_days_with_their_sitting():
    """load_activity's collapse: session start and play count come from ALL
    the sitting's rows, an observe row counts toward the sitting but not its
    plays, a replay in a later sitting the same day is one row in the first,
    and a row without a session id carries none."""
    rows = pd.DataFrame({
        "item_id": ["a", "b", "a", "c", "d", "e"],
        "day": pd.to_datetime(["2026-05-09"] * 5 + ["2026-05-10"]),
        "source_platform": ["tiktok"] * 6,
        "_ts": pd.to_datetime(["2026-05-09 10:00", "2026-05-09 10:01", "2026-05-09 22:00",
                               "2026-05-09 23:50", "2026-05-09 23:55", "2026-05-10 00:05"]),
        "_is_play": [True, True, True, True, False, True],
        "_sid": ["c__0", "c__0", "c__1", "c__2", "c__2", None],
    })
    out = ce._collapse_to_item_days(rows.sample(frac=1.0, random_state=3))
    by_item = out.set_index(["item_id", "day"])
    assert len(out) == 5
    assert by_item.loc[("a", pd.Timestamp("2026-05-09")), "session"] == "2026-05-09T10:00:00"
    assert by_item.loc[("c", pd.Timestamp("2026-05-09")), "session"] == "2026-05-09T23:50:00"
    assert by_item.loc[("c", pd.Timestamp("2026-05-09")), "session_plays"] == 1
    assert by_item.loc[("a", pd.Timestamp("2026-05-09")), "session_plays"] == 2
    e = by_item.loc[("e", pd.Timestamp("2026-05-10"))]
    assert pd.isna(e["session"]) and e["session_plays"] == 0


def test_progress_reports_the_sessions_a_collection_can_offer(monkeypatch):
    """Three sittings on one day: one complete (annotated + one failed for
    good), one half done with a burnt annotation, one too small to count.
    The burnt item is failed, not awaiting — in the sessions figures and in
    the day series alike."""
    day = "2026-05-09"
    activity = _activity({day: [12, 12, 4]})
    s0 = sorted(_session_items(activity, _session_key(day, 0)))
    s1 = sorted(_session_items(activity, _session_key(day, 1)))
    status = _status(list(activity["item_id"]),
                     scraped=s0[:11] + s1[:8], annotated=s0[:11] + s1[:5],
                     scrape_fail=[s0[11]], annotated_fail=[s1[5]])
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: status)

    out = ce.progress("c1", _entry())
    sessions = out["sessions"]
    assert sessions["min_plays"] == ce.DEFAULT_SESSION_MIN_PLAYS
    assert sessions["total"] == 3 and sessions["candidates"] == 2
    assert sessions["ready"] == 1
    # The unfinished candidate: 4 unscraped, 2 scraped and awaiting (5
    # annotated, 1 burnt), on the day at index 0.
    assert sessions["per"] == {"d": [0], "n": [4], "w": [2]}
    daily = out["daily"]
    assert daily["awaiting"] == [2] and daily["failed"] == [2]
    assert daily["annotated"] == [16] and daily["total"] == [28]


def test_progress_without_session_columns_reports_no_sessions(monkeypatch):
    activity = _activity({"2026-05-09": 12})
    monkeypatch.setattr(ce, "load_activity", lambda cid: activity)
    monkeypatch.setattr(ce, "load_status", lambda i=None: None)
    sessions = ce.progress("c1", _entry())["sessions"]
    assert sessions == {"min_plays": ce.DEFAULT_SESSION_MIN_PLAYS, "total": 0,
                        "candidates": 0, "ready": 0, "per": {"d": [], "n": [], "w": []}}


def test_journal_names_the_sittings_a_batch_finishes(tick, monkeypatch):
    import web_interface.services.enrichment_journal as journal

    monkeypatch.setattr(ce, "plan_cycle",
                        lambda cid, entry, **kw: {
                            "item_ids": [f"{cid}-i{n}" for n in range(5)],
                            "a_cursor": "2026-07", "b_cursor": "2026-08-27",
                            "a": 1, "b": 4, "exhausted": False,
                            "platform": "tiktok", "sessions": 2})
    tick["plans"] = {"c1": {**_entry(), "spent_items": 10, "platform": "tiktok"}}
    tick["run"]()
    events = (tick["store"].get(journal.JOURNAL_FILENAME) or {}).get("events") or []
    queued = next(e for e in events if e["kind"] == "slice.queued")
    assert queued["detail"]["sessions"] == 2
    assert "2 viewing session(s)" in queued["message"]
    ledger = tick["store"][ce.LEDGER_FILENAME]["c1"]
    assert ledger["last_batch"]["sessions"] == 2


def test_journal_stays_quiet_when_a_batch_finishes_no_sitting(tick):
    import web_interface.services.enrichment_journal as journal

    tick["plans"] = {"c1": {**_entry(), "spent_items": 10, "platform": "tiktok"}}
    tick["run"]()
    events = (tick["store"].get(journal.JOURNAL_FILENAME) or {}).get("events") or []
    queued = next(e for e in events if e["kind"] == "slice.queued")
    assert queued["detail"]["sessions"] == 0
    assert "viewing session" not in queued["message"]
