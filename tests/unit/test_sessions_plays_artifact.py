"""The sessions plays artifact: build, publish, and the detail read path.

``sessions_plays.parquet`` exists so the detail endpoint's per-collection read
prunes row groups instead of decoding the whole consolidated activity file.
These tests pin the contract: the shard is sorted, publish verifies row
counts (with a grace skip for pre-upgrade runs), and ``_session_plays``
returns identical frames from the artifact and the activity-file fallback.
"""

import pandas as pd
import pyarrow as pa
import pytest

import fyp.data_io as data_io
from fyp.analysis import session_explorer as se


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Local tmp cache/recoded storage locations."""
    from fyp.fyp_config import fyp_cf

    for loc in ("recoded", "cache"):
        d = tmp_path / loc
        d.mkdir()
        monkeypatch.setitem(fyp_cf["paths"], loc, str(d))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)
    return tmp_path






def _plays_frame():
    rows = [
        # Deliberately unsorted: collB before collA, timestamps shuffled.
        {"collection_id": "collB", "item_id": "v3",
         "_ts": pd.Timestamp("2026-03-02 10:04:00"), "play_duration": 12.0,
         "session_id": "collB__0", "source_platform": "tiktok"},
        {"collection_id": "collA", "item_id": "v2",
         "_ts": pd.Timestamp("2026-03-01 10:02:00"), "play_duration": 11.0,
         "session_id": "collA__0", "source_platform": "tiktok"},
        {"collection_id": "collA", "item_id": "v1",
         "_ts": pd.Timestamp("2026-03-01 10:00:00"), "play_duration": 10.0,
         "session_id": "collA__0", "source_platform": "tiktok"},
        # A null-session play, recovered by time span (na_ keys).
        {"collection_id": "collA", "item_id": "v9",
         "_ts": pd.Timestamp("2026-03-05 09:00:00"), "play_duration": 5.0,
         "session_id": None, "source_platform": "tiktok"},
    ]
    df = pd.DataFrame(rows)
    df["collection_id"] = df["collection_id"].astype("string")
    df["item_id"] = df["item_id"].astype("string")
    df["session_id"] = df["session_id"].astype("string")
    return df






def test_plays_table_is_sorted_and_schema_typed():
    tbl = se.plays_table(_plays_frame())
    assert tbl.num_rows == 4
    assert tbl.column("collection_id").to_pylist() == [
        "collA", "collA", "collA", "collB"]
    # Within a collection, time-sorted.
    ts = tbl.column("ts").to_pylist()
    assert ts[0] < ts[1] < ts[2]
    assert tbl.schema.field("ts").type == pa.timestamp("us")
    assert tbl.schema.field("play_duration").type == pa.float64()
    # The null session id survives (na_ recovery depends on it).
    assert tbl.column("session_id").to_pylist()[-2] is None or \
        None in tbl.column("session_id").to_pylist()




def test_plays_table_empty_is_schema_correct():
    tbl = se.plays_table(None)
    assert tbl.num_rows == 0
    assert set(tbl.schema.names) == {"collection_id", "session_id", "item_id",
                                     "ts", "play_duration", "source_platform"}




def test_publish_includes_plays_and_verifies_counts(storage):
    plays = _plays_frame()
    se.write_batch_shards("runX", 0, [], [], [], plays=plays)
    se.publish_artifacts(
        "runX", n_chunks=1,
        expected={"sessions": 0, "episodes": 0, "windows": 0, "plays": 4},
        meta={"n_plays": 4})
    df = data_io.load_parquet_selective(storage_location="cache",
                                        filename=se.PLAYS_FILE)
    assert len(df) == 4
    assert list(df["collection_id"].astype(str))[:3] == ["collA"] * 3
    # Shards were swept after publish.
    leftovers = [f for f in data_io.listdir(storage_location="cache")
                 if f.startswith(tuple(se.SHARD_PREFIXES.values()))]
    assert leftovers == []




def test_publish_rejects_a_plays_count_mismatch(storage):
    se.write_batch_shards("runY", 0, [], [], [], plays=_plays_frame())
    with pytest.raises(RuntimeError, match="plays"):
        se.publish_artifacts(
            "runY", n_chunks=1,
            expected={"sessions": 0, "episodes": 0, "windows": 0, "plays": 99},
            meta={})




def test_publish_skips_plays_for_a_pre_upgrade_run(storage):
    """A run whose links never wrote plays shards still publishes the rest."""
    se.write_batch_shards("runZ", 0, [], [], [], plays=_plays_frame())
    data_io.remove(storage_location="cache",
                   filename=se.shard_filename("plays", "runZ", 0))
    se.publish_artifacts(
        "runZ", n_chunks=1,
        expected={"sessions": 0, "episodes": 0, "windows": 0, "plays": 4},
        meta={})
    assert not data_io.exists(storage_location="cache", filename=se.PLAYS_FILE)
    assert data_io.exists(storage_location="cache", filename=se.SESSIONS_FILE)




def _write_activity_file(plays):
    from fyp.organize_datasets import COLLECTIONS_LABEL

    df = plays.rename(columns={"_ts": "local_timestamp"}).copy()
    df["local_timestamp"] = df["local_timestamp"].astype(str)
    df["activity_type"] = "play"
    data_io.save_parquet(df=df, storage_location="recoded",
                         filename=f"{COLLECTIONS_LABEL}_recoded.parquet")




def _session_row(session_id, start="2026-03-01 09:00:00", end="2026-03-06 00:00:00"):
    return pd.Series({"session_id": session_id, "start_ts": start, "end_ts": end})




def test_session_plays_artifact_matches_fallback(storage):
    import web_interface.routes.api_sessions_routes as mod

    plays = _plays_frame()
    _write_activity_file(plays)

    # Fallback first (no artifact yet).
    mod._STAT_CACHE.clear()
    fb = mod._session_plays("collA", _session_row("collA__0"))
    assert list(fb["item_id"]) == ["v1", "v2"]

    # Now publish the artifact and read again — identical rows.
    tbl = se.plays_table(plays)
    data_io.write_parquet_stream(storage_location="cache", filename=se.PLAYS_FILE,
                                 batches=[tbl], schema=tbl.schema)
    mod._STAT_CACHE.clear()
    art = mod._session_plays("collA", _session_row("collA__0"))
    assert list(art["item_id"]) == list(fb["item_id"])
    assert [t.isoformat() for t in art["_ts"]] == [t.isoformat() for t in fb["_ts"]]
    assert list(art["play_duration"]) == list(fb["play_duration"])
    assert list(art["source_platform"]) == list(fb["source_platform"])




def test_session_plays_recovers_na_sessions_from_the_artifact(storage):
    import web_interface.routes.api_sessions_routes as mod

    plays = _plays_frame()
    tbl = se.plays_table(plays)
    data_io.write_parquet_stream(storage_location="cache", filename=se.PLAYS_FILE,
                                 batches=[tbl], schema=tbl.schema)
    mod._STAT_CACHE.clear()
    got = mod._session_plays(
        "collA", _session_row("na_0", start="2026-03-05 08:00:00",
                              end="2026-03-05 10:00:00"))
    assert list(got["item_id"]) == ["v9"]




def test_session_plays_falls_back_when_artifact_lacks_the_collection(storage):
    """A stale plays artifact (collection absent) must not blank the detail."""
    import web_interface.routes.api_sessions_routes as mod

    plays = _plays_frame()
    _write_activity_file(plays)
    only_b = plays[plays["collection_id"] == "collB"]
    tbl = se.plays_table(only_b)
    data_io.write_parquet_stream(storage_location="cache", filename=se.PLAYS_FILE,
                                 batches=[tbl], schema=tbl.schema)
    mod._STAT_CACHE.clear()
    got = mod._session_plays("collA", _session_row("collA__0"))
    assert list(got["item_id"]) == ["v1", "v2"]
