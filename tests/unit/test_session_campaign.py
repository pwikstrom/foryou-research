"""Session campaign selection (web_interface/services/session_campaign.py) and the
shared scrape/annotate classification (collection_enrichment.classify_for_enrichment)."""

import pandas as pd

from web_interface.services import collection_enrichment as ce
from web_interface.services import session_campaign as sc


def _status(rows):
    """rows: item_id -> (scraped_ok, scrape_fail, video_downloaded, annotated_ok, annotated_fail)."""
    cols = ["scraped_ok", "scrape_fail", "video_downloaded", "annotated_ok", "annotated_fail"]
    df = pd.DataFrame.from_dict(rows, orient="index", columns=cols)
    df.index.name = "item_id"
    return df


def test_classify_for_enrichment():
    status = _status(
        {
            "done": (True, False, True, True, False),
            "ann": (True, False, True, False, False),
            "nomedia": (True, False, False, False, False),
            "annfail": (True, False, True, False, True),
            "dead": (False, True, False, False, False),
        }
    )
    scrape, annotate = ce.classify_for_enrichment(
        ["done", "ann", "nomedia", "annfail", "dead", "new"], status, "tiktok"
    )
    assert annotate == ["ann"]
    assert scrape == {"tiktok": ["new"]}
    # No status at all: everything still needs a scrape.
    scrape, annotate = ce.classify_for_enrichment(["a", "b"], None, "tiktok", {"b": "youtube"})
    assert scrape == {"tiktok": ["a"], "youtube": ["b"]} and annotate == []


def _plays(spec):
    """spec: list of (collection, session, month, [item ids])."""
    rows = []
    for cid, sid, month, items in spec:
        for k, iid in enumerate(items):
            rows.append(
                {
                    "collection_id": cid,
                    "session_id": sid,
                    "item_id": iid,
                    "_ts": pd.Timestamp(f"{month}-10") + pd.Timedelta(seconds=10 * k),
                }
            )
    return pd.DataFrame(rows)


def _ids(prefix, n):
    return [f"{prefix}{i}" for i in range(n)]


def _all_done(ids):
    return dict.fromkeys(ids, (True, False, True, True, False))


def test_session_table_counts_and_cost():
    plays = _plays([("c1", "s1", "2025-01", _ids("a", 10))])
    rows = _all_done(_ids("a", 7))
    rows.update({"a7": (True, False, True, False, False), "a8": (False, True, False, False, False)})
    states = sc.item_states(_ids("a", 10), _status(rows))
    t = sc.session_table(plays, states, scrape_yield=0.5, annotate_yield=1.0)
    row = t.iloc[0]
    assert (row.n_distinct, row.n_covered, row.n_to_annotate, row.n_to_scrape) == (10, 7, 1, 1)
    assert row.coverage == 0.7
    assert row.expected_coverage == (7 + 1 + 0.5) / 10
    assert row.cost == 1 + 0.5


def _scenario():
    # c1 already has one covered long session; c2 none. Each has cheap and dear candidates.
    spec, rows = [], {}
    spec.append(("c1", "c1_cov", "2025-01", _ids("p", 60)))
    rows.update(_all_done(_ids("p", 60)))
    for cid in ("c1", "c2"):
        for j, (month, missing) in enumerate([("2025-01", 10), ("2025-01", 12), ("2025-02", 8)]):
            ids = _ids(f"{cid}s{j}_", 60)
            spec.append((cid, f"{cid}_s{j}", month, ids))
            rows.update(_all_done(ids[missing:]))
            rows.update(dict.fromkeys(ids[:missing], (True, False, True, False, False)))
    plays = _plays(spec)
    states = sc.item_states(plays["item_id"].unique(), _status(rows))
    return plays, states, sc.session_table(plays, states)


def test_select_balances_participants_and_respects_month_cap():
    plays, states, table = _scenario()
    sel, summary = sc.select_sessions(table, target=2, month_cap=2, budget=1000, salt="x")
    # c2 (fewer covered) goes first; each takes its cheapest feasible candidate
    # (8 missing), then c2 takes the next cheapest (10 missing); c1 is at target.
    assert list(sel["session_id"]) == ["c2_s2", "c1_s2", "c2_s0"]
    assert summary["participants_at_target_after"] == 2
    to_scrape, to_annotate = sc.campaign_items(plays, states, sel["session_id"])
    assert to_scrape == [] and len(to_annotate) == 8 + 8 + 10


