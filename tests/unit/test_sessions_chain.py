"""Batch-and-chain invariants for the sessions_refresh build.

The headline test is golden equivalence: any partition of the corpus into
batches must produce identical artifacts, because every batch is centred on
the GLOBAL corpus mean. The anti-guard test proves the guard can fail: a
deliberately per-batch mean must break equivalence — a guard that cannot
fail is not a guard, and per-batch-centred distances are plausible-looking.
"""

import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import fyp.data_io as data_io
from fyp.analysis import embedding_store, embeddings, session_explorer as se
from web_interface import run_sessions_refresh as worker

DIM = 8






class FakeReporter:
    def __init__(self, cancel_after: int | None = None):
        self.lines: list[str] = []
        self.progress: list[tuple[int, str]] = []
        self._checks = 0
        self._cancel_after = cancel_after

    def log(self, msg):
        self.lines.append(str(msg))

    def update_progress(self, pct, msg):
        self.progress.append((int(pct), str(msg)))

    def emit_data(self, data):
        pass

    def check_cancelled(self):
        self._checks += 1
        return (self._cancel_after is not None
                and self._checks > self._cancel_after)






@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """Synthetic two-cluster corpus: shards + plays + video_map, local mode."""
    from fyp.fyp_config import fyp_cf

    for loc in ("recoded", "cache"):
        d = tmp_path / loc
        d.mkdir()
        monkeypatch.setitem(fyp_cf["paths"], loc, str(d))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)

    model = embeddings.active_embedding_backend().model_id()
    rng = np.random.default_rng(0)

    # Two tight clusters far apart -> episodes survive within a cluster.
    centres = np.array([[4.0] + [0.0] * (DIM - 1), [0.0] * (DIM - 1) + [4.0]])
    n_items = 60
    ids = [f"v{i:03d}" for i in range(n_items)]
    mat = np.vstack([
        centres[i % 2] + rng.standard_normal(DIM) * 0.05 for i in range(n_items)
    ]).astype(np.float16)

    # Two shards (split mid-corpus) so the sidecar has multiple parts.
    for lo, hi in ((0, 35), (35, n_items)):
        df = pd.DataFrame({
            "item_id": pd.array(ids[lo:hi], dtype="string[pyarrow]"),
            "embedding": pd.array(
                pa.array([r.tobytes() for r in mat[lo:hi]], type=pa.large_binary()),
                dtype=pd.ArrowDtype(pa.large_binary())),
            "model": pd.array([model] * (hi - lo), dtype="string[pyarrow]"),
            "dim": pd.array([DIM] * (hi - lo), dtype="int32[pyarrow]"),
        })
        data_io.save_parquet(df=df, storage_location="recoded",
                             filename=f"{embeddings.SHARD_PREFIX}{lo}{embeddings.SHARD_SUFFIX}")

    # Plays: 3 collections x 2 sessions x 8 distinct cluster-coherent videos.
    rows = []
    t0 = pd.Timestamp("2026-03-01 10:00:00")
    from itertools import count

    item_iter = count()
    for c in range(3):
        cid = f"coll{c}"
        for s in range(2):
            cluster = (c + s) % 2
            sess = f"{cid}__{s}"
            base = t0 + pd.Timedelta(days=c, hours=s * 3)
            for k in range(8):
                # Pick items from the session's cluster (parity of index).
                while True:
                    i = next(item_iter) % n_items
                    if i % 2 == cluster:
                        break
                rows.append({
                    "collection_id": cid, "item_id": ids[i],
                    "local_timestamp": (base + pd.Timedelta(minutes=2 * k)).isoformat(),
                    "play_duration": 10 + k, "session_id": sess,
                    "source_platform": "tiktok", "activity_type": "play",
                })
    plays = pd.DataFrame(rows).astype({
        "collection_id": "string[pyarrow]", "item_id": "string[pyarrow]",
        "local_timestamp": "string[pyarrow]", "session_id": "string[pyarrow]",
        "source_platform": "string[pyarrow]", "activity_type": "string[pyarrow]"})
    from fyp.organize_datasets import COLLECTIONS_LABEL

    data_io.save_parquet(df=plays, storage_location="recoded",
                         filename=f"{COLLECTIONS_LABEL}_recoded.parquet")

    vm = pd.DataFrame({
        "item_id": pd.array(ids, dtype="string[pyarrow]"),
        "niche_name": pd.array([f"niche{i % 2}" for i in range(n_items)], dtype="string[pyarrow]"),
        "category": pd.array(["cat"] * n_items, dtype="string[pyarrow]"),
        "story": pd.array(["s"] * n_items, dtype="string[pyarrow]"),
        "political_score": pd.array([0.0] * n_items, dtype="double[pyarrow]"),
        "sensitivity_score": pd.array([0.0] * n_items, dtype="double[pyarrow]"),
        "advertising": pd.array(["no"] * n_items, dtype="string[pyarrow]"),
    })
    data_io.save_parquet(df=vm, storage_location="recoded", filename="video_map.parquet")

    embedding_store.ensure_dense_store(model)
    return {"model": model, "ids": ids, "n_collections": 3}






