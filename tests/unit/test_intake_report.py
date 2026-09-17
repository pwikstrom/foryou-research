"""Unit tests for the pure aggregators in ``scripts/intake_report.py``.

Synthetic ledger, verdict and activity inputs built inline; the only I/O is
``tmp_path`` for the snapshot-config writer. Nothing here reads project
storage.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("fyp_intake_report", ROOT / "scripts" / "intake_report.py")
ir = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ir  # dataclasses resolve postponed annotations through sys.modules
_spec.loader.exec_module(ir)


def _ledger() -> dict:
    return {
        "legacy.json": {"outcome": "skipped_legacy", "raw_rows": None, "platform": None, "source": None,
                        "notes": "migrated from legacy discarded_collection_files.json"},
        "tt1.json": {"outcome": "added_as_new", "raw_rows": 1000, "processed_rows": 970, "kept_rows": 950,
                     "deduped_rows": 20, "dropped": {"not_parseable": 20, "missing_required": 10},
                     "platform": "tiktok", "source": "ddp", "tz": "Australia/Brisbane", "uploaded_at": "2026-08-01T00:00:00+00:00",
                     "user_id": "a@x", "ts_first_seen": "2026-08-01T01:00:00+00:00"},
        "tt2.json": {"outcome": "merged_with_existing", "raw_rows": 500, "processed_rows": 500, "kept_rows": 100,
                     "deduped_rows": 400, "dropped": {}, "platform": "tiktok", "source": "ddp",
                     "merged_with_siblings": ["tt1.json"], "collection_id": "c1", "uploaded_at": "2026-08-05T00:00:00+00:00",
                     "user_id": "b@x"},
        "yt1.zip": {"outcome": "added_as_new", "raw_rows": 200, "processed_rows": 200, "kept_rows": 200,
                    "deduped_rows": 0, "dropped": {}, "platform": "youtube", "source": "ddp",
                    "notes": "Uploader withheld: Comments, Post | Time zone: 12 row(s) carry an ambiguous abbreviation (IST); read as its most common Takeout meaning.",
                    "uploaded_at": "2026-08-10T00:00:00+00:00", "user_id": "c@x"},
        "ig1.zip": {"outcome": "load_failed", "platform": "instagram", "source": "ddp", "raw_rows": 0},
        "old.json": {"outcome": "added_as_new", "raw_rows": 50, "processed_rows": 50, "kept_rows": 50,
                     "deduped_rows": 0, "dropped": {}, "platform": "tiktok", "source": "ddp"},
        "prebreak.json": {"outcome": "added_as_new", "raw_rows": 100, "processed_rows": None, "kept_rows": 90,
                          "deduped_rows": None, "dropped": None, "platform": "tiktok", "source": "ddp"},
    }


def test_attrition_by_route_counts_legacy_files_without_rows():
    att = ir.attrition_by_route(_ledger())
    assert att["unknown"]["files"] == 1 and att["unknown"]["files_without_counts"] == 1
    tt = att["tiktok_ddp"]
    assert tt["files"] == 4 and tt["rows_read"] == 1650
    assert tt["dropped_not_parseable"] == 20 and tt["dropped_missing_required"] == 10
    assert tt["deduped"] == 420 and tt["kept"] == 1190
    assert tt["files_without_breakdown"] == 1 and tt["unattributed_pre_breakdown"] == 10
    assert tt["unaccounted"] == 0
    assert tt["kept_pct"] == pytest.approx(72.12, abs=0.01)
    assert att["all"]["files"] == 7
    assert att["instagram_ddp"]["rows_read"] == 0 and att["instagram_ddp"]["kept_pct"] is None


def test_outcomes_by_route():
    out = ir.outcomes_by_route(_ledger())
    assert out["tiktok_ddp"] == {"added_as_new": 3, "merged_with_existing": 1}
    assert out["all"]["load_failed"] == 1


def _verdicts() -> dict:
    return {
        "f3": {"status": "learning", "platform": "tiktok", "source": "ddp", "variant": None, "findings": [],
               "processed_stats": None, "ts_evaluated": "2026-08-01T00:00:00+00:00"},
        "f4": {"status": "ok", "platform": "tiktok", "source": "ddp", "variant": None, "findings": [],
               "processed_stats": {"kept_ratio": 0.9}, "ts_evaluated": "2026-08-02T00:00:00+00:00"},
        "f5": {"status": "quarantined", "platform": "tiktok", "source": "ddp", "variant": None,
               "findings": [{"layer": "structure", "code": "type_changed", "severity": "quarantine", "detail": "x retyped"}],
               "processed_stats": None, "ts_evaluated": "2026-08-03T00:00:00+00:00"},
        "f6": {"status": "approved", "platform": "tiktok", "source": "ddp", "variant": None,
               "findings": [{"layer": "structure", "code": "new_key_paths", "severity": "warn", "detail": "2 new"}],
               "processed_stats": {"kept_ratio": 0.8}, "ts_evaluated": "2026-08-20T00:00:00+00:00",
               "reviewed_at": "2026-08-12T00:00:00+00:00", "review_action": "approve", "reviewed_by": "admin",
               "withheld_sections": ["Comments"]},
        "f7": {"status": "rejected", "platform": "youtube", "source": "ddp", "variant": "reviewed",
               "findings": [{"layer": "stats", "code": "stat_outlier_hard", "severity": "quarantine", "detail": "z=5", "metric": "kept_ratio"}],
               "processed_stats": {"kept_ratio": 0.1}, "ts_evaluated": "2026-08-15T00:00:00+00:00",
               "reviewed_at": "2026-08-16T00:00:00+00:00", "review_action": "reject", "reviewed_by": "admin"},
    }


def _baselines() -> dict:
    return {
        "tiktok_ddp": {"n_accepted": 5, "learned_files": ["f1", "f2", "f3", "f4", "f6"],
                       "accepted_structures": [{"filename": "f6", "approved_by": "admin", "ts": "2026-08-12T00:00:00+00:00"}]},
        "youtube_ddp__reviewed": {"n_accepted": 0, "learned_files": [], "accepted_structures": []},
    }


def test_sentinel_denominators():
    d = ir.sentinel_denominators(_verdicts(), _baselines())
    tt = d["tiktok_ddp"]
    assert tt["n_verdicts"] == 4 and tt["n_learning"] == 1
    assert tt["n_structure_eligible"] == 3 and tt["n_stats_eligible"] == 2
    assert tt["n_bootstrapped"] == 2  # f1, f2 have no verdict
    assert d["youtube_ddp__reviewed"]["n_verdicts"] == 1
    assert d["all"]["n_verdicts"] == 5


def test_quarantine_rows_and_summary():
    ledger = {
        "f6": {"uploaded_at": "2026-08-10T00:00:00+00:00", "ts_first_seen": "2026-08-21T00:00:00+00:00"},
        "f5": {"ts_first_seen": "2026-08-03T12:00:00+00:00"},
        "f7": {},
    }
    commits = [{"sha": "abc12345", "date": "2026-08-11T00:00:00+00:00", "subject": "fix parser"},
               {"sha": "def12345", "date": "2026-09-01T00:00:00+00:00", "subject": "later"}]
    rows = ir.quarantine_rows(_verdicts(), ledger, _baselines(), commits)
    by = {r["filename"]: r for r in rows}
    assert set(by) == {"f5", "f6", "f7"}
    # ts_evaluated (08-20) is later than reviewed_at (08-12): start comes from uploaded_at.
    assert by["f6"]["start_source"] == "uploaded_at" and by["f6"]["days_in_quarantine"] == 2.0
    assert by["f6"]["in_accepted_structures"] is True and by["f6"]["was_warn_only"] is True
    assert "abc12345" in by["f6"]["parser_commits"] and "def12345" not in by["f6"]["parser_commits"]
    assert by["f6"]["withheld"] == "Comments" and by["f6"]["suggested_class"] == ""
    assert by["f5"]["suggested_class"] == "e" and by["f5"]["start_source"] == "ts_evaluated"
    assert by["f7"]["suggested_class"] == "d" and by["f7"]["start_source"] == "ts_evaluated"
    assert by["f7"]["days_in_quarantine"] == 1.0

    summary = ir.quarantine_summary(rows)
    assert summary["classes"] == {"a": 0, "b": 0, "c": 0, "d": 1, "e": 1}
    assert summary["n_unclassified_approved"] == 1 and summary["unclassified"] == ["f6"]
    assert summary["n_pending"] == 1 and summary["days_in_quarantine_median"] == 1.5

    classified = ir.quarantine_summary(rows, {"f6": "b"})
    assert classified["classes"]["b"] == 1 and classified["n_unclassified_approved"] == 0

    # A row whose only timestamps contradict yields a negative duration, counted not clamped.
    rows2 = ir.quarantine_rows({"g": {**_verdicts()["f6"], "ts_evaluated": "2026-08-20T00:00:00+00:00"}},
                               {"g": {"ts_first_seen": "2026-08-25T00:00:00+00:00"}}, {}, [])
    assert ir.quarantine_summary(rows2)["n_negative_durations"] == 1


def test_parse_git_log_and_commits_between():
    text = "aaaa1111\t2026-08-11T10:00:00+10:00\tfix parser\nbbbb2222\t2026-08-13T10:00:00+10:00\tlater\nbroken line"
    commits = ir.parse_git_log(text)
    assert [c["sha"] for c in commits] == ["aaaa1111", "bbbb2222"]
    picked = ir.commits_between(commits, "2026-08-11T00:00:00+00:00", "2026-08-12T00:00:00+00:00")
    assert [c["sha"] for c in picked] == ["aaaa1111"]
    assert len(ir.commits_between(commits, None, None)) == 2


def test_withheld_union_counts_once():
    verdicts = {"yt1.zip": {"status": "ok", "withheld_sections": ["Comments"], "platform": "youtube", "source": "ddp"}}
    out = ir.withheld_counts(verdicts, _ledger())
    assert out["n_files_with_withheld_sections"] == 1
    assert out["sections"] == {"Comments": 1, "Post": 1}
    assert ir.parse_withheld_note("Time zone: x") == []


def test_region_of_tz_and_contingency():
    assert ir.region_of_tz("Australia/Brisbane") == "Australia"
    assert ir.region_of_tz("+05:30") == "fixed_offset"
    assert ir.region_of_tz(None) == "unknown"
    two = ir.contingency({"tiktok": (5, 20), "youtube": (2, 30)})
    assert two["test"] == "chi2" and "fisher_p" in two and two["dof"] == 1
    three = ir.contingency({"a": (5, 20), "b": (2, 30), "c": (1, 10)})
    assert three["test"] == "chi2" and "fisher_p" not in three and three["dof"] == 2
    assert ir.contingency({"a": (1, 2)})["test"] is None


def test_resolution_levels():
    res = ir.resolution_levels(_ledger())
    assert res["tiktok_ddp"] == {"inferred": 1, "supplied_zone": 1, "unknown_no_provenance": 2}
    assert res["youtube_ddp"] == {"export_native_ambiguous_label": 1}
    assert res["instagram_ddp"] == {"unknown_no_provenance": 1}


def _daytime_series(tz: str, days: int = 3) -> pd.Series:
    stamps = []
    for d in range(days):
        for h in range(6, 24):
            for m in (0, 20, 40):
                stamps.append(pd.Timestamp(f"2026-06-{10 + d:02d} {h:02d}:{m:02d}:00", tz=tz))
    return pd.Series(pd.to_datetime(stamps)).dt.tz_convert("UTC")


def test_calibrate_one_file_agrees_with_true_zone_and_flags_wrong_one():
    utc = _daytime_series("Australia/Brisbane")
    right = ir.calibrate_one_file(utc, "Australia/Brisbane")
    assert right["agree"] is True and right["zone_offset"] == 10.0
    wrong = ir.calibrate_one_file(utc, "Asia/Kolkata")
    assert wrong["off_gt_1h"] is True
    summary = ir.calibration_summary([right, wrong])
    assert summary["n_calibrated"] == 2 and summary["agree_pct"] == 50.0


def test_session_stats_matches_assign_session_ids():
    from fyp.ingest.base import assign_session_ids

    df = pd.DataFrame({
        "collection_id": ["c1"] * 6 + ["c2"] * 3,
        "utc_timestamp": pd.to_datetime([0, 100, 400, 1300, 1400, 3400, 0, 1000, 1800], unit="s", utc=True),
    })
    out = ir.session_stats(df, ir.SESSION_GAPS)
    for gap in ir.SESSION_GAPS:
        expected = assign_session_ids(df.copy(), gap_threshold_s=gap)["session_id"].nunique()
        assert out[f"gap_{gap}s"]["n_sessions"] == expected
    assert out["gap_300s"]["n_sessions"] == 6 and out["gap_1800s"]["n_sessions"] == 3


def test_comment_gap_stats():
    df = pd.DataFrame({
        "raw_file": ["r"] * 5,
        "collection_id": ["c"] * 5,
        "utc_timestamp": pd.to_datetime([0, 30, 200, 400, 1000], unit="s", utc=True),
        "activity_type": ["play", "comment", "comment", "comment", "play"],
        "item_id": ["v1", "v1", "v1", None, "v2"],
        "link_method": [None, "ffill_180s", None, None, None],
    })
    out = ir.comment_gap_stats(df, ir.COMMENT_GAPS)
    assert out["n_comments"] == 3 and out["n_comments_null_item_id"] == 1
    assert out["n_comments_marked_ffill_180s"] == 1
    # Gaps from the play: 30 s, then chain 30->200 (170 s), then 200->400 (200 s).
    assert out["window_60s"]["linked"] == 1
    assert out["window_180s"]["linked"] == 2
    assert out["window_300s"]["linked"] == 3
    assert out["window_60s"]["linked_to_preceding_play_same_collection_pct"] == pytest.approx(33.3, abs=0.1)


def _reference_overlaps(ts_sets: dict[str, set[int]]) -> dict[tuple[str, str], float]:
    names = sorted(ts_sets)
    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = len(ts_sets[a] & ts_sets[b])
            if shared:
                out[(a, b)] = shared / min(len(ts_sets[a]), len(ts_sets[b]))
    return out


def test_timestamp_overlaps_matches_reference_and_union_find():
    pl = pytest.importorskip("polars")
    sets = {"a": set(range(100)), "b": set(range(80, 200)), "c": set(range(190, 260)), "d": set(range(1000, 1010))}
    rows = [(fn, s) for fn, secs in sets.items() for s in secs]
    frame = pl.DataFrame({"raw_file": [r[0] for r in rows],
                          "utc_timestamp": [pd.Timestamp(r[1], unit="s", tz="UTC") for r in rows]})
    pairs_df = ir.timestamp_overlaps(frame)
    got = {(a, b): round(o, 6) for a, b, o in zip(pairs_df["a"].to_list(), pairs_df["b"].to_list(), pairs_df["overlap"].to_list(), strict=False)}
    assert got == {k: round(v, 6) for k, v in _reference_overlaps(sets).items()}
    pairs = [(a, b, o) for (a, b), o in got.items()]
    events = {k: len(v) for k, v in sets.items()}
    users = {"a": "u1", "b": "u2", "c": None, "d": None}
    at_01 = ir.union_find_merges(pairs, 0.1, events, users)
    assert at_01["n_merges"] == 1 and at_01["n_files_merged"] == 3
    assert at_01["n_false_merges_different_accounts"] == 1
    at_02 = ir.union_find_merges(pairs, 0.2, events, users)  # a-b overlap 0.2 is not > 0.2
    assert at_02["n_merges"] == 0
    small = ir.union_find_merges([("c", "d", 0.5)], 0.2, events, users,
                                 collection_per_file={"c": "x", "d": "y"}, shared_per_pair={("c", "d"): 2})
    assert small["n_merges_involving_small_file"] == 1
    assert small["n_merges_spanning_collections"] == 1
    assert small["n_pairs_on_two_shared_seconds_or_fewer"] == 1


def test_read_classification_validates():
    text = "filename,class\nf6,b\nf9,\n"
    assert ir.read_classification(text, {"f6"}) == {"f6": "b"}
    with pytest.raises(ValueError, match="unknown class"):
        ir.read_classification("filename,class\nf6,z\n", {"f6"})
    with pytest.raises(ValueError, match="f6"):
        ir.read_classification("filename,class\nf7,a\n", {"f6", "f7"})


def test_renderers_contain_their_labels():
    att = ir.attrition_by_route(_ledger())
    svg = ir.render_funnel_svg(att)
    assert svg.startswith("<svg") and "tiktok ddp" in svg and "kept" in svg
    report = {
        "snapshot": {"snapshot_root": "/s", "git_head": "abc", "run_at": "now"},
        "attrition": att,
        "outcomes": ir.outcomes_by_route(_ledger()),
        "sentinel": {
            "denominators": ir.sentinel_denominators(_verdicts(), _baselines()),
            "quarantine": ir.quarantine_summary(ir.quarantine_rows(_verdicts(), _ledger(), _baselines(), [])),
            "findings": ir.findings_by_layer_code(_verdicts()),
            "withheld": ir.withheld_counts(_verdicts(), _ledger()),
            "contingency": ir.quarantine_contingencies(_verdicts(), _ledger()),
        },
        "resolution": ir.resolution_levels(_ledger()),
    }
    md = ir.render_tables_md(report)
    for header in ("## 5.1", "## 5.2", "## 5.3", "structure:type_changed"):
        assert header in md
    rows = ir.worksheet_rows(ir.quarantine_rows(_verdicts(), _ledger(), _baselines(), []))
    assert list(rows[0]) == list(ir.WORKSHEET_COLUMNS)


def test_write_snapshot_config(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "config.toml").write_text('[paths]\nlocal_data = "~/x"\n')
    snap = tmp_path / "snap"
    out = tmp_path / "out"
    path = ir.write_snapshot_config(repo, snap, out)
    assert path.exists()
    overlay = tomllib.loads((path.parent / "config.local.toml").read_text())
    assert overlay["misc"]["local_mode"] is True
    assert overlay["data_io"]["use_gcs_for_data"] is False
    assert Path(overlay["paths"]["local_data"]).is_absolute()
