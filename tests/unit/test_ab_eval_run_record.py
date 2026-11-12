"""A test run records its annotation versions, honours the configured
test-set limit, and can be spread over several background tasks.

Cost-free and storage-free: arms run through a stub runner and every
``data_io`` call the harness makes lands in an in-memory store.

* Each arm of a run's manifest carries the ``av_`` identifier it annotated
  under (the identity a production annotation would be stamped with for that
  contract, backend and overrides), and the arm's contract text is kept in the
  run folder. A manifest written before this still loads.
* The test-set limit comes from ``[machine] test_set_max_items``.
* ``execute_run`` checkpoints every few videos; a paused run continues from
  its own folder and ends with the report a single pass would have written.
"""

import copy
import json
import tomllib

import pytest

from fyp.annotation import ab_eval, human_eval
from fyp.annotation import annotation_contract as ac
from fyp.annotation import annotation_versioning as av
from fyp.core import data_io
from fyp.core.fyp_config import fyp_cf
from web_interface.workers import run_ab_eval as worker


class _Runner:
    """Runner double: one empty-but-valid row per video, counting every call."""

    def __init__(self):
        self.annotated: list[str] = []

    def run(self, prompt_text, response_schema, item_ids, platform_map, progress_cb=None):
        rows = []
        for done, item_id in enumerate(item_ids, start=1):
            self.annotated.append(str(item_id))
            rows.append(
                {
                    "item_id": str(item_id),
                    "model": "stub",
                    "parsed": None,
                    "response": "",
                    "finish_reason": "DNF - stub",
                    "usage": {"prompt_tokens": 1, "candidates_tokens": 1, "total_tokens": 2},
                    "inference_duration": 0.0,
                    "error": "stub",
                }
            )
            if progress_cb:
                progress_cb(done, len(item_ids))
        return rows


class _Reporter:
    """The slice of TaskStatusReporter the worker uses."""

    def __init__(self):
        self.progress: list[int] = []
        self.logs: list[str] = []

    def update_progress(self, pct, message=""):
        self.progress.append(pct)

    def log(self, message):
        self.logs.append(message)

    def emit_data(self, data):
        pass

    def check_cancelled(self):
        return False


@pytest.fixture
def store(monkeypatch):
    """Route the harness's storage onto a dict, and skip the platform lookup."""
    files: dict = {}

    def _save(data=None, storage_location="cache", filename="", **kw):
        files[(storage_location, filename)] = copy.deepcopy(data)

    def _save_text(data="", storage_location="cache", filename="", **kw):
        files[(storage_location, filename)] = data

    def _save_parquet(df=None, storage_location="cache", filename="", **kw):
        files[(storage_location, filename)] = df.copy()

    monkeypatch.setattr(data_io, "save_json", _save)
    monkeypatch.setattr(data_io, "save_text", _save_text)
    monkeypatch.setattr(data_io, "save_parquet", _save_parquet)
    monkeypatch.setattr(
        data_io,
        "load_json",
        lambda storage_location="cache", filename="", **kw: copy.deepcopy(
            files.get((storage_location, filename))
        ),
    )
    monkeypatch.setattr(
        data_io,
        "load_text",
        lambda storage_location="cache", filename="", **kw: files.get((storage_location, filename)),
    )
    monkeypatch.setattr(
        data_io,
        "load_parquet",
        lambda storage_location="cache", filename="", **kw: files[
            (storage_location, filename)
        ].copy(),
    )
    monkeypatch.setattr(
        data_io,
        "exists",
        lambda storage_location="cache", filename="", **kw: (
            (
                storage_location,
                filename,
            )
            in files
        ),
    )
    monkeypatch.setattr(
        data_io,
        "remove",
        lambda storage_location="cache", filename="", **kw: files.pop(
            (storage_location, filename), None
        ),
    )
    monkeypatch.setattr(ab_eval, "ensure_locations", lambda: None)
    import fyp.annotation.machine_annotation as ma

    monkeypatch.setattr(ma, "platform_map_for", lambda ids: {})
    return files


def _live_text() -> str:
    return ac._read_baked_text()