def test_select_month_cap_forces_another_month():
    _, _, table = _scenario()
    sel, _ = sc.select_sessions(table, target=3, month_cap=1, budget=1000, salt="x")
    c1 = sel[sel["collection_id"] == "c1"]
    # January is full (one covered session already): only February is open.
    assert list(c1["session_id"]) == ["c1_s2"]


def test_select_stops_at_budget_and_is_deterministic():
    _, _, table = _scenario()
    sel, summary = sc.select_sessions(table, target=5, month_cap=9, budget=4, salt="x")
    assert summary["expected_annotations"] <= 4 and summary["budget_exhausted"]
    again, _ = sc.select_sessions(table, target=5, month_cap=9, budget=4, salt="x")
    assert list(sel["session_id"]) == list(again["session_id"])


def test_infeasible_and_short_sessions_are_not_candidates():
    spec, rows = [], {}
    long_dead = _ids("d", 60)
    spec.append(("c1", "dead", "2025-01", long_dead))
    rows.update(dict.fromkeys(long_dead[:20], (False, True, False, False, False)))
    rows.update(_all_done(long_dead[20:]))
    short = _ids("s", 10)
    spec.append(("c1", "short", "2025-01", short))
    plays = _plays(spec)
    table = sc.session_table(plays, sc.item_states(plays["item_id"].unique(), _status(rows)))
    sel, _ = sc.select_sessions(table, target=5, month_cap=9, budget=1000, salt="x")
    assert sel.empty


def test_manifest_hash_is_stable():
    m = {"params": {"a": 1}, "sessions": [{"s": 1}], "to_scrape": ["x"], "to_annotate": []}
    assert sc.manifest_hash(m) == sc.manifest_hash(dict(reversed(list(m.items()))))


DONE = (True, False, True, True, False)
FAILED = (False, True, False, False, False)
TODO = (False, False, False, False, False)


def _one_session(states_in_order):
    ids = [f"v{i}" for i in range(len(states_in_order))]
    plays = _plays([("c1", "s1", "2025-01", ids)])
    rows = {i: s for i, s in zip(ids, states_in_order) if s is not TODO}
    table = sc.session_table(plays, sc.item_states(ids, _status(rows)))
    return table.iloc[0], table


def test_attempted_rule_needs_every_video_tried():
    # 85% annotated, 15% failed: covered under the attempted rule at 0.82.
    row, table = _one_session([DONE] * 85 + [FAILED] * 15)
    assert row.attempted_share == 1.0 and row.coverage == 0.85
    marked = sc.mark_rule(table, "attempted", coverage=0.82)
    assert bool(marked.iloc[0].is_covered)
    # The same session with one video never tried is not covered.
    _, table = _one_session([DONE] * 85 + [FAILED] * 14 + [TODO])
    assert not bool(sc.mark_rule(table, "attempted", coverage=0.82).iloc[0].is_covered)


def test_gap_rule_counts_consecutive_missing_plays():
    spread = ([DONE] * 6 + [FAILED]) * 13 + [DONE] * 9  # isolated failures
    row, table = _one_session(spread)
    assert row.max_missing_run == 1
    assert bool(sc.mark_rule(table, "attempted", coverage=0.82, max_gap=2).iloc[0].is_covered)
    bunched = [DONE] * 85 + [FAILED] * 3 + [DONE] * 12  # three failures in a row
    row, table = _one_session(bunched)
    assert row.max_missing_run == 3 and row.max_dead_run == 3
    marked = sc.mark_rule(table, "attempted", coverage=0.82, max_gap=2)
    assert not bool(marked.iloc[0].is_covered) and not bool(marked.iloc[0].is_feasible)


def test_preselected_sessions_count_toward_the_target():
    _, _, table = _scenario()
    first, _ = sc.select_sessions(table, target=2, month_cap=9, budget=1000, salt="x")
    second, summary = sc.select_sessions(
        table, target=2, month_cap=9, budget=1000, salt="x", preselected=set(first["session_id"])
    )
    assert second.empty and summary["preselected_long"] == len(first)


