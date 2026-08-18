#!/usr/bin/env python3
"""Ad-hoc regression test for the Instagram and YouTube collection classes.

Runs each collection end-to-end against offline fixtures (built from the sample
uploads under ``tmp/fixtures/``) through load_raw -> save_enrichment_seed ->
process, and asserts the activity rows and the donated enrichment seed are
correct. Synthetic fixtures additionally cover the classic Instagram export
schema, US-locale and GMT-offset YouTube timestamps, ad misclassification
guards, unsupported-locale handling (pending, not discarded), and seed merging
across ingest runs. Isolated: raw and processed data go to throwaway temp
storage locations registered at runtime, so nothing under the real data dirs
is touched.

Run:
    source .venv/bin/activate
    python tests/test_instagram_youtube_ingest.py
"""

import json
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import fyp.data_io as data_io
from fyp.ingest import InstagramDDPCollection, YouTubeDDPCollection, registered_raw_locations

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, "..", "tmp", "fixtures")
_REQUIRED_CORE = ["activity_type", "utc_timestamp", "collection_id", "data_source", "tz_offset"]

# Real donation exports are never committed. Point these at your own local
# copies to run the end-to-end checks; the tests skip when they are unset.
_SRC_IG = os.environ.get("FYP_TEST_IG_ZIP", "")
_SRC_YT = os.environ.get("FYP_TEST_YT_ZIP", "")
_SRC_YT_JSON = os.environ.get("FYP_TEST_YT_JSON_ZIP", "")

_YT_CELL = (
    '<div class="outer-cell mdl-cell"><div class="mdl-grid">'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
    'Watched <a href="https://www.youtube.com/watch?v={vid}">{title}</a><br>'
    '{channel}{ts}<br></div>'
    '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
    '<b>Products:</b><br>&emsp;YouTube{details}</div></div></div>'
)


def _yt_cell(vid: str, title: str, ts: str, channel_id: str = "UCx", channel: str = "Chan", ad: bool = False) -> str:
    """Render one synthetic watch-history cell."""
    channel_html = f'<a href="https://www.youtube.com/channel/{channel_id}">{channel}</a><br>' if channel_id else ""
    details = "<br><b>Details:</b><br>&emsp;From Google Ads" if ad else ""
    return _YT_CELL.format(vid=vid, title=title, channel=channel_html, ts=ts, details=details)


def _yt_json_record(vid: str = None, title: str = None, ts: str = "2026-07-01T22:18:23.481Z",
                    channel_id: str = "UCx", channel: str = "Chan", ad: bool = False) -> dict:
    """Render one synthetic JSON watch-history record.

    ``vid=None`` yields a non-video event (no titleUrl); ``title=None`` with a
    vid mimics a removed/private video (the watch URL echoed as the title, no
    subtitles).
    """
    record = {
        "header": "YouTube",
        "time": ts,
        "products": ["YouTube"],
        "activityControls": ["YouTube watch history"],
    }
    if vid is None:
        record["title"] = title or "Used Shorts creation tools"
        return record
    url = f"https://www.youtube.com/watch?v={vid}"
    record["titleUrl"] = url
    if title is None:
        record["title"] = f"Watched {url}"
    else:
        record["title"] = f"Watched {title}"
        if channel_id:
            record["subtitles"] = [
                {"name": channel, "url": f"https://www.youtube.com/channel/{channel_id}"}
            ]
    if ad:
        record["details"] = [{"name": "From Google Ads"}]
    return record