def _candidate_text() -> str:
    """The live contract with one instruction changed (a different instrument)."""
    live_text = _live_text()
    cand = copy.deepcopy(tomllib.loads(live_text))
    cand["prompt"]["footer"] = "CANDIDATE FOOTER (run-record unit test)"
    return ac.serialize_contract(cand, base_text=live_text)


def _run_file(run_id: str, name: str):
    return (ab_eval.LOCATION, ab_eval._run_file(run_id, name))


# ---------------------------------------------------------------------------
# The run records the version and the contract of every arm
# ---------------------------------------------------------------------------


def test_manifest_records_each_arms_annotation_version(store):
    live_text, cand_text = _live_text(), _candidate_text()
    ab_eval.execute_run(
        run_id="r1",
        arms=[
            {"name": "live", "source": "live", "text": live_text},
            {"name": "cand", "source": "candidate", "text": cand_text, "candidate": "cand"},
            {"name": "warm", "source": "live", "text": live_text, "temperature": 0.7},
        ],
        item_ids=["1", "2"],
        runner=_Runner(),
    )
    manifest = store[_run_file("r1", "manifest.json")]
    arms = {a["name"]: a for a in manifest["arms"]}

    def _expected(text, overrides=None):
        contract, _ = ac.parse_and_validate(text)
        return av.contract_version_descriptor(contract, "gemini", overrides)[0]

    assert arms["live"]["annotation_version"] == _expected(live_text)["annotation_version"]
    assert arms["cand"]["annotation_version"] == _expected(cand_text)["annotation_version"]
    assert all(a["annotation_version"].startswith("av_") for a in arms.values())
    # A changed instruction, and a changed setting, are each a different version.
    assert len({a["annotation_version"] for a in arms.values()}) == 3
    assert (
        arms["warm"]["annotation_version"]
        == (_expected(live_text, {"temperature": 0.7})["annotation_version"])
    )
    assert arms["warm"]["version_descriptor"]["gen_params"]["temperature"] == 0.7
    assert (
        arms["live"]["version_descriptor"]["prompt_hash"]
        != (arms["cand"]["version_descriptor"]["prompt_hash"])
    )
    assert ab_eval.arm_versions(manifest) == {
        name: arm["annotation_version"] for name, arm in arms.items()
    }


def test_live_arm_version_is_the_one_production_would_stamp(store, monkeypatch):
    """The recorded id equals the active version for the same contract and backend."""
    from fyp.annotation import backends

    monkeypatch.setattr(backends, "active_backend_name", lambda: "gemini")
    live = ac.load_contract()
    recorded = ab_eval.arm_version(live, "gemini", {})
    assert recorded["annotation_version"] == av.active_annotation_version(fresh=True)


def test_arm_contract_text_is_stored_in_the_run_folder(store):
    live_text, cand_text = _live_text(), _candidate_text()
    ab_eval.execute_run(
        run_id="r2",
        arms=[
            {"name": "live", "source": "live", "text": live_text},
            {"name": "cand", "source": "candidate", "text": cand_text},
        ],
        item_ids=["1"],
        runner=_Runner(),
    )
    assert ab_eval.load_arm_contract_text("r2", "live") == live_text
    assert ab_eval.load_arm_contract_text("r2", "cand") == cand_text
    manifest = store[_run_file("r2", "manifest.json")]
    assert {a["contract_file"] for a in manifest["arms"]} == {
        "contract_live.toml",
        "contract_cand.toml",
    }
    assert all(location == ab_eval.LOCATION for location, _ in store)  # isolation rule

    assert ab_eval.delete_run("r2")
    assert not [key for key in store if "/r2/" in key[1]]


