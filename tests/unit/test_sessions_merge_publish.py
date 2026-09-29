"""merge_publish_artifacts: row replacement, guards, and write order.

The end-to-end merge flows are covered in test_sessions_chain.py; these
tests pin the publish-time guarantees in isolation: only the targeted
collections' rows are replaced/dropped, every guard refuses BEFORE any
artifact is touched, and the sessions index is still written last.
"""

import pandas as pd
import pyarrow as pa
import pytest

import fyp.analysis.sessions.inputs as sessions_inputs
import fyp.analysis.sessions.publish as sessions_publish
import fyp.core.data_io as data_io
from fyp.analysis import session_explorer as se

TREND: list[str] = []


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Local tmp cache/recoded storage locations."""
    from fyp.core.fyp_config import fyp_cf

    for loc in ("recoded", "cache"):
        d = tmp_path / loc
        d.mkdir()
        monkeypatch.setitem(fyp_cf["paths"], loc, str(d))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)
    return tmp_path


def _rows(kind: str, cids: list[str], tag: str) -> list[dict]:
    """Minimal schema-valid rows, one per collection, labelled via session_id."""
    key = {"sessions": "session_id", "episodes": "session_id", "windows": "session_id"}[kind]
    return [{"collection_id": cid, key: f"{cid}__{tag}"} for cid in cids]


def _write_artifact(kind: str, final: str, cids: list[str], tag: str) -> None:
    tbl = sessions_publish._arrow_table(
        _rows(kind, cids, tag),
        {
            "sessions": sessions_publish.sessions_schema(TREND),
            "episodes": sessions_publish._EPISODES_SCHEMA,
            "windows": sessions_publish._WINDOWS_SCHEMA,
        }[kind],
    )
    data_io.write_parquet_stream(
        storage_location="cache", filename=final, batches=[tbl], schema=tbl.schema
    )


def _seed(run_id: str, refresh: list[str], n_chunks: int = 1):
    """Old artifacts with A,B,C + one shard set containing `refresh` rows."""
    for kind, final in (
        ("sessions", sessions_inputs.SESSIONS_FILE),
        ("episodes", sessions_inputs.EPISODES_FILE),
        ("windows", sessions_inputs.WINDOWS_FILE),
    ):
        _write_artifact(kind, final, ["A", "B", "C"], "old")
    ptbl = sessions_publish.plays_table(None)
    data_io.write_parquet_stream(
        storage_location="cache",
        filename=sessions_inputs.PLAYS_FILE,
        batches=[ptbl],
        schema=ptbl.schema,
    )
    for chunk in range(n_chunks):
        for kind in ("sessions", "episodes", "windows"):
            tbl = sessions_publish._arrow_table(
                _rows(kind, refresh, "new"),
                {
                    "sessions": sessions_publish.sessions_schema(TREND),
                    "episodes": sessions_publish._EPISODES_SCHEMA,
                    "windows": sessions_publish._WINDOWS_SCHEMA,
                }[kind],
            )
            data_io.write_parquet_stream(
                storage_location="cache",
                filename=sessions_publish.shard_filename(kind, run_id, chunk),
                batches=[tbl] if chunk == 0 else [],
                schema=tbl.schema,
            )
        data_io.write_parquet_stream(
            storage_location="cache",
            filename=sessions_publish.shard_filename("plays", run_id, chunk),
            batches=[],
            schema=sessions_publish.plays_table(None).schema,
        )


def _ids(final: str) -> list[str]:
    df = data_io.load_parquet_selective(
        storage_location="cache", filename=final, columns=["collection_id", "session_id"]
    )
    return sorted(df["session_id"].astype(str))


def _expected(refresh: list[str]) -> dict:
    n = len(refresh)
    return {"sessions": n, "episodes": n, "windows": n, "plays": 0}


def test_merge_replaces_refreshed_and_drops_departed(storage):
    _seed("run1", refresh=["B"])
    meta = {"collections": {"A": {}, "B": {}}}
    sessions_publish.merge_publish_artifacts(
        "run1",
        n_chunks=1,
        refresh_cids=["B"],
        drop_cids=["C"],
        expected=_expected(["B"]),
        meta=meta,
        trend_cols=TREND,
        covered_collections=1,
    )
    for final in (
        sessions_inputs.SESSIONS_FILE,
        sessions_inputs.EPISODES_FILE,
        sessions_inputs.WINDOWS_FILE,
    ):
        assert _ids(final) == ["A__old", "B__new"]
    saved = data_io.load_json(storage_location="cache", filename=sessions_inputs.META_FILE)
    assert saved["n_sessions"] == 2 and saved["n_collections"] == 2
    leftovers = [
        fn
        for fn in data_io.listdir(storage_location="cache")
        if fn.startswith(tuple(sessions_inputs.SHARD_PREFIXES.values()))
    ]
    assert leftovers == []


def test_partial_coverage_refuses_before_touching_artifacts(storage):
    _seed("run1", refresh=["B"])
    with pytest.raises(RuntimeError, match="covered"):
        sessions_publish.merge_publish_artifacts(
            "run1",
            n_chunks=1,
            refresh_cids=["B", "X"],
            drop_cids=[],
            expected=_expected(["B"]),
            meta={},
            trend_cols=TREND,
            covered_collections=1,
        )
    for final in (
        sessions_inputs.SESSIONS_FILE,
        sessions_inputs.EPISODES_FILE,
        sessions_inputs.WINDOWS_FILE,
    ):
        assert _ids(final) == ["A__old", "B__old", "C__old"]


def test_row_count_mismatch_refuses_before_touching_artifacts(storage):
    _seed("run1", refresh=["B"])
    bad = dict(_expected(["B"]), sessions=99)
    with pytest.raises(RuntimeError, match="shard rows"):
        sessions_publish.merge_publish_artifacts(
            "run1",
            n_chunks=1,
            refresh_cids=["B"],
            drop_cids=[],
            expected=bad,
            meta={},
            trend_cols=TREND,
            covered_collections=1,
        )
    for final in (
        sessions_inputs.SESSIONS_FILE,
        sessions_inputs.EPISODES_FILE,
        sessions_inputs.WINDOWS_FILE,
    ):
        assert _ids(final) == ["A__old", "B__old", "C__old"]


def test_incomplete_shard_set_refuses(storage):
    _seed("run1", refresh=["B"])
    data_io.remove(
        storage_location="cache", filename=sessions_publish.shard_filename("windows", "run1", 0)
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        sessions_publish.merge_publish_artifacts(
            "run1",
            n_chunks=1,
            refresh_cids=["B"],
            drop_cids=[],
            expected=_expected(["B"]),
            meta={},
            trend_cols=TREND,
            covered_collections=1,
        )
    assert _ids(sessions_inputs.SESSIONS_FILE) == ["A__old", "B__old", "C__old"]


def test_old_schema_drift_refuses(storage):
    _seed("run1", refresh=["B"])
    # Rewrite the sessions artifact with an extra column: setup should have
    # escalated to full, so the merge must refuse rather than mix schemas.
    schema = dict(sessions_publish.sessions_schema(TREND))
    schema["surprise_col"] = pa.string()
    tbl = sessions_publish._arrow_table(_rows("sessions", ["A"], "old"), schema)
    data_io.write_parquet_stream(
        storage_location="cache",
        filename=sessions_inputs.SESSIONS_FILE,
        batches=[tbl],
        schema=tbl.schema,
    )
    with pytest.raises(RuntimeError, match="columns differ"):
        sessions_publish.merge_publish_artifacts(
            "run1",
            n_chunks=1,
            refresh_cids=["B"],
            drop_cids=[],
            expected=_expected(["B"]),
            meta={},
            trend_cols=TREND,
            covered_collections=1,
        )


def test_merge_writes_sessions_index_last(storage, monkeypatch):
    _seed("run1", refresh=["B"])
    order: list[str] = []
    real = data_io.write_parquet_stream

    def spy(**kwargs):
        fn = kwargs["filename"]
        if fn in (
            sessions_inputs.SESSIONS_FILE,
            sessions_inputs.EPISODES_FILE,
            sessions_inputs.WINDOWS_FILE,
            sessions_inputs.PLAYS_FILE,
        ):
            order.append(fn)
        return real(**kwargs)

    monkeypatch.setattr(sessions_publish.data_io, "write_parquet_stream", spy)
    sessions_publish.merge_publish_artifacts(
        "run1",
        n_chunks=1,
        refresh_cids=["B"],
        drop_cids=[],
        expected=_expected(["B"]),
        meta={"collections": {}},
        trend_cols=TREND,
        covered_collections=1,
    )
    assert order[-1] == sessions_inputs.SESSIONS_FILE
    assert set(order[:-1]) == {
        sessions_inputs.PLAYS_FILE,
        sessions_inputs.EPISODES_FILE,
        sessions_inputs.WINDOWS_FILE,
    }


def test_missing_old_artifact_degrades_to_shards_only(storage):
    _seed("run1", refresh=["B"])
    data_io.remove(storage_location="cache", filename=sessions_inputs.WINDOWS_FILE)
    sessions_publish.merge_publish_artifacts(
        "run1",
        n_chunks=1,
        refresh_cids=["B"],
        drop_cids=[],
        expected=_expected(["B"]),
        meta={"collections": {}},
        trend_cols=TREND,
        covered_collections=1,
    )
    assert _ids(sessions_inputs.WINDOWS_FILE) == ["B__new"]
    assert _ids(sessions_inputs.SESSIONS_FILE) == ["A__old", "B__new", "C__old"]