def _make_zip(path: str, members: dict[str, str]) -> None:
    """Write a zip with the given {member_name: text} contents."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, text in members.items():
            zout.writestr(name, text)


def _ensure_fixtures() -> None:
    """Build the offline fixtures from the sample uploads if they are missing."""
    os.makedirs(_FIXTURES, exist_ok=True)
    ig = os.path.join(_FIXTURES, "ig_sample.zip")
    yt = os.path.join(_FIXTURES, "yt_sample.zip")
    if not os.path.exists(ig) and os.path.exists(_SRC_IG):
        members = [
            "your_instagram_activity/story_interactions/stories_viewed.json",
            "your_instagram_activity/likes/liked_posts.json",
        ]
        with zipfile.ZipFile(_SRC_IG) as zin, zipfile.ZipFile(ig, "w", zipfile.ZIP_DEFLATED) as zout:
            for member in members:
                zout.writestr(member, zin.read(member))
    if not os.path.exists(yt) and os.path.exists(_SRC_YT):
        member = "Takeout/YouTube and YouTube Music/history/watch-history.html"
        with zipfile.ZipFile(_SRC_YT) as zin, zipfile.ZipFile(yt, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.writestr(member, zin.read(member))
    yt_json = os.path.join(_FIXTURES, "yt_sample_json.zip")
    if not os.path.exists(yt_json) and os.path.exists(_SRC_YT_JSON):
        member = "Takeout/YouTube and YouTube Music/history/watch-history.json"
        with zipfile.ZipFile(_SRC_YT_JSON) as zin, zipfile.ZipFile(yt_json, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.writestr(member, zin.read(member))


def _fresh_locations(tag: str) -> tuple[str, str, str, str]:
    """Create and register a throwaway raw + output storage location pair."""
    raw_dir = tempfile.mkdtemp(prefix=f"fyp_test_raw_{tag}_", dir=os.path.expanduser("~/fyp_local_test") if False else None)
    out_dir = tempfile.mkdtemp(prefix=f"fyp_test_out_{tag}_")
    raw_loc = f"test_raw_{tag}"
    out_loc = f"test_out_{tag}"
    try:
        data_io.register_location(raw_loc, raw_dir)
        data_io.register_location(out_loc, out_dir)
    except ValueError:
        # Temp dirs live outside local_data; in local mode registration still
        # needs the paths entry, so insert directly for the test.
        from fyp.fyp_config import fyp_cf
        fyp_cf["paths"].setdefault(raw_loc, raw_dir)
        fyp_cf["paths"].setdefault(out_loc, out_dir)
    return raw_loc, raw_dir, out_loc, out_dir


def _run_collection(cls, fixture_path: str, collection_id: str, raw_loc: str = None,
                    raw_dir: str = None, out_loc: str = None, out_dir: str = None,
                    tz: str = None):
    """Ingest one fixture zip; return (activity_df, seed_df, collection)."""
    if raw_loc is None:
        raw_loc, raw_dir, out_loc, out_dir = _fresh_locations(collection_id)

    zip_name = f"{collection_id}.zip"
    shutil.copy(fixture_path, os.path.join(raw_dir, zip_name))
    manifest_fn = "ingestion_manifest.json"
    manifest = {}
    if data_io.exists(storage_location=raw_loc, filename=manifest_fn):
        manifest = data_io.load_json(storage_location=raw_loc, filename=manifest_fn) or {}
    manifest[zip_name] = {"collection_id": collection_id, "tags": []}
    if tz:
        manifest[zip_name]["tz"] = tz
    data_io.save_json(data=manifest, storage_location=raw_loc, filename=manifest_fn)

    col = cls(verbose=False)
    col.raw_path = raw_loc
    col.processed_storage_location = out_loc

    col.load_raw()
    col.save_enrichment_seed()
    col.process()

    seed_fn = f"{col.source_platform}_{col.data_source}_enrichment_seed.parquet"
    seed_df = None
    if data_io.exists(storage_location=out_loc, filename=seed_fn):
        seed_df = data_io.load_parquet(storage_location=out_loc, filename=seed_fn)
    return col.data, seed_df, col


def _assert_core(df, platform: str) -> None:
    """Assert the platform-agnostic activity contract holds for a frame."""
    assert len(df) > 0, f"{platform}: no activity rows produced"
    for col in _REQUIRED_CORE:
        assert col in df.columns, f"{platform}: missing required-core column {col}"
        assert df[col].isna().sum() == 0, f"{platform}: null values in required-core {col}"
    assert (df["source_platform"] == platform).all(), f"{platform}: source_platform not stamped"
    years = pd.to_datetime(df["utc_timestamp"]).dt.year
    assert years.between(2015, 2035).all(), f"{platform}: utc_timestamp out of sane range"


def test_instagram():
    """Instagram: viewed reels -> play, liked posts -> fave; seed mojibake-repaired."""
    fixture = os.path.join(_FIXTURES, "ig_sample.zip")
    df, seed, _ = _run_collection(InstagramDDPCollection, fixture, "P001_ig")
    _assert_core(df, "instagram")

    types = df["activity_type"].value_counts().to_dict()
    print(f"  instagram activity_type counts: {types}")
    assert set(types).issubset({"play", "fave"}), f"unexpected activity types: {types}"
    assert types.get("play", 0) == 86, f"expected 86 viewed reels, got {types.get('play', 0)}"
    assert types.get("fave", 0) >= 1, "expected at least one liked post"

    assert df["item_id"].notna().all(), "instagram: sample rows all carry URLs"
    assert df["item_id"].str.match(r"^[\w-]+$").all(), "instagram: malformed shortcode"

    assert seed is not None and len(seed) > 0, "instagram: no enrichment seed written"
    assert (seed["scrape_status"] == "donated").all(), "instagram: seed scrape_status not 'donated'"
    assert seed["scrape_contract_version"].notna().all(), "instagram: seed missing version stamp"
    assert seed["item_id"].is_unique, "instagram: seed not deduped by item_id"

    row = seed[seed["item_id"] == "DY1zHU_xQM2"]
    assert len(row) == 1, "instagram: expected seed row for DY1zHU_xQM2"
    desc = str(row.iloc[0]["desc"])
    assert "Actually impressive" in desc, f"instagram: caption not captured: {desc!r}"
    assert "â" not in desc and "—" in desc, f"instagram: mojibake not repaired: {desc!r}"
    print("  [PASS] instagram")


def test_instagram_classic_schema():
    """Classic string_list_data/string_map_data exports yield rows too."""
    likes = json.dumps({"likes_media_likes": [
        {"title": f"user{i}", "string_list_data": [
            {"href": f"https://www.instagram.com/p/Classic{i:03}/", "value": "x", "timestamp": 1700000000 + i}
        ]} for i in range(12)
    ]})
    stories = json.dumps([
        {"title": f"author{i}", "string_map_data": {"Time": {"timestamp": 1700100000 + i}}}
        for i in range(15)
    ])
    fixture = os.path.join(_FIXTURES, "ig_classic.zip")
    _make_zip(fixture, {
        "your_instagram_activity/likes/liked_posts.json": likes,
        "your_instagram_activity/story_interactions/stories_viewed.json": stories,
    })

    df, seed, _ = _run_collection(InstagramDDPCollection, fixture, "P003_igc")
    _assert_core(df, "instagram")
    types = df["activity_type"].value_counts().to_dict()
    assert types.get("fave", 0) == 12, f"classic likes not extracted: {types}"
    assert types.get("play", 0) == 15, f"classic story views not extracted: {types}"
    faves = df[df["activity_type"] == "fave"]
    assert faves["item_id"].notna().all(), "classic likes: item_id missing"
    assert df[df["activity_type"] == "play"]["item_id"].isna().all(), \
        "classic story views carry no URL — item_id must be NA"
    assert seed is not None and (seed["author_id"].str.startswith("user")).all(), \
        "classic seed: record-level title not captured as author_id"
    print("  [PASS] instagram classic schema")


def test_youtube():
    """YouTube: organic -> play, ads -> ad_play, non-video dropped; seed captured."""
    fixture = os.path.join(_FIXTURES, "yt_sample.zip")
    df, seed, _ = _run_collection(YouTubeDDPCollection, fixture, "P001_yt")
    _assert_core(df, "youtube")

    types = df["activity_type"].value_counts().to_dict()
    print(f"  youtube activity_type counts: {types}; total={len(df)}")
    assert set(types).issubset({"play", "ad_play"}), f"unexpected activity types: {types}"
    assert types.get("play", 0) >= 900, f"too few organic watches: {types.get('play', 0)}"
    assert types.get("ad_play", 0) >= 300, f"too few ads flagged: {types.get('ad_play', 0)}"
    assert len(df) == 1290, f"expected all 1290 video watches, got {len(df)}"

    assert df["item_id"].notna().all(), "youtube: null item_id"
    assert df["item_id"].str.match(r"^[\w-]{11}$").all(), "youtube: malformed video id"

    assert seed is not None and len(seed) > 0, "youtube: no enrichment seed written"
    assert (seed["scrape_status"] == "donated").all(), "youtube: seed scrape_status not 'donated'"
    assert seed["scrape_contract_version"].notna().all(), "youtube: seed missing version stamp"
    assert seed["item_id"].is_unique, "youtube: seed not deduped by item_id"

    row = seed[seed["item_id"] == "pKOOk7f6FHk"]
    assert len(row) == 1 and pd.notna(row.iloc[0]["author_name"]), "youtube: channel name not captured"
    print("  [PASS] youtube")


def test_youtube_locales_and_ads():
    """US month-first 12h timestamps, GMT offsets, and caption-only ad detection."""
    cells = "".join([
        # 12 organic day-first cells (baseline)
        *[_yt_cell(f"AAAAAAAAA{i:02}", f"vid {i}", f"{i + 1} Jun 2026, 10:00:0{i % 10} AEST") for i in range(12)],
        # US-locale organic: 5:36:02 PM PDT == 00:36:02 UTC next day
        _yt_cell("USLOCALE001", "us cell", "Apr 4, 2024, 5:36:02 PM PDT"),
        # GMT+05:30 (India rendered as offset): 20:15:33 - 5.5h == 14:45:33 UTC
        _yt_cell("GMTOFFSET01", "gmt cell", "15 Aug 2025, 20:15:33 GMT+05:30"),
        # organic video whose TITLE contains the ad marker phrase — must stay 'play'
        _yt_cell("TITLETRAP01", "How to opt out From Google Ads", "3 Jun 2026, 09:00:00 AEST"),
        # a real ad (marker in the details/caption cell)
        _yt_cell("REALADVERT1", "ad vid", "3 Jun 2026, 09:05:00 AEST", ad=True),
    ])
    fixture = os.path.join(_FIXTURES, "yt_locales.zip")
    _make_zip(fixture, {"Takeout/YouTube and YouTube Music/history/watch-history.html":
                        f"<html><body>{cells}</body></html>"})

    df, _, _ = _run_collection(YouTubeDDPCollection, fixture, "P004_ytl")
    _assert_core(df, "youtube")
    by_id = df.set_index("item_id")

    us = pd.Timestamp(by_id.loc["USLOCALE001", "utc_timestamp"])
    assert (us.month, us.day, us.hour, us.minute) == (4, 5, 0, 36), f"US-locale timestamp wrong: {us}"
    gmt = pd.Timestamp(by_id.loc["GMTOFFSET01", "utc_timestamp"])
    assert (gmt.hour, gmt.minute) == (14, 45), f"GMT+05:30 timestamp wrong: {gmt}"
    assert by_id.loc["TITLETRAP01", "activity_type"] == "play", "ad phrase in title misclassified"
    assert by_id.loc["REALADVERT1", "activity_type"] == "ad_play", "real ad not flagged"
    print("  [PASS] youtube locales + ad detection")


def test_youtube_json():
    """JSON watch history: plays/ads/removed/non-video handled; UTC exact."""
    records = [
        # 12 organic watches with channel info, distinct minutes
        *[_yt_json_record(f"JSONVIDEO{i:02}", f"vid {i}", f"2026-06-01T10:{i:02}:05.123Z",
                          channel_id=f"UCjson{i}", channel=f"Chan {i}") for i in range(12)],
        # a served ad
        _yt_json_record("JSONADVERT1", "ad vid", "2026-06-02T11:00:00.000Z", ad=True),
        # a removed/private video: URL echoed as title, no subtitles
        _yt_json_record("JSONREMOVED", None, "2026-06-03T12:00:00.000Z"),
        # non-video events: no titleUrl -> dropped
        _yt_json_record(None, "Used Shorts creation tools", "2026-06-04T13:00:00.000Z"),
        _yt_json_record(None, "Viewed Ads On YouTube Shorts", "2026-06-05T14:00:00.000Z"),
    ]
    fixture = os.path.join(_FIXTURES, "yt_json_synth.zip")
    _make_zip(fixture, {"Takeout/YouTube and YouTube Music/history/watch-history.json":
                        json.dumps(records)})

    df, seed, _ = _run_collection(YouTubeDDPCollection, fixture, "P009_ytj", tz="Australia/Brisbane")
    _assert_core(df, "youtube")

    types = df["activity_type"].value_counts().to_dict()
    assert len(df) == 14, f"expected 14 video rows (2 non-video dropped), got {len(df)}"
    assert types.get("play", 0) == 13, f"expected 13 plays (12 organic + removed), got {types}"
    assert types.get("ad_play", 0) == 1, f"expected 1 ad_play, got {types}"

    by_id = df.set_index("item_id")
    ts = pd.Timestamp(by_id.loc["JSONVIDEO03", "utc_timestamp"])
    assert (ts.hour, ts.minute, ts.second) == (10, 3, 5), f"ISO-8601 UTC not preserved: {ts}"
    # Manifest tz only sets tz_offset (timestamps are already UTC): Brisbane = +10.
    assert (df["tz_offset"] == 10).all(), f"tz_offset not from manifest zone: {sorted(df['tz_offset'].unique())}"

    assert seed is not None and seed["item_id"].is_unique, "youtube json: seed missing or not deduped"
    seed_by_id = seed.set_index("item_id")
    assert seed_by_id.loc["JSONVIDEO03", "desc"] == "vid 3", "youtube json: donated title not in seed"
    assert seed_by_id.loc["JSONVIDEO03", "author_id"] == "UCjson3", "youtube json: channel id not in seed"
    assert seed_by_id.loc["JSONVIDEO03", "author_name"] == "Chan 3", "youtube json: channel name not in seed"
    assert pd.isna(seed_by_id.loc["JSONREMOVED", "desc"]), "removed video must carry no donated title"
    print("  [PASS] youtube json (synthetic)")


def test_youtube_json_real():
    """The real JSON Takeout export parses with the expected play/ad split."""
    fixture = os.path.join(_FIXTURES, "yt_sample_json.zip")
    if not os.path.exists(fixture):
        print("  [SKIP] youtube json real export (fixture yt_sample_json.zip unavailable)")
        return
    df, seed, _ = _run_collection(YouTubeDDPCollection, fixture, "P010_ytjr")
    _assert_core(df, "youtube")

    types = df["activity_type"].value_counts().to_dict()
    print(f"  youtube json activity_type counts: {types}; total={len(df)}")
    assert len(df) == 1284, f"expected 1284 video watches (9 non-video dropped), got {len(df)}"
    assert types.get("play", 0) == 922, f"expected 922 plays, got {types.get('play', 0)}"
    assert types.get("ad_play", 0) == 362, f"expected 362 ad plays, got {types.get('ad_play', 0)}"
    assert df["item_id"].str.match(r"^[\w-]{11}$").all(), "youtube json: malformed video id"
    assert seed is not None and seed["item_id"].is_unique, "youtube json: bad seed"
    print("  [PASS] youtube json (real export)")


def test_youtube_json_preferred_over_html():
    """When a zip carries both members, the JSON one wins."""
    json_records = [
        _yt_json_record(f"JSONWINNER{i:01}", f"json vid {i}", f"2026-06-10T10:00:{i:02}.000Z")
        for i in range(11)
    ]
    html_cells = "".join(
        _yt_cell(f"HTMLLOSERR{i:01}", f"html vid {i}", f"{i + 1} Jun 2026, 10:00:00 AEST")
        for i in range(11)
    )
    fixture = os.path.join(_FIXTURES, "yt_both_members.zip")
    _make_zip(fixture, {
        "Takeout/YouTube and YouTube Music/history/watch-history.json": json.dumps(json_records),
        "Takeout/YouTube and YouTube Music/history/watch-history.html":
            f"<html><body>{html_cells}</body></html>",
    })

    df, _, _ = _run_collection(YouTubeDDPCollection, fixture, "P011_ytb")
    _assert_core(df, "youtube")
    assert df["item_id"].str.startswith("JSONWINNER").all(), \
        f"HTML rows leaked in despite JSON member: {sorted(df['item_id'].unique())[:5]}"
    assert len(df) == 11, f"expected the 11 JSON rows, got {len(df)}"
    print("  [PASS] youtube json preferred over html")


def test_youtube_invalid_json_stays_pending():
    """A corrupt watch-history.json raises and the file is left pending."""
    fixture = os.path.join(_FIXTURES, "yt_badjson.zip")
    _make_zip(fixture, {"Takeout/YouTube and YouTube Music/history/watch-history.json":
                        '{"not": "a list", truncated'})

    raw_loc, raw_dir, out_loc, out_dir = _fresh_locations("P012_ytbad")
    shutil.copy(fixture, os.path.join(raw_dir, "P012.zip"))
    data_io.save_json(data={"P012.zip": {"collection_id": "P012", "tags": []}},
                      storage_location=raw_loc, filename="ingestion_manifest.json")

    col = YouTubeDDPCollection(verbose=False)
    col.raw_path = raw_loc
    col.processed_storage_location = out_loc
    col.load_raw()

    assert "P012.zip" not in col.discarded_raw_files, \
        "invalid-JSON donation was discarded instead of left pending"
    assert col.state == "empty", "no rows should have loaded from invalid JSON"
    print("  [PASS] invalid json stays pending")


def test_unsupported_locale_stays_pending():
    """A donation whose timestamps can't be parsed is left pending, not discarded."""
    cells = "".join(
        _yt_cell(f"BBBBBBBBB{i:02}", f"vid {i}", f"{i + 1}. Juli 2026, 10:00:00 MESZ") for i in range(12)
    )
    fixture = os.path.join(_FIXTURES, "yt_german.zip")
    _make_zip(fixture, {"Takeout/YouTube and YouTube Music/history/watch-history.html":
                        f"<html><body>{cells}</body></html>"})

    raw_loc, raw_dir, out_loc, out_dir = _fresh_locations("P005_ytg")
    shutil.copy(fixture, os.path.join(raw_dir, "P005.zip"))
    data_io.save_json(data={"P005.zip": {"collection_id": "P005", "tags": []}},
                      storage_location=raw_loc, filename="ingestion_manifest.json")

    col = YouTubeDDPCollection(verbose=False)
    col.raw_path = raw_loc
    col.processed_storage_location = out_loc
    col.load_raw()

    assert "P005.zip" not in col.discarded_raw_files, \
        "unsupported-locale donation was discarded (would be ledger-blacklisted) instead of left pending"
    assert col.state == "empty", "no parseable rows should have loaded"
    print("  [PASS] unsupported locale stays pending")


