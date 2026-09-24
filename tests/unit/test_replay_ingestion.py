"""Unit tests for ``scripts/replay_ingestion.py``.

The pure pieces are checked against the production code they mirror: the
section census against ``TikTokDDPCollection.process_single`` and the link
census against ``derive_play_duration``. One end-to-end test runs the script
in a subprocess on a synthetic snapshot under ``tmp_path``, with its own
throwaway config, so nothing here touches project storage.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest

from fyp.ingest.base import derive_play_duration
from fyp.ingest.tiktok import TikTokDDPCollection

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("fyp_replay_ingestion", ROOT / "scripts" / "replay_ingestion.py")
rp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rp
_spec.loader.exec_module(rp)


def _ts(minutes: float) -> str:
    return (datetime(2025, 3, 1, 10, 0) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _export(n_plays: int = 12, start: float = 0.0, extra: bool = True) -> dict:
    """A small TikTok export: watch history plus sections the parser treats differently."""
    plays = [{"Date": _ts(start + i), "Link": f"https://www.tiktokv.com/share/video/{7000000000000000000 + i}/"}
             for i in range(n_plays)]
    doc = {"Your Activity": {"Watch History": {"VideoList": plays}}}
    if extra:
        plays.append({"Date": _ts(start + 50), "Link": "https://www.tiktok.com/@x/live"})  # play without a video id
        plays.append({"Date": "not a date", "Link": "https://www.tiktokv.com/share/video/1/"})
        doc["Your Activity"]["Like List"] = {"ItemFavoriteList": [
            {"Date": _ts(start + 0.5), "Link": f"https://www.tiktokv.com/share/video/{7000000000000000000}/"},
            {"Date": _ts(start + 90), "Link": f"https://www.tiktokv.com/share/video/{7000000000000000003}/"},
            {"Date": _ts(start + 91), "Link": "https://www.tiktokv.com/share/video/6999999999999999999/"},
        ]}
        doc["Your Activity"]["Share History"] = {"ShareHistoryList": [
            {"Date": _ts(start + 2.2), "SharedContent": "video",
             "Link": f"https://www.tiktokv.com/share/video/{7000000000000000002}/", "Method": "chat_head"},
            {"Date": _ts(start + 2.2), "SharedContent": "video",
             "Link": f"https://www.tiktokv.com/share/video/{7000000000000000002}/", "Method": "chat_head"},
        ]}
        doc["Comment"] = {"Comments": {"CommentsList": [{"Date": _ts(start + 4.5), "Comment": "nice"}]}}
        doc["Login History"] = {"LoginHistoryList": [{"Date": _ts(start - 5), "IP": "1.2.3.4"}]}
        doc["Post"] = {"Posts": {"VideoList": [{"Date": _ts(start - 60), "Link": "https://www.tiktokv.com/share/video/5/"}]}}
        doc["Off TikTok Activity"] = {"OffTikTokActivityDataList": [{"TimeStamp": "x", "Source": "y"}] * 4}
        doc["Direct Messages"] = {"Chat History": {"ChatHistory": {
            "Chat History with someone:": [{"Date": _ts(start + 3), "From": "a", "Content": "hi"}]}}}
    return doc


def test_order_precedence_and_monotonic_mtimes():
    t = datetime(2025, 1, 1, tzinfo=UTC)
    files = [
        {"raw_file": "b", "route": "aio", "candidates": {"aio_date": t, "table_first_added": t + timedelta(days=9)}},
        {"raw_file": "a", "route": "aio", "candidates": {"aio_date": t}},
        {"raw_file": "tiktok_ddp_20250301T000000Z_0123abcd.json", "route": "ddp",
         "candidates": {"name_stamp": rp.stamp_from_name("tiktok_ddp_20250301T000000Z_0123abcd.json"),
                        "table_first_added": t}},
        {"raw_file": "z", "route": "ddp", "candidates": {}},
    ]
    rows = rp.donation_order(files)
    assert [r["raw_file"] for r in rows] == ["a", "b", "tiktok_ddp_20250301T000000Z_0123abcd.json", "z"]
    assert [r["order_source"] for r in rows] == ["aio_date", "aio_date", "name_stamp", "none"]
    mt = [r["replay_mtime"] for r in rows]
    assert all(b > a for a, b in pairwise(mt))
    rev = rp.donation_order(files, order="reverse")
    assert [r["rank"] for r in rev] == [4, 3, 2, 1]
    assert all(b > a for a, b in pairwise(r["replay_mtime"] for r in rev))
    agree = rp.order_agreement(rows)
    assert agree["pairs"]["aio_date~table_first_added"]["median_days"] == 9
    assert agree["chosen_after_first_in_table"] == 1  # the name stamp falls after the table's first add


def test_section_census_matches_the_parser():
    export = _export()
    records = TikTokDDPCollection._walk_sections(export)
    census = rp.section_census(records, TikTokDDPCollection)
    assert census["records"] == len(records)
    assert census["viewing_records"] == 14
    assert census["section_fate"]["posted_videolist"] == "outside_whitelist"
    assert "chat history with …" in census["by_section"]
    assert not any("someone" in k for k in census["by_section"])

    parser = TikTokDDPCollection()
    df = pd.DataFrame.from_records(records)
    df["raw_file"] = "f.json"
    parser.file_stats_this_run = {"f.json": {"raw_rows": len(df), "dropped": {}}}
    out = parser.process_single(df)
    dropped = parser.file_stats_this_run["f.json"]["dropped"]
    assert census["outside_whitelist"] == dropped["outside_whitelist"]
    assert census["share_copies_merged"] == dropped["share_copies_merged"]
    not_parseable = len(df) - len(out) - dropped["outside_whitelist"] - dropped["share_copies_merged"]
    assert sum(census["not_parseable"].values()) == not_parseable
    assert census["not_parseable"] == {"unreadable_date": 1, "play_without_video_id": 1}
    assert census["kept_by_type"] == out["activity_type"].astype(str).value_counts().to_dict()


def _frame(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["minute", "activity_type", "item_id", "link_method"])
    df["utc_timestamp"] = pd.to_datetime("2025-03-01 10:00", utc=True) + pd.to_timedelta(df.pop("minute"), unit="m")
    df["extra_data"] = pd.NA
    df["raw_file"] = "f.json"
    df["activity_type"] = df["activity_type"].astype("string[pyarrow]")
    df["item_id"] = df["item_id"].astype("string[pyarrow]")
    df["link_method"] = df["link_method"].astype("string[pyarrow]")
    return df


def test_link_census_agrees_with_the_fold():
    df = _frame([
        (0, "play", "A", None),
        (0.5, "fave", "A", None),          # adjacent to its play
        (1, "play", "B", None),
        (1.2, "play", "B", None),          # a repeat play folds as a token
        (2, "comment", "B", "ffill_180s"),  # filled comment, adjacent to B
        (3, "play", "C", None),
        (60, "save", "A", None),           # A was played an hour earlier: nearest play
        (61, "share", "Z", None),          # never played in this file
        (62, "comment", None, None),       # no video id at all
        (63, "play", "D", None),
        (80, "play", "E", None),           # 17 minutes to the next row: over the cap
        (81, "fave", "E", None),
    ])
    census = rp.link_census(df)
    folded = derive_play_duration(df.copy())
    assert census["tokens_expected"] == rp.tokens_written(folded)
    assert census["by_type"]["fave"] == {"rows": 2, "adjacent": 2}
    assert census["by_type"]["save"] == {"rows": 1, "nearest_play": 1}
    assert census["by_type"]["share"] == {"rows": 1, "no_play_of_item": 1}
    assert census["by_type"]["comment"] == {"rows": 2, "adjacent": 1, "no_item_id": 1}
    assert census["nearest_dt_by_type"]["save"] == [3600.0]
    assert census["comments"]["filled_id"] == 1
    assert census["comments"]["fill_reach_seconds"] == [48.0]
    assert census["comments"]["fill_source_types"] == {"play": 1}
    assert census["plays"]["folded_repeat_plays"] == 1
    assert census["plays"]["leading_a_run"] == 3


def test_fill_check_on_comments_that_name_their_video():
    df = _frame([
        (0, "play", "A", None),
        (1, "comment", "A", None),   # observed; the fill would borrow A: same video
        (2, "play", "B", None),
        (2.5, "comment", "C", None),  # observed C, but the fill would borrow B
        (10, "comment", "D", None),   # more than 180 s after anything: no fill
    ])
    got = rp.link_census(df)["comments"]["fill_check"]
    assert got["180"] == {"same_video": 1, "different_video": 1, "no_fill": 1}
    assert got["60"] == {"same_video": 1, "different_video": 1, "no_fill": 1}
    assert got["300"]["no_fill"] == 1


def test_link_census_counts_engagement_from_before_the_watch_history():
    df = _frame([
        (0, "save", "A", None),       # a year before any play: left unlinked
        (525600, "play", "B", None),
        (525601, "play", "A", None),
    ])
    census = rp.link_census(df)
    assert census["by_type"]["save"] == {"rows": 1, "before_watch_history": 1}
    assert census["tokens_expected"] == rp.tokens_written(derive_play_duration(df.copy())) == 0


def test_prior_overlap_matches_a_set_reference():
    prior = {"a": set(range(100)), "b": set(range(95, 100)), "c": {500}, "d": set()}
    new = set(range(90, 110))
    got = rp.prior_overlap(new, prior)
    assert got["files_touching"] == 2
    assert got["max_partner"] == "b" and got["max_overlap"] == 1.0
    assert sorted(got["would_cluster"]) == ["a", "b"]
    assert rp.prior_overlap({1, 2}, {"x": {1, 2}})["would_cluster"] == []  # under three shared seconds


def test_fidelity_and_order_comparison():
    fid = rp.fidelity({"a": 10, "b": 5, "c": 1}, {"a": "k", "b": "k", "c": "c"},
                      {"a": 10, "b": 4, "d": 2}, {"a": "k", "b": "k", "d": "d"})
    assert fid["files_in_both"] == 2 and fid["files_exact"] == 1 and fid["abs_row_difference"] == 1
    assert fid["merged_pairs_both"] == 1
    runs = rp.compare_runs([{"raw_file": "a", "final_rows": 3, "collection_id": "x"}],
                           [{"raw_file": "a", "final_rows": "3", "collection_id": "y"}])
    assert runs["files_with_different_rows"] == 0 and runs["identical_collections"] == 1


def test_copy_of_earlier_follows_donation_rank():
    rows = [{"raw_file": "b", "rank": 2, "sha256": "x"}, {"raw_file": "a", "rank": 1, "sha256": "x"},
            {"raw_file": "c", "rank": 3, "sha256": "y"}, {"raw_file": "d", "rank": 4, "sha256": "x"}]
    assert rp.copy_of_earlier(rows) == {"b": 1, "d": 1}


def test_session_census_donor_rows_match_production():
    from fyp.ingest.base import assign_session_ids

    base = pd.Timestamp("2025-03-01 10:00", tz="UTC")
    rows = [("c1", 0, "play"), ("c1", 60, "play"), ("c1", 900, "followed_by"), ("c1", 1700, "play"),
            ("c1", 5000, "login"), ("c2", 0, "play"), ("c2", 100, "fave"), ("c2", 2000, "play")]
    df = pd.DataFrame(rows, columns=["collection_id", "sec", "activity_type"])
    df["utc_timestamp"] = base + pd.to_timedelta(df.pop("sec"), unit="s")
    got = rp.session_census(df, gaps=(900,), production_gap=900)
    prod = assign_session_ids(df.copy(), gap_threshold_s=900)
    assert got["definitions"]["without_followed_by"]["900"]["sessions"] == prod["session_id"].nunique()
    assert got["definitions"]["all_rows"]["900"]["sessions_without_viewing"] == 1   # the lone login
    assert got["definitions"]["viewing_only"]["900"]["sessions"] == 4
    assert got["viewing_sittings_joined_by_all_rows"] == 1                          # followed_by bridges c1
    assert got["viewing_sittings_joined_by_without_followed_by"] == 0
    assert sum(got["viewing_gap_bins"].values()) == got["viewing_gaps"] == 3


@pytest.mark.parametrize("postcode,country,expected", [
    ("4000", "Australia", ("Australia/Brisbane", "postcode")),
    ("0870", None, ("Australia/Darwin", "postcode")),
    ("2880", "AU", ("Australia/Broken_Hill", "postcode")),
    ("3052", "", ("Australia/Melbourne", "postcode")),
    ("10001", "United States", (None, "outside_australia")),
    ("", "Australia", (None, "no_postcode")),
])
def test_zone_from_postcode(postcode, country, expected):
    assert rp.zone_from_postcode(postcode, country) == expected


def test_sentinel_summary_counts_flags():
    steps = [{"rank": 1, "route": "aio", "sentinel_status": "learning", "sentinel_findings": ""},
             {"rank": 2, "route": "aio", "sentinel_status": "warn",
              "sentinel_findings": "structure:new_key_paths:warn;stats:rows_per_mb:warn"},
             {"rank": 3, "route": "aio", "sentinel_status": "ok", "sentinel_findings": ""}]
    got = rp.sentinel_summary(steps)
    assert got["by_status"] == {"learning": 1, "warn": 1, "ok": 1}
    assert got["past_learning"] == 2
    assert got["findings"]["stats:rows_per_mb:warn"] == 1
    assert [f["rank"] for f in got["flagged"]] == [2]


def test_rows_by_type_maps_legacy_names():
    got = rp.rows_by_type_comparison({"following": 3, "fave": 10, "play": 5}, {"follow": 3, "fave": 6, "save": 4, "play": 6})
    assert got["follow"]["difference"] == 0
    assert got["fave+save"] == {"production": 10, "replay": 10, "difference": 0}
    assert got["play"]["difference"] == 1


def test_end_to_end_on_a_synthetic_snapshot(tmp_path):
    snap = tmp_path / "snap"
    (snap / "recoded").mkdir(parents=True)
    (snap / "recoded" / "ingestion_ledger.json").write_text(json.dumps({"schema_version": 1, "files": {}}))
    (snap / "recoded" / "collections_tags.json").write_text("{}")
    pd.DataFrame({
        "raw_file": pd.Series([], dtype="string"), "source_platform": pd.Series([], dtype="string"),
        "data_source": pd.Series([], dtype="string"), "collection_id": pd.Series([], dtype="string"),
        "activity_type": pd.Series([], dtype="string"),
        "ts_added_to_dataset": pd.Series([], dtype="datetime64[ns]"),
    }).to_parquet(snap / "recoded" / "collections_recoded.parquet")
    raw = snap / "activity_data" / "aio" / "aio_raw"
    raw.mkdir(parents=True)
    (snap / "activity_data" / "aio" / "aio_participants").mkdir()
    (raw / "donor-first").write_text(json.dumps(_export(30)))
    (raw / "donor-again").write_text(json.dumps(_export(40)))   # re-donation: its first 30 plays repeat
    (raw / "too-small").write_text(json.dumps(_export(5, start=5000, extra=False)))
    (snap / "activity_data" / "aio" / "aio_participants" / "ddp_metadata_latest.json").write_text(json.dumps({
        "Items": [{"id": {"S": "donor-first"}, "date": {"S": "2025-03-02T00:00:00Z"}},
                  {"id": {"S": "donor-again"}, "date": {"S": "2025-03-05T00:00:00Z"}},
                  {"id": {"S": "too-small"}, "date": {"S": "2025-03-06T00:00:00Z"}}]}))
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k not in ("FYP_CONFIG_PATH", "GEMINI_API_KEY")}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "replay_ingestion.py"),
                           "--snapshot", str(snap), "--out", str(out)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    steps = {r["raw_file"]: r for r in csv.DictReader((out / "replay_steps.csv").open())}
    assert steps["donor-first"]["rank"] == "1" and steps["donor-first"]["order_source"] == "aio_date"
    assert steps["donor-again"]["merged_with_earlier"] == "True"
    assert int(steps["donor-again"]["rows_replaced_in_older_files"]) > 0
    assert steps["too-small"]["outcome"] == "discarded_at_load"
    assert int(steps["too-small"]["records"]) == 5
    assert all(r["census_matches_parser"] in ("True", "") for r in steps.values())
    assert all(r["tokens_match"] in ("True", "") for r in steps.values())
    report = json.loads((out / "replay_report.json").read_text())
    assert report["overlap"]["files_merged_with_earlier"] == 1
    assert report["overlap"]["redonations_with_new_content"] == 1
    assert report["overlap"]["byte_copies_of_an_earlier_file"] == 0
    assert report["sentinel"]["files_evaluated"] >= 2
    assert "sessions" in report
    assert (snap / "recoded" / "ingestion_ledger.json").read_text() == json.dumps({"schema_version": 1, "files": {}})
    assert sorted(p.name for p in raw.iterdir()) == ["donor-again", "donor-first", "too-small"]


def test_refuses_gcs(tmp_path):
    snap = tmp_path / "snap"
    (snap / "recoded").mkdir(parents=True)
    env = {**{k: v for k, v in os.environ.items() if k != "FYP_CONFIG_PATH"}, "FYP_FORCE_GCS": "1"}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "replay_ingestion.py"),
                           "--snapshot", str(snap), "--out", str(tmp_path / "out")],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert "REFUSING" in proc.stdout + proc.stderr


@pytest.mark.parametrize("name,expected", [
    ("tiktok_ddp_20260906T112918Z_3f9a1c7b.json", datetime(2026, 9, 6, 11, 29, 18, tzinfo=UTC)),
    ("user_data_tiktok.json", None),
])
def test_stamp_from_name(name, expected):
    assert rp.stamp_from_name(name) == expected
