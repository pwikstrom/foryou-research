"""Batch-and-chain invariants for the sessions_refresh build.

The headline test is golden equivalence: any partition of the corpus into
batches must produce identical artifacts, because every batch is centred on
the GLOBAL corpus mean. The anti-guard test proves the guard can fail: a
deliberately per-batch mean must break equivalence — a guard that cannot
fail is not a guard, and per-batch-centred distances are plausible-looking.
"""

import json
import multiprocessing

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

    # The worker only builds collections covered by >=1 study; a study with no
    # date bounds covers everything (wide defaults), so the golden-equivalence
    # comparisons against the unscoped in-process driver still hold.
    data_io.save_json(
        data={"Chain Study": {"SELECTED_COLLECTIONS": ["coll0", "coll1", "coll2"]}},
        storage_location="recoded", filename="studies.json")

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






_HAS_FORK = "fork" in multiprocessing.get_all_start_methods()




@pytest.mark.skipif(not _HAS_FORK, reason="forked worker pool needs the fork start method")
def test_golden_equivalence_across_worker_counts(corpus):
    """The pool is a wall-time device only: rows identical at any worker count."""
    meta_serial = se.build_artifacts(batch_size=99, workers=1)
    want = _read_artifacts()
    assert meta_serial["n_windows"] > 0

    reporter = FakeReporter()
    meta_pool = se.build_artifacts(batch_size=99, workers=3, reporter=reporter)
    got = _read_artifacts()
    assert meta_pool["n_sessions"] == meta_serial["n_sessions"]
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])
    # The pool genuinely ran — no silent degradation to the serial path.
    assert not any("worker pool failed" in line for line in reporter.lines)
    timing = [line for line in reporter.lines if "[TIMING] sessions_link" in line]
    assert timing and "workers=3" in timing[0] and "units=3" in timing[0]

    se.build_artifacts(batch_size=1, workers=3)
    got_one = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got_one[kind])




@pytest.mark.skipif(not _HAS_FORK, reason="forked worker pool needs the fork start method")
def test_session_chunking_does_not_change_rows(corpus, monkeypatch):
    """One session per work unit vs the default chunk: same rows, same order."""
    se.build_artifacts(batch_size=99, workers=1)
    want = _read_artifacts()

    monkeypatch.setattr(se, "SESSION_CHUNK_PLAYS", 1)
    se.build_artifacts(batch_size=99, workers=3)
    got = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])




def test_broken_pool_falls_back_to_serial(corpus, monkeypatch):
    """A pool failure must degrade to the serial path, never fail the run."""
    from concurrent.futures.process import BrokenProcessPool

    se.build_artifacts(batch_size=99, workers=1)
    want = _read_artifacts()

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise BrokenProcessPool("boom")

    monkeypatch.setattr(se, "ProcessPoolExecutor", _Boom)
    monkeypatch.setattr(se, "resolve_workers", lambda requested=None: 3)
    reporter = FakeReporter()
    meta = se.build_artifacts(batch_size=99, reporter=reporter)
    assert not meta.get("cancelled")
    got = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])
    assert any("worker pool failed" in line and "serially" in line
               for line in reporter.lines)




