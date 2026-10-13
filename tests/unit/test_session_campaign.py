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