def _read_artifacts():
    out = {}
    for kind, fn, keys in (("sessions", se.SESSIONS_FILE, ["collection_id", "session_id"]),
                           ("episodes", se.EPISODES_FILE, ["collection_id", "session_id", "episode_idx"]),
                           ("windows", se.WINDOWS_FILE, ["collection_id", "session_id", "window_idx"])):
        df = data_io.load_parquet_selective(storage_location="cache", filename=fn)
        out[kind] = df.sort_values(keys, kind="stable").reset_index(drop=True)
    return out






def test_golden_equivalence_across_batch_sizes(corpus):
    meta_all = se.build_artifacts(batch_size=99)
    got_all = _read_artifacts()
    assert meta_all["n_sessions"] == 6
    assert meta_all["n_windows"] > 0  # the fixture must exercise windows

    meta_one = se.build_artifacts(batch_size=1)
    got_one = _read_artifacts()

    assert meta_one["n_sessions"] == meta_all["n_sessions"]
    assert meta_one["n_episodes"] == meta_all["n_episodes"]
    assert meta_one["n_windows"] == meta_all["n_windows"]
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(got_all[kind], got_one[kind])






def test_anti_guard_per_batch_mean_breaks_equivalence(corpus, monkeypatch):
    """A guard that cannot fail is not a guard: per-batch centring MUST differ."""
    meta_all = se.build_artifacts(batch_size=99)
    got_all = _read_artifacts()
    assert meta_all["n_windows"] > 0

    def per_batch_mean_block(model, item_ids, corpus_mean, index=None):
        if index is None:
            index = embedding_store.load_index(model)
        rows, found = index.lookup(item_ids)
        U = embedding_store.read_vectors(model, rows, index, dtype=np.float32)
        # THE BUG THIS SUITE GUARDS AGAINST: centring on the batch's own mean.
        se._directionalise(U, U.mean(axis=0, dtype=np.float64))
        found_ids = [str(i) for i, f in zip(item_ids, found) if f]
        return {iid: i for i, iid in enumerate(found_ids)}, U

    monkeypatch.setattr(se, "load_directional_block", per_batch_mean_block)
    se.build_artifacts(batch_size=1)
    got_bad = _read_artifacts()

    same = np.allclose(
        got_all["sessions"]["min_window_cosdist"].to_numpy(dtype=float),
        got_bad["sessions"]["min_window_cosdist"].to_numpy(dtype=float),
        equal_nan=True)
    assert not same, "per-batch mean produced identical distances — the guard is dead"






def _run_chain(task_args=None, reporter=None, max_links=50):
    reporter = reporter or FakeReporter()
    task_args = dict(task_args or {})
    links = 0
    while True:
        chain = worker.run_sessions_refresh(reporter, task_args)
        links += 1
        if not chain:
            return reporter, links
        task_args = chain["next_task_args"]
        assert links < max_links






def test_chained_worker_matches_single_shot(corpus):
    se.build_artifacts(batch_size=99)
    want = _read_artifacts()

    reporter, links = _run_chain({"batch_size": 1})
    assert links == 3  # one collection per link
    got = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])

    # Meta finalised; intermediate shards + progress cleaned up.
    meta = data_io.load_json(storage_location="cache", filename=se.META_FILE)
    assert meta["n_sessions"] == 6 and meta["n_collections"] == 3
    assert meta["store_fingerprint"]
    leftovers = [fn for fn in data_io.listdir(storage_location="cache")
                 if fn.startswith(tuple(se.SHARD_PREFIXES.values()) + (se.PROGRESS_PREFIX,))]
    assert leftovers == []
    assert reporter.progress[-1][0] == 100