ANN_FAILED = (True, False, True, False, True)  # scraped with media, annotation failed
ANN_FAILED_NO_MEDIA = (True, False, False, False, True)


def _retry_plan(sessions, min_plays=60):
    """sessions: session id -> list of states, one distinct video each."""
    spec, rows = [], {}
    for sid, states in sessions.items():
        ids = [f"{sid}_{i}" for i in range(len(states))]
        spec.append(("c1", sid, "2025-01", ids))
        rows.update({i: s for i, s in zip(ids, states) if s is not TODO})
    plays = _plays(spec)
    status = _status(rows)
    states = sc.item_states(plays["item_id"].unique(), status)
    return sc.annotation_retry_plan(plays, status, states, coverage=0.82, min_plays=min_plays)


def test_annotation_retry_reaches_only_sessions_a_retry_can_lift():
    sessions, items = _retry_plan(
        {
            # 78 annotated + 5 retryable of 100: 83% after a retry.
            "reach": [DONE] * 78 + [ANN_FAILED] * 5 + [FAILED] * 17,
            # 70 annotated + 5 retryable: 75%, still short.
            "short": [DONE] * 70 + [ANN_FAILED] * 5 + [FAILED] * 25,
            # Already covered: nothing to do.
            "covered": [DONE] * 85 + [ANN_FAILED] * 5 + [FAILED] * 10,
            # Would reach, but one video was never tried.
            "untried": [DONE] * 78 + [ANN_FAILED] * 5 + [FAILED] * 16 + [TODO],
            # Failures without media cannot be retried.
            "nomedia": [DONE] * 78 + [ANN_FAILED_NO_MEDIA] * 5 + [FAILED] * 17,
        }
    )
    assert list(sessions["session_id"]) == ["reach"]
    assert sessions.iloc[0].n_retry == 5
    assert items == sorted(f"reach_{i}" for i in range(78, 83))


def test_annotation_retry_skips_short_sessions():
    sessions, items = _retry_plan({"s": [DONE] * 40 + [ANN_FAILED] * 5 + [FAILED] * 5})
    assert sessions.empty and items == []


# --------------------------------------------------------------------------- #
# The designed sample: a stratified random draw
# --------------------------------------------------------------------------- #


def _draw_table():
    """Two participants; participant A has long sessions in three quarters (4, 2, 1)
    and three 12-59-play sessions; B has one long session."""
    rows = []
    for k, q in enumerate(["2024-01"] * 4 + ["2024-04"] * 2 + ["2024-07"]):
        rows.append(("A", f"A_long{k}", q, 80))
    for k in range(3):
        rows.append(("A", f"A_mid{k}", "2024-02", 30))
    rows.append(("A", "A_short", "2024-02", 5))
    rows.append(("B", "B_long0", "2025-01", 100))
    t = pd.DataFrame(rows, columns=["collection_id", "session_id", "month", "n_plays"])
    t["start"] = pd.to_datetime(t["month"] + "-15")
    t["band"] = sc.session_bands(t)
    t["quarter"] = sc.quarter_of(t["start"]).to_numpy()
    return t


def test_session_bands_need_for_you_plays_for_long_sessions():
    t = _draw_table()
    assert t.set_index("session_id")["band"].to_dict()["A_short"] is None
    assert (t.loc[t["n_plays"] == 30, "band"] == "mid").all()
    fy = pd.Series({"A_long0": 40, "B_long0": 90})
    b = sc.session_bands(t, fy).set_axis(t["session_id"])
    assert b["A_long0"] is None  # 80 plays but only 40 For You plays
    assert b["B_long0"] == "long"


def test_quarter_of_handles_arrow_timestamps():
    starts = pd.Series(pd.to_datetime(["2024-01-31", "2024-04-01", "2025-12-31"]))
    expected = ["2024Q1", "2024Q2", "2025Q4"]
    assert list(sc.quarter_of(starts)) == expected
    arrow = starts.astype("timestamp[ns][pyarrow]")
    assert list(sc.quarter_of(arrow)) == expected