@pytest.mark.skipif(not _HAS_FORK, reason="forked worker pool needs the fork start method")
def test_cancel_under_pool_leaves_previous_artifacts_intact(corpus, monkeypatch):
    se.build_artifacts(batch_size=99, workers=1)
    want = _read_artifacts()

    monkeypatch.setattr(se, "_CANCEL_CHECK_EVERY", 1)
    reporter = FakeReporter(cancel_after=1)
    setup = worker.run_sessions_refresh(reporter, {"batch_size": 2, "workers": 2})
    out = worker.run_sessions_refresh(reporter, setup["next_task_args"])
    assert out is None
    got = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])




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
    assert links == 4  # setup-only link + one collection per batch link
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
    setup = worker.run_sessions_refresh(reporter, {"batch_size": 1})
    # Replay the first batch link (Cloud Tasks at-least-once): the first
    # execution chains; the duplicate loses the chain-dispatch claim and
    # stops, so a platform retry can no longer fork a second chain.
    replay = dict(setup["next_task_args"])
    chain2 = worker.run_sessions_refresh(reporter, dict(replay))
    chain2b = worker.run_sessions_refresh(reporter, dict(replay))
    assert chain2["next_task_args"]["chunk_index"] == 1
    assert chain2b is None, "the duplicate execution must not chain"
    args = chain2["next_task_args"]
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
    # Chain A runs to completion and publishes all 3 collections. Capture the
    # args of the SECOND batch link (chunk 1) — a trailing duplicate resumed
    # from there covers only collections 2..3, never 1.
    reporter = FakeReporter()
    args: dict = {"batch_size": 1}
    first_link_args = None
    while True:
        chain = worker.run_sessions_refresh(reporter, args)
        if (first_link_args is None and chain
                and chain["next_task_args"].get("chunk_index") == 1):
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
    setup = worker.run_sessions_refresh(reporter, {"batch_size": 2})
    out = worker.run_sessions_refresh(reporter, setup["next_task_args"])
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





# ---- Incremental (stale_only / merge) modes ----


def _set_study_defs(defs: dict) -> None:
    data_io.save_json(data=defs, storage_location="recoded",
                      filename="studies.json")




def test_full_run_writes_per_collection_block(corpus):
    _run_chain({"batch_size": 2})
    meta = data_io.load_json(storage_location="cache", filename=se.META_FILE)
    block = meta["collections"]
    assert sorted(block) == ["coll0", "coll1", "coll2"]
    for rec in block.values():
        assert rec["windows"] and rec["n_plays"] > 0 and rec["built_at"]




def test_stale_only_noops_when_nothing_changed(corpus, monkeypatch):
    _run_chain({"batch_size": 2})
    want = _read_artifacts()

    swept = []
    monkeypatch.setattr(se, "sweep_stale_run_files",
                        lambda run_id: swept.append(run_id))
    reporter, links = _run_chain({"stale_only": True})
    assert links == 1, "noop must not chain"
    assert reporter.progress[-1] == (100, "Up to date")
    assert swept == [], "noop must never sweep another run's files"
    got = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])




def test_stale_only_merges_only_the_changed_collection(corpus):
    _run_chain({"batch_size": 2})
    before = _read_artifacts()

    # Narrow coll1's window to before any of its plays: its rows must go,
    # while coll0/coll2 rows stay byte-identical (they are not re-segmented).
    _set_study_defs({
        "Chain Study": {"SELECTED_COLLECTIONS": ["coll0", "coll2"]},
        "Old Study": {"SELECTED_COLLECTIONS": ["coll1"],
                      "START_DATE": "2020-01-01", "END_DATE": "2020-01-02"},
    })
    reporter, links = _run_chain({"stale_only": True})
    assert links == 1, "a pure-drop merge publishes inline at setup"
    after = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        assert not (after[kind]["collection_id"] == "coll1").any()
        pd.testing.assert_frame_equal(
            before[kind][before[kind]["collection_id"] != "coll1"]
            .reset_index(drop=True),
            after[kind].reset_index(drop=True))
    meta = data_io.load_json(storage_location="cache", filename=se.META_FILE)
    assert sorted(meta["collections"]) == ["coll0", "coll2"]
    assert meta["n_collections"] == 2