def test_manifest_from_before_versions_were_recorded_still_loads(store):
    """An old run has no version, no contract file: it reads as 'not recorded'."""
    old_manifest = {
        "run_id": "old",
        "status": "complete",
        "item_ids": ["1"],
        "n_items": 1,
        "arms": [{"name": "live", "source": "live", "etag": "x", "backend": "gemini"}],
    }
    store[_run_file("old", "manifest.json")] = old_manifest
    store[_run_file("old", "report.json")] = {"run_id": "old", "arms": ["live"], "comparisons": {}}

    run = ab_eval.load_run("old")
    assert run["manifest"]["arms"][0]["name"] == "live"
    assert ab_eval.arm_versions(run["manifest"]) == {"live": None}
    assert ab_eval.load_arm_contract_text("old", "live") is None
    assert human_eval.load_human("old") is None  # the run page's human block
    with pytest.raises(ValueError, match="no stored contract"):
        ab_eval.stored_arms("old")
    assert ab_eval.delete_run("old")


# ---------------------------------------------------------------------------
# The test-set limit is a setting
# ---------------------------------------------------------------------------


def test_test_set_limit_comes_from_config(store, monkeypatch):
    machine = fyp_cf["machine"]
    monkeypatch.delitem(machine, "test_set_max_items", raising=False)
    assert ab_eval.max_eval_items() == ab_eval.DEFAULT_MAX_EVAL_ITEMS == 50

    for unusable in (0, -3, "many", None):
        monkeypatch.setitem(machine, "test_set_max_items", unusable)
        assert ab_eval.max_eval_items() == 50

    monkeypatch.setitem(machine, "test_set_max_items", 200)
    assert ab_eval.max_eval_items() == 200

    # The run accepts a set the default would refuse, and still refuses one
    # over the configured limit.
    arms = [{"name": "live", "source": "live", "text": _live_text()}]
    ids = [str(i) for i in range(200)]
    summary = ab_eval.execute_run("big", arms, ids, runner=_Runner())
    assert summary["status"] == "complete" and summary["n_items"] == 200
    with pytest.raises(ValueError, match="200-item cap"):
        ab_eval.execute_run("too-big", arms, [*ids, "x"], runner=_Runner())

    # The sampler and the set store apply the same number.
    import pandas as pd

    frame = pd.DataFrame(
        {
            "item_id": [str(i) for i in range(500)],
            "source_platform": "tiktok",
            "video_downloaded": True,
            "scraped_ok": True,
        }
    )
    monkeypatch.setattr(ab_eval, "_enrichment_status_frame", lambda: frame)
    assert len(ab_eval.sample_items(400, seed=1)) == 200
    monkeypatch.setattr(ab_eval, "_save_sets_store", lambda s: s)
    assert len(ab_eval.save_eval_set(ids, name="ref")["item_ids"]) == 200
    with pytest.raises(ValueError, match="capped at 200"):
        ab_eval.save_eval_set([*ids, "x"], name="ref")


# ---------------------------------------------------------------------------
# A long run pauses at a checkpoint and continues from its own folder
# ---------------------------------------------------------------------------


def _two_arms():
    return [
        {"name": "live", "source": "live", "text": _live_text()},
        {"name": "cand", "source": "candidate", "text": _candidate_text(), "candidate": "c"},
    ]


def test_paused_run_continues_and_matches_a_single_pass(store):
    ids = [str(i) for i in range(20)]  # 20 videos = 3 chunks of 8 per arm

    reference = _Runner()
    ab_eval.execute_run("one-pass", _two_arms(), ids, runner=reference)

    runner = _Runner()
    summary = ab_eval.execute_run("chained", _two_arms(), ids, runner=runner, pause_cb=lambda: True)
    # Asked to pause at once, the call still annotates one chunk first.
    assert summary["paused"] and summary["status"] == "running"
    assert len(runner.annotated) == ab_eval.CHUNK_ITEMS
    assert store[_run_file("chained", "manifest.json")]["status"] == "running"
    assert _run_file("chained", "report.json") not in store
    assert ab_eval.arm_progress("chained") == {"live": 8, "cand": 0}

    links = 1
    while summary["paused"]:
        manifest, arms = ab_eval.stored_arms("chained")
        summary = ab_eval.execute_run(
            "chained",
            arms,
            manifest["item_ids"],
            runner=runner,
            resume=True,
            pause_cb=lambda: True,
        )
        links += 1
        assert links < 20, "a paused run must make progress on every link"

    assert links == 6  # 3 chunks per arm, one chunk per link
    assert summary["status"] == "complete"
    # No video is annotated twice, and none is skipped.
    assert sorted(runner.annotated) == sorted(reference.annotated)
    assert len(runner.annotated) == len(ids) * 2

    chained, one_pass = (
        store[_run_file(run_id, "report.json")] for run_id in ("chained", "one-pass")
    )
    for key in ("arms", "comparisons", "distributions", "adjudication", "costs"):
        assert json.dumps(chained[key], sort_keys=True, default=str) == json.dumps(
            one_pass[key], sort_keys=True, default=str
        ), key
    for arm in ("live", "cand"):
        rows = store[_run_file("chained", f"raw_{arm}.json")]
        assert [r["item_id"] for r in rows] == ids
    # The manifest written at the start is the one the run finished under.
    first, last = (store[_run_file(r, "manifest.json")] for r in ("one-pass", "chained"))
    assert [a["annotation_version"] for a in first["arms"]] == [
        a["annotation_version"] for a in last["arms"]
    ]