def test_structural_failure_stays_pending():
    """A zip without the expected members raises and is not discarded."""
    fixture = os.path.join(_FIXTURES, "ig_empty.zip")
    _make_zip(fixture, {"something/else.txt": "hello"})

    raw_loc, raw_dir, out_loc, out_dir = _fresh_locations("P006_igx")
    shutil.copy(fixture, os.path.join(raw_dir, "P006.zip"))
    data_io.save_json(data={"P006.zip": {"collection_id": "P006", "tags": []}},
                      storage_location=raw_loc, filename="ingestion_manifest.json")

    col = InstagramDDPCollection(verbose=False)
    col.raw_path = raw_loc
    col.processed_storage_location = out_loc
    col.load_raw()

    assert "P006.zip" not in col.discarded_raw_files, \
        "structurally-broken zip was discarded instead of left pending"
    print("  [PASS] structural failure stays pending")


def test_seed_merges_across_runs():
    """A second ingest run must extend the seed parquet, not overwrite it."""
    fixture1 = os.path.join(_FIXTURES, "ig_sample.zip")
    likes2 = json.dumps({"likes_media_likes": [
        {"title": "runtwo", "string_list_data": [
            {"href": f"https://www.instagram.com/p/RunTwo{i:04}/", "timestamp": 1710000000 + i}
        ]} for i in range(11)
    ]})
    fixture2 = os.path.join(_FIXTURES, "ig_run2.zip")
    _make_zip(fixture2, {"your_instagram_activity/likes/liked_posts.json": likes2})

    raw_loc, raw_dir, out_loc, out_dir = _fresh_locations("P007_igm")
    _, seed1, _ = _run_collection(InstagramDDPCollection, fixture1, "P007a",
                                  raw_loc=raw_loc, raw_dir=raw_dir, out_loc=out_loc, out_dir=out_dir)
    n1 = len(seed1)

    # Second run: fresh collection instance, same locations; run 1's zip is
    # skipped as already ingested (mirrors the refresh worker's skip list).
    zip_name2 = "P007b.zip"
    shutil.copy(fixture2, os.path.join(raw_dir, zip_name2))
    manifest = data_io.load_json(storage_location=raw_loc, filename="ingestion_manifest.json") or {}
    manifest[zip_name2] = {"collection_id": "P007b", "tags": []}
    data_io.save_json(data=manifest, storage_location=raw_loc, filename="ingestion_manifest.json")

    col2 = InstagramDDPCollection(verbose=False)
    col2.raw_path = raw_loc
    col2.processed_storage_location = out_loc
    col2.load_raw(skip_these_raw_files=["P007a.zip"])
    col2.save_enrichment_seed()

    seed_fn = "instagram_ddp_enrichment_seed.parquet"
    seed2 = data_io.load_parquet(storage_location=out_loc, filename=seed_fn)
    assert len(seed2) == n1 + 11, \
        f"seed not merged: run1 had {n1} rows, run2 wrote {len(seed2)} (expected {n1 + 11})"
    assert (seed2["item_id"] == "DY1zHU_xQM2").any(), "run 1 seed rows lost on second ingest"
    assert seed2["item_id"].str.startswith("RunTwo").sum() == 11, "run 2 seed rows missing"
    print(f"  [PASS] seed merges across runs ({n1} + 11 = {len(seed2)})")