def test_stale_only_resegments_a_window_change(corpus):
    _run_chain({"batch_size": 2})
    before = _read_artifacts()
    meta_before = data_io.load_json(storage_location="cache", filename=se.META_FILE)

    # Give coll2 its own explicit (still-covering) window: the fingerprint's
    # windows differ, so exactly coll2 is re-segmented; its data is unchanged
    # so its rows come back identical — but its built_at advances.
    _set_study_defs({
        "Chain Study": {"SELECTED_COLLECTIONS": ["coll0", "coll1"]},
        "New Study": {"SELECTED_COLLECTIONS": ["coll2"],
                      "START_DATE": "2026-02-01", "END_DATE": "2026-04-01"},
    })
    reporter, links = _run_chain({"stale_only": True})
    assert links == 2, "setup + one batch link for the single stale collection"
    after = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(before[kind].reset_index(drop=True),
                                      after[kind].reset_index(drop=True))
    meta = data_io.load_json(storage_location="cache", filename=se.META_FILE)
    assert meta["collections"]["coll2"]["built_at"] > \
        meta_before["collections"]["coll2"]["built_at"]
    assert meta["collections"]["coll0"]["built_at"] == \
        meta_before["collections"]["coll0"]["built_at"]




def test_targeted_collections_merge_preserves_the_rest(corpus):
    _run_chain({"batch_size": 2})
    before = _read_artifacts()

    reporter, links = _run_chain({"collections": "coll1"})
    assert links == 2  # setup + one batch link
    after = _read_artifacts()
    # Nothing changed in the data, so the merged artifacts equal the originals
    # — crucially INCLUDING coll0/coll2, which a targeted run used to drop.
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(
            before[kind].reset_index(drop=True),
            after[kind].reset_index(drop=True))
    meta = data_io.load_json(storage_location="cache", filename=se.META_FILE)
    assert meta["n_collections"] == 3




def test_skip_if_busy_yields_to_a_live_run(corpus):
    _run_chain({"batch_size": 2})
    want = _read_artifacts()
    # A fresh foreign progress file looks like an in-flight chain.
    data_io.save_json(data={"chunks": {}}, storage_location="cache",
                      filename=f"{se.PROGRESS_PREFIX}otherrun.json")

    reporter, links = _run_chain({"skip_if_busy": True})
    assert links == 1
    assert any("skipping" in m.lower() for m in reporter.lines)
    assert data_io.exists(storage_location="cache",
                          filename=f"{se.PROGRESS_PREFIX}otherrun.json"), \
        "the busy guard must not sweep the live run's files"
    got = _read_artifacts()
    for kind in ("sessions", "episodes", "windows"):
        pd.testing.assert_frame_equal(want[kind], got[kind])




def test_restart_args_keep_stale_only_but_strip_skip_if_busy(corpus, monkeypatch):
    # Make one collection stale so a stale_only run actually chains.
    data_io.remove(storage_location="cache", filename=se.META_FILE) \
        if data_io.exists(storage_location="cache", filename=se.META_FILE) else None
    reporter = FakeReporter()
    setup = worker.run_sessions_refresh(
        reporter, {"batch_size": 1, "stale_only": True, "skip_if_busy": True})
    args = setup["next_task_args"]
    assert args["stale_only"] is True
    assert "skip_if_busy" not in args

    def drifted(model, expected_fp=None, reporter=None):
        raise embedding_store.CorpusMeanDrift("store moved")

    monkeypatch.setattr(embedding_store, "get_corpus_mean", drifted)
    restart = worker.run_sessions_refresh(reporter, args)
    rargs = restart["next_task_args"]
    assert rargs["stale_only"] is True
    assert "skip_if_busy" not in rargs




def test_workers_ride_the_chain_but_stay_out_of_params(corpus, monkeypatch):
    """A worker count must survive chain + restart args, and must never reach
    ``params`` — anything there lands in the meta and would force a rebuild."""
    reporter = FakeReporter()
    setup = worker.run_sessions_refresh(reporter, {"batch_size": 1, "workers": 2})
    args = setup["next_task_args"]
    assert args["workers"] == 2
    assert "workers" not in json.loads(args["params_json"])
    assert "workers" not in se.default_params()

    def drifted(model, expected_fp=None, reporter=None):
        raise embedding_store.CorpusMeanDrift("store moved")

    monkeypatch.setattr(embedding_store, "get_corpus_mean", drifted)
    restart = worker.run_sessions_refresh(reporter, args)
    assert restart["next_task_args"]["workers"] == 2