def test_continued_run_uses_the_contracts_it_started_with(store):
    """A candidate edited mid-run does not change what the run annotates with."""
    ids = [str(i) for i in range(10)]
    started_with = _candidate_text()
    arms = [{"name": "cand", "source": "candidate", "text": started_with, "candidate": "c"}]
    ab_eval.execute_run("r", arms, ids, runner=_Runner(), pause_cb=lambda: True)

    manifest, stored = ab_eval.stored_arms("r")
    assert stored[0]["text"] == started_with
    assert stored[0]["candidate"] == "c" and stored[0]["backend"] == "gemini"
    assert manifest["item_ids"] == ids


def test_worker_chains_until_the_run_is_complete(store, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(ab_eval, "_runner_for_arm", lambda arm, cancel_cb=None: runner)
    monkeypatch.setattr(ab_eval, "_validate_arm_backend", lambda name, backend: None)
    monkeypatch.setattr(ac, "effective_contract_text", _live_text)
    monkeypatch.setattr(worker, "LINK_BUDGET_SECONDS", -1)  # every checkpoint pauses

    ids = [str(i) for i in range(20)]
    reporter = _Reporter()
    task_args = {
        "run_id": "w",
        "arms_spec": [{"source": "live", "label": "live"}],
        "item_ids": ids,
        "name": "reference set",
        "started_by": "tester",
    }
    links = 0
    while True:
        result = worker.run_ab_eval(reporter=reporter, task_args=task_args)
        links += 1
        if not result:
            break
        assert result["chain"] is True
        assert result["dispatch_deadline_seconds"] == 1800
        task_args = result["next_task_args"]
        # The next link is told only which run to continue.
        assert task_args == {
            "run_id": "w",
            "resume": True,
            "chunk_index": links,
            "started_by": "tester",
        }

    assert links == 3
    manifest = store[_run_file("w", "manifest.json")]
    assert manifest["status"] == "complete" and manifest["name"] == "reference set"
    assert len(runner.annotated) == 20
    assert reporter.progress == sorted(reporter.progress)  # never jumps backwards
    assert _run_file("w", "report.json") in store


def test_small_run_finishes_in_one_link(store, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(ab_eval, "_runner_for_arm", lambda arm, cancel_cb=None: runner)
    monkeypatch.setattr(ac, "effective_contract_text", _live_text)
    result = worker.run_ab_eval(
        reporter=_Reporter(),
        task_args={
            "run_id": "s",
            "arms_spec": [{"source": "live", "label": "live"}],
            "item_ids": [str(i) for i in range(50)],
        },
    )
    assert result is None
    assert store[_run_file("s", "manifest.json")]["status"] == "complete"


def test_test_run_task_gets_the_long_dispatch_deadline():
    """A link annotates for 15 minutes; the 600 s default would re-deliver it."""
    from web_interface.tasks import worker_registry

    assert worker_registry.deadline_for("ab_eval") == (
        worker_registry.CLOUD_TASKS_MAX_DISPATCH_DEADLINE
    )
    assert worker.LINK_BUDGET_SECONDS < worker_registry.CLOUD_TASKS_MAX_DISPATCH_DEADLINE / 1.5