def test_manifest_timezone_override():
    """An explicit manifest timezone overrides the label, exactly and DST-aware."""
    from fyp.ingest import parse_donor_timezone

    # parse helper accepts IANA names and fixed offsets, rejects junk.
    assert parse_donor_timezone("Asia/Kolkata") is not None
    assert parse_donor_timezone("+05:30") is not None
    assert parse_donor_timezone("-8") is not None
    assert parse_donor_timezone("Not/AZone") is None
    assert parse_donor_timezone("") is None

    # Build a YouTube file whose LABEL says IST but whose donor is really in
    # India (+5:30). Without the override, "IST" maps to +5.5 by our default —
    # so to prove the override *wins*, label it with an ambiguous zone and
    # override to a DIFFERENT, unambiguous zone, then check the offset.
    cells = "".join(
        _yt_cell(f"TZOVERRIDE{i:01}", f"vid {i}", f"{i + 1} Jun 2026, 12:00:00 IST") for i in range(12)
    )
    fixture = os.path.join(_FIXTURES, "yt_tzoverride.zip")
    _make_zip(fixture, {"Takeout/YouTube and YouTube Music/history/watch-history.html":
                        f"<html><body>{cells}</body></html>"})

    # Override to New York (US Eastern, DST-aware). June => EDT = -4.
    df, _, _ = _run_collection(YouTubeDDPCollection, fixture, "P008_tz", tz="America/New_York")
    _assert_core(df, "youtube")
    # Local 12:00 in New York on a June date (EDT, -4) == 16:00 UTC.
    utc = pd.to_datetime(df["utc_timestamp"])
    assert (utc.dt.hour == 16).all(), f"override not applied: got hours {sorted(utc.dt.hour.unique())}"
    # tz_offset comes from the zone, not inference: EDT = -4.
    assert (df["tz_offset"] == -4).all(), f"tz_offset not from zone: {sorted(df['tz_offset'].unique())}"
    print("  [PASS] manifest timezone override (YouTube, DST-aware)")

    # Instagram: epoch is already UTC, so the override only sets tz_offset.
    # Use a clean integer-offset zone (Perth, +8, no DST); tz_offset is stored
    # as integer hours in the activity contract, so half-hour zones like +05:30
    # would truncate (a pre-existing schema limitation — utc_timestamp stays
    # exact, only derived local time loses the 30 min).
    ig_df, _, _ = _run_collection(
        InstagramDDPCollection, os.path.join(_FIXTURES, "ig_sample.zip"), "P008_igtz", tz="Australia/Perth")
    _assert_core(ig_df, "instagram")
    assert (ig_df["tz_offset"] == 8).all(), \
        f"instagram tz_offset not from manifest zone: {sorted(ig_df['tz_offset'].unique())}"
    print("  [PASS] manifest timezone override (Instagram offset)")


