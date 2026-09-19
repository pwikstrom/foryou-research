"""Fixture tests for YouTubeDDPCollection: hand-built Takeout zips through
``load_single_raw`` and ``process_single``.

Covers the JSON watch history (organic vs. ad impressions), the comments
CSV folded onto its play with ``link_method``, the HTML history's ambiguous
time-zone abbreviation being recorded as a per-file ledger note, and a
supplied donor zone silencing that note.
"""

import json
import zipfile

import pytest

import fyp.ingest.youtube as youtube_mod

_IDS = [f"vid{i:08d}" for i in range(12)]  # 11 chars each


def _json_export(path) -> str:
    records = []
    for i, vid in enumerate(_IDS):
        record = {
            "title": f"Watched Video {i}",
            "titleUrl": f"https://www.youtube.com/watch?v={vid}",
            "subtitles": [{"name": "Some Channel", "url": "https://www.youtube.com/channel/UCabcdef"}],
            "time": f"2026-05-01T10:{i:02d}:00.000Z",
        }
        if i == 5:
            record["details"] = [{"name": "From Google Ads"}]
        records.append(record)
    comments = (
        "Comment create timestamp,Video ID,Comment text\n"
        f"2026-05-01T10:00:30.000Z,{_IDS[0]},\"{{\"\"text\"\": \"\"great\"\"}}\"\n"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Takeout/YouTube and YouTube Music/history/watch-history.json", json.dumps(records))
        zf.writestr("Takeout/YouTube and YouTube Music/comments/comments.csv", comments)
    return str(path)


def _html_export(path, tz_label: str) -> str:
    blocks = []
    for i, vid in enumerate(_IDS):
        blocks.append(
            '<div class="outer-cell"><div class="body-1">'
            f'Watched <a href="https://www.youtube.com/watch?v={vid}">Video {i}</a><br>'
            f'<a href="https://www.youtube.com/channel/UCabcdef">Some Channel</a><br>'
            f'{i + 1} Jun 2026, 21:{i:02d}:00 {tz_label}</div>'
            '<div class="mdl-typography--caption"></div></div>'
        )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Takeout/YouTube and YouTube Music/history/watch-history.html",
                    "<html>" + "".join(blocks) + "</html>")
    return str(path)


def _patch(monkeypatch, zip_path: str):
    monkeypatch.setattr(youtube_mod.data_io, "local_copy",
                        lambda storage_location=None, filename=None: zip_path)
    monkeypatch.setattr(youtube_mod.data_io, "release_local_copy", lambda p: None)


@pytest.fixture
def collection():
    c = youtube_mod.YouTubeDDPCollection(verbose=False)
    c._current_file_tz = None
    return c


def test_json_history_and_comments_parse(collection, monkeypatch, tmp_path):
    _patch(monkeypatch, _json_export(tmp_path / "takeout.zip"))

    df = collection.load_single_raw("takeout.zip")
    df["raw_file"] = "takeout.zip"
    out = collection.process_single(df).sort_values("utc_timestamp").reset_index(drop=True)

    assert (out["activity_type"] == "play").sum() == 11
    assert (out["activity_type"] == "ad_play").sum() == 1
    assert out.loc[out["activity_type"] == "ad_play", "item_id"].iloc[0] == _IDS[5]
    comment = out[out["activity_type"] == "comment"].iloc[0]
    assert comment["item_id"] == _IDS[0]
    assert comment["extra_data"] == "great"
    first_play = out[(out["activity_type"] == "play") & (out["item_id"] == _IDS[0])].iloc[0]
    assert first_play["extra_data"] == "comment:great"
    assert first_play["link_method"] == "adjacent"


def test_ambiguous_abbreviation_is_recorded_on_the_file(collection, monkeypatch, tmp_path):
    """IST is India in Takeout's most common usage but also Ireland/Israel;
    the resolution is a per-file ledger note, not only a log line."""
    _patch(monkeypatch, _html_export(tmp_path / "takeout.zip", "IST"))

    df = collection.load_single_raw("takeout.zip")

    assert len(df) == 12
    notes = collection.parse_notes_this_run.get("takeout.zip") or []
    assert len(notes) == 1
    assert "ambiguous" in notes[0] and "IST" in notes[0]
    # 21:00 IST (UTC+5:30) is 15:30 UTC.
    assert df["utc_timestamp"].iloc[0].strftime("%H:%M") == "15:30"


def test_supplied_zone_silences_the_ambiguity_note(collection, monkeypatch, tmp_path):
    _patch(monkeypatch, _html_export(tmp_path / "takeout.zip", "IST"))
    collection._current_file_tz = "Europe/Dublin"

    df = collection.load_single_raw("takeout.zip")

    assert collection.parse_notes_this_run.get("takeout.zip") is None
    # 21:00 Irish Standard Time in June (UTC+1) is 20:00 UTC.
    assert df["utc_timestamp"].iloc[0].strftime("%H:%M") == "20:00"


def test_unrecognised_label_is_recorded_on_the_file(collection, monkeypatch, tmp_path):
    _patch(monkeypatch, _html_export(tmp_path / "takeout.zip", "XQZ"))

    collection.load_single_raw("takeout.zip")

    notes = collection.parse_notes_this_run.get("takeout.zip") or []
    assert len(notes) == 1
    assert "unrecognised" in notes[0] and "XQZ" in notes[0]


def _ad_heavy_export(path, n_organic: int, n_ads: int, n_comments: int = 0) -> str:
    """A Takeout whose row count clears the floor only on ads and comments."""
    records = []
    for i in range(n_organic + n_ads):
        vid = f"vid{i:08d}"
        record = {
            "title": f"Watched Video {i}",
            "titleUrl": f"https://www.youtube.com/watch?v={vid}",
            "subtitles": [{"name": "Some Channel", "url": "https://www.youtube.com/channel/UCabcdef"}],
            "time": f"2026-05-01T10:{i:02d}:00.000Z",
        }
        if i >= n_organic:
            record["details"] = [{"name": "From Google Ads"}]
        records.append(record)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Takeout/YouTube and YouTube Music/history/watch-history.json", json.dumps(records))
        if n_comments:
            rows = "".join(
                f"2026-05-01T10:{i:02d}:30.000Z,vid{i:08d},\"{{\"\"text\"\": \"\"hi\"\"}}\"\n"
                for i in range(n_comments)
            )
            zf.writestr("Takeout/YouTube and YouTube Music/comments/comments.csv",
                        "Comment create timestamp,Video ID,Comment text\n" + rows)
    return str(path)


def test_ads_and_comments_do_not_satisfy_the_viability_floor(collection, monkeypatch, tmp_path):
    """35 rows, but only 5 organic watches — ads are dropped from every study."""
    _patch(monkeypatch, _ad_heavy_export(tmp_path / "ads.zip", n_organic=5, n_ads=20, n_comments=10))

    assert collection.load_single_raw("ads.zip").empty


def test_enough_organic_watches_still_load_alongside_ads(collection, monkeypatch, tmp_path):
    """The floor counts organic watches only — it must not reject a healthy export."""
    _patch(monkeypatch, _ad_heavy_export(tmp_path / "ok.zip", n_organic=10, n_ads=3))

    df = collection.load_single_raw("ok.zip")
    assert int(df["is_ad"].ne(True).sum()) == 10
    assert int(df["is_ad"].eq(True).sum()) == 3
