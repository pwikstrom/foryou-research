"""The row-deduplication key must not include ``tz_offset``.

A re-donation of the same history with a corrected donor time zone carries
the same events with a different offset. Keying on the offset kept both
copies, doubling every overlapping row and contradicting the "re-running
ingest is idempotent" guarantee. The newest donation's row (and offset)
must win instead.

Also pins the ledger-note helper that carries a parser's per-file notes
(an ambiguous time-zone label it resolved) onto the ledger entry.
"""

import pandas as pd

from fyp.ingest.base import ForYouCollection
from web_interface.run_ingest_refresh import _withheld_note


def _rows(raw_file: str, cid: str, tz: int, added: str, n: int = 20) -> pd.DataFrame:
    return pd.DataFrame({
        "raw_file": raw_file,
        "collection_id": cid,
        "item_id": [f"item{i}" for i in range(n)],
        "activity_type": "play",
        "utc_timestamp": pd.to_datetime([1_700_000_000 + 60 * i for i in range(n)], unit="s", utc=True),
        "tz_offset": tz,
        "ts_added_to_dataset": pd.Timestamp(added, tz="UTC"),
    })


def test_redonation_with_corrected_zone_deduplicates_and_newest_offset_wins():
    first = _rows("old.json", "c_old", tz=0, added="2026-01-01")
    second = _rows("new.json", "c_new", tz=10, added="2026-06-01")
    collection = ForYouCollection(verbose=False)
    collection.data = pd.concat([first, second], ignore_index=True)
    collection.state = "processed"

    collection.identify_similar_file_content(overlap_threshold=0.2)

    out = collection.data
    assert len(out) == 20, "identical events must collapse to one row each"
    assert (out["tz_offset"] == 10).all(), "the newest donation's offset wins"
    assert out["collection_id"].nunique() == 1


def test_withheld_note_carries_parser_notes():
    assert _withheld_note({}) is None
    assert _withheld_note({"withheld_sections": ["Comments"]}) == "Uploader withheld: Comments"
    note = _withheld_note({
        "withheld_sections": ["Comments"],
        "parse_notes": ["Time zone: 12 row(s) carry an ambiguous abbreviation (IST); read as its most common Takeout meaning."],
    })
    assert note.startswith("Uploader withheld: Comments | Time zone: 12 row(s)")
    assert _withheld_note({"parse_notes": ["only a note"]}) == "only a note"


def test_a_pair_on_fewer_than_three_shared_seconds_never_merges():
    """A five-event capture coinciding with one second of a large export is a
    20 % overlap by the ratio alone; the shared-seconds floor keeps them apart."""
    big = _rows("export.json", "c_big", tz=10, added="2026-01-01", n=200)
    small = big.iloc[:1].copy()
    small = pd.concat([small, _rows("capture.ndjson", "c_small", tz=10, added="2026-02-01", n=4)
                       .assign(utc_timestamp=pd.to_datetime([1_800_000_000 + i for i in range(4)], unit="s", utc=True))])
    small["raw_file"] = "capture.ndjson"
    small["collection_id"] = "c_small"
    collection = ForYouCollection(verbose=False)
    collection.data = pd.concat([big, small], ignore_index=True)
    collection.state = "processed"

    collection.identify_similar_file_content(overlap_threshold=0.2)

    assert collection.data["collection_id"].nunique() == 2, "one shared second is not a re-donation"

    # Three shared seconds out of five clear the floor and the ratio: merged.
    small3 = big.iloc[:3].copy()
    small3["raw_file"] = "capture3.ndjson"
    small3["collection_id"] = "c_small3"
    small3 = pd.concat([small3, _rows("capture3.ndjson", "c_small3", tz=10, added="2026-02-01", n=2)
                        .assign(utc_timestamp=pd.to_datetime([1_800_000_000 + i for i in range(2)], unit="s", utc=True))])
    collection = ForYouCollection(verbose=False)
    collection.data = pd.concat([big, small3], ignore_index=True)
    collection.state = "processed"
    collection.identify_similar_file_content(overlap_threshold=0.2)
    assert collection.data["collection_id"].nunique() == 1