def test_draw_takes_quarters_in_turn_and_is_nested():
    ordered = sc.draw_order(_draw_table(), salt="s")
    a_long = ordered[(ordered["collection_id"] == "A") & (ordered["band"] == "long")]
    first3 = a_long.sort_values("draw_rank").head(3)
    # The first round takes one session from each of A's three quarters.
    assert sorted(first3["quarter"]) == ["2024Q1", "2024Q2", "2024Q3"]
    small = sc.draw_stratified(ordered, {"long": 2, "mid": 1})
    large = sc.draw_stratified(ordered, {"long": 5, "mid": 2})
    assert set(small["session_id"]) <= set(large["session_id"])
    # B has one long session and no 12-59-play one: it contributes what it has.
    assert list(large.loc[large["collection_id"] == "B", "session_id"]) == ["B_long0"]
    assert "A_short" not in set(large["session_id"])


def test_draw_is_deterministic_and_depends_on_the_salt():
    t = _draw_table()
    a = sc.draw_stratified(sc.draw_order(t, "s1"), {"long": 3, "mid": 2})
    b = sc.draw_stratified(sc.draw_order(t, "s1"), {"long": 3, "mid": 2})
    assert list(a["session_id"]) == list(b["session_id"])
    orders = {
        tuple(sc.draw_order(t, f"salt{i}").sort_values(["band", "draw_rank"])["session_id"])
        for i in range(10)
    }
    assert len(orders) > 1


def test_draw_records_inclusion_probabilities():
    d = sc.draw_stratified(sc.draw_order(_draw_table(), "s"), {"long": 4, "mid": 1})
    a_long = d[(d["collection_id"] == "A") & (d["band"] == "long")]
    per_q = a_long.groupby("quarter")["drawn_in_stratum"].first().to_dict()
    # Four draws over quarters of 4, 2 and 1 sessions: one from each, then a second
    # from one of the two quarters that still have sessions.
    assert sum(per_q.values()) == 4 and per_q["2024Q3"] == 1
    assert sorted(per_q.values()) == [1, 1, 2]
    row = a_long[a_long["quarter"] == "2024Q1"].iloc[0]
    assert row["stratum_size"] == 4
    assert row["inclusion_probability"] == row["drawn_in_stratum"] / 4


def test_reach_probability_marks_sessions_that_lost_too_many_videos():
    t = pd.DataFrame(
        {
            "session_id": ["done", "gone", "open"],
            "n_distinct": [100, 100, 100],
            "n_covered": [90, 70, 50],
            "n_to_annotate": [0, 0, 20],
            "n_to_scrape": [0, 5, 30],
        }
    )
    r = sc.reach_probability(t, scrape_yield=1.0, annotate_yield=1.0, coverage=0.82)
    r = r.set_index("session_id")
    assert r.loc["done", "p_reach"] == 1.0 and not r.loc["done", "impossible"]
    assert r.loc["gone", "impossible"] and r.loc["gone", "p_reach"] == 0.0
    assert r.loc["gone", "n_dead"] == 25
    assert r.loc["open", "p_reach"] == 1.0  # every attempt succeeds: 100%
    mid = sc.reach_probability(t, scrape_yield=0.5, annotate_yield=0.9, coverage=0.82, seed=1)
    low = sc.reach_probability(t, scrape_yield=0.2, annotate_yield=0.5, coverage=0.82, seed=1)
    p_mid = mid.set_index("session_id").loc["open", "p_reach"]  # 81.5 of 100 expected: about even
    p_low = low.set_index("session_id").loc["open", "p_reach"]  # 63 expected: hopeless
    assert 0.2 < p_mid < 0.8 and p_low == 0.0


def test_manifest_hash_unchanged_for_manifests_without_the_new_lists():
    m = {
        "params": {"a": 1},
        "sessions": [{"session_id": "s"}],
        "to_scrape": ["x"],
        "to_annotate": [],
    }
    h = sc.manifest_hash(m)
    assert sc.manifest_hash({**m, "summary": {"n": 1}}) == h
    assert sc.manifest_hash({**m, "to_reannotate": ["y"]}) != h