def test_retry_of_a_link_does_not_duplicate_rows(corpus):
    reporter = FakeReporter()
    task_args: dict = {"batch_size": 1}
    chain = worker.run_sessions_refresh(reporter, task_args)
    # Replay link 1 (Cloud Tasks at-least-once), then continue normally.
    replay = dict(chain["next_task_args"])
    chain2 = worker.run_sessions_refresh(reporter, dict(replay))
    chain2b = worker.run_sessions_refresh(reporter, dict(replay))
    assert chain2["next_task_args"]["chunk_index"] == chain2b["next_task_args"]["chunk_index"]
    args = chain2b["next_task_args"]
    while True:
        nxt = worker.run_sessions_refresh(reporter, args)
        if not nxt:
            break
        args = nxt["next_task_args"]

    meta = data_io.load_json(storage_location="cache", filename=se.META_FILE)
    assert meta["n_sessions"] == 6  # not 8 — the replayed link overwrote its shard






def test_trailing_duplicate_chain_cannot_clobber_a_published_artifact(corpus):
    """The prod incident of 2026-08-09, reproduced.

    A Cloud Tasks retry re-delivers the SAME task_args, so duplicate chains
    share a run_id. The first to finish publishes and deletes both the shards
    and the progress file; the trailing chain then rebuilds a progress file
    covering only its remaining chunks and agrees with its own truncated shard
    set. Row-count verification alone waves that through and a partial
    artifact silently replaces a complete one.
    """
    # Chain A runs to completion and publishes all 3 collections.
    reporter = FakeReporter()
    args: dict = {"batch_size": 1}
    first_link_args = None
    while True:
        chain = worker.run_sessions_refresh(reporter, args)
        if first_link_args is None and chain:
            first_link_args = dict(chain["next_task_args"])
        if not chain:
            break
        args = chain["next_task_args"]

    good = _read_artifacts()
    good_meta = data_io.load_json(storage_location="cache", filename=se.META_FILE)
    assert good_meta["n_collections"] == 3

    # Chain B resumes from link 1 with the SAME run_id, after A already
    # published and swept. It covers only collections 2..3 — never 1.
    trailing = FakeReporter()
    args = first_link_args
    with pytest.raises(RuntimeError) as excinfo:
        while True:
            chain = worker.run_sessions_refresh(trailing, args)
            if not chain:
                break
            args = chain["next_task_args"]
    assert "collections" in str(excinfo.value) or "incomplete shard set" in str(excinfo.value)

    # The complete artifact is untouched.
    after = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(good[kind], after[kind])
    assert data_io.load_json(storage_location="cache",
                             filename=se.META_FILE)["n_collections"] == 3






def test_corpus_drift_restarts_chain_bounded(corpus, monkeypatch):
    reporter = FakeReporter()
    chain = worker.run_sessions_refresh(reporter, {"batch_size": 1})
    fp = chain["next_task_args"]["corpus_mean_fp"]
    assert fp

    def drifted(model, expected_fp=None, reporter=None):
        raise embedding_store.CorpusMeanDrift("store moved")

    monkeypatch.setattr(embedding_store, "get_corpus_mean", drifted)
    restart = worker.run_sessions_refresh(reporter, chain["next_task_args"])
    assert restart["next_task_args"]["chunk_index"] == 0
    assert restart["next_task_args"]["chain_restarts"] == 1
    assert restart["next_task_args"].get("batch_size") == 1

    # Beyond the bound: fail rather than loop forever.
    exhausted = dict(chain["next_task_args"])
    exhausted["chain_restarts"] = worker.MAX_CHAIN_RESTARTS
    with pytest.raises(RuntimeError, match="giving up"):
        worker.run_sessions_refresh(reporter, exhausted)






def test_cancel_leaves_previous_artifacts_intact(corpus):
    se.build_artifacts(batch_size=99)
    want = _read_artifacts()

    reporter = FakeReporter(cancel_after=1)
    out = worker.run_sessions_refresh(reporter, {"batch_size": 2})
    assert out is None  # no chain, no publish
    got = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])






def test_publish_order_sessions_index_last(corpus, monkeypatch):
    order: list[str] = []
    real_concat = data_io.concat_parquet_files

    def spy(**kwargs):
        order.append(kwargs["dst_filename"])
        return real_concat(**kwargs)

    monkeypatch.setattr(data_io, "concat_parquet_files", spy)
    monkeypatch.setattr(se.data_io, "concat_parquet_files", spy)
    _run_chain({"batch_size": 2})
    assert order[-1] == se.SESSIONS_FILE
    assert set(order[:-1]) == {se.PLAYS_FILE, se.EPISODES_FILE, se.WINDOWS_FILE}
