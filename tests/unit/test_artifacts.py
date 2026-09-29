"""fyp.core.artifacts: the shared artifact names and their column resolution."""

import pandas as pd

from fyp.core import artifacts


def test_selective_names_match_the_on_disk_tuple_form():
    # load_parquet_selective matches the stringified tuple exactly.
    assert artifacts.metadata_column_names(artifacts.ACCEPTED_COLUMN, "accepted") == [
        "('other', 'accepted')",
        "accepted",
    ]


def test_metadata_column_prefers_the_tuple_then_the_first_flat_name():
    tuple_cols = pd.DataFrame(columns=[("other", "accepted"), "accepted"]).columns
    assert artifacts.metadata_column(tuple_cols, artifacts.ACCEPTED_COLUMN, "accepted") == (
        "other",
        "accepted",
    )
    flat = ["other_accepted", "accepted"]
    assert (
        artifacts.metadata_column(flat, artifacts.ACCEPTED_COLUMN, "accepted", "other_accepted")
        == "accepted"
    )
    assert artifacts.metadata_column(["x"], artifacts.ACCEPTED_COLUMN, "accepted") is None


def test_load_enrichment_status_is_none_before_the_first_consolidation(monkeypatch):
    monkeypatch.setattr(artifacts.data_io, "exists", lambda **kw: False)
    assert artifacts.load_enrichment_status() is None


def test_load_enrichment_status_reads_the_whole_frame(monkeypatch):
    frame = pd.DataFrame({"scraped_ok": [True]})
    calls = []
    monkeypatch.setattr(artifacts.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(artifacts.data_io, "load_parquet", lambda **kw: calls.append(kw) or frame)
    assert artifacts.load_enrichment_status() is frame
    assert calls == [{"storage_location": "recoded", "filename": "enrichment_status.parquet"}]