def test_generic_structure():
    """Both platforms emit the identical activity + seed schema (generalization)."""
    ig_df, ig_seed, _ = _run_collection(
        InstagramDDPCollection, os.path.join(_FIXTURES, "ig_sample.zip"), "P002_ig")
    yt_df, yt_seed, _ = _run_collection(
        YouTubeDDPCollection, os.path.join(_FIXTURES, "yt_sample.zip"), "P002_yt")

    ig_cols = set(ig_df.columns)
    yt_cols = set(yt_df.columns)
    assert ig_cols == yt_cols, (
        f"activity schemas diverge: only-ig={ig_cols - yt_cols}, only-yt={yt_cols - ig_cols}"
    )
    # play_duration is a base column now — the forward-delta derivation runs for
    # every DDP platform (values may be NA when fixture events are >600s apart).
    assert "play_duration" in ig_cols and "play_duration" in yt_cols
    assert set(ig_seed.columns) == set(yt_seed.columns), "seed schemas diverge"
    assert "instagram_raw" in registered_raw_locations(), "instagram_raw not in registry locations"
    assert "youtube_raw" in registered_raw_locations(), "youtube_raw not in registry locations"
    print(f"  shared activity columns: {sorted(ig_cols)}")
    print("  [PASS] generic structure identical across platforms")


if __name__ == "__main__":
    _ensure_fixtures()
    missing = [
        name for name in ("ig_sample.zip", "yt_sample.zip")
        if not os.path.exists(os.path.join(_FIXTURES, name))
    ]
    if missing:
        print(
            f"SKIP: fixtures {missing} not found under tmp/fixtures/ and the source "
            "exports are unavailable. Fixtures are gitignored (they hold personal "
            "donation data); rebuild them from a local Instagram/Takeout export."
        )
        sys.exit(0)

    print("Running Instagram/YouTube ingestion tests...")
    test_instagram()
    test_instagram_classic_schema()
    test_youtube()
    test_youtube_locales_and_ads()
    test_youtube_json()
    test_youtube_json_real()
    test_youtube_json_preferred_over_html()
    test_youtube_invalid_json_stays_pending()
    test_unsupported_locale_stays_pending()
    test_structural_failure_stays_pending()
    test_seed_merges_across_runs()
    test_manifest_timezone_override()
    test_generic_structure()
    print("\nAll tests passed.")
