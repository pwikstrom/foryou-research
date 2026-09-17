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
