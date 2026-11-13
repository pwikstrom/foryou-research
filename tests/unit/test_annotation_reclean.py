"""Re-cleaning stored annotation answers (``fyp.annotation.annotation_reclean``).

Everything runs on throwaway temp storage. The raw answers are a handful from
the golden fixture, including three whose model answer names the content
category "Technology, Design & Reviews" and several with numeric video ids
(as raw files written before April 2026 have). The "stored" refined rows are
today's refinement of those answers with an old cleaning error put back in:
the technology category collapsed to "other category".

* A dry run reports what would change, for every variable, and writes nothing.
* A real run replaces the named variables only, keeps a copy of the original
  file, and never touches a row's annotation version, annotation time or
  platform, nor any variable it was not given.
* The next consolidation rebuilds the dataset, reports exactly the changed
  videos, and clears the marker.
* Files that cannot be re-cleaned safely are left alone and named.
"""

import copy
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import pytest

import fyp.core.data_io as data_io
from fyp.annotation import annotation_reclean as reclean
from fyp.annotation import annotation_refinement
from fyp.core.fyp_config import fyp_cf
from tests._web import web_client
from web_interface.workers import run_annotation_reclean as worker

FIXTURE = Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "raw_sample.json"
RAW_FN = "machine_annotations_20251220000000000001.json"
PARQUET_FN = RAW_FN.replace(".json", ".parquet")
TECH = "technology  and  design  and  reviews"
OTHER = "other category"


def _raw_answers() -> dict:
    """Three 'technology' answers, five with numeric ids, four with text ids."""
    raw = json.loads(FIXTURE.read_text())
    is_tech = {
        key
        for key, entry in raw.items()
        if re.search(r'content_category"\s*:\s*\[[^\]]*echnolog', str(entry.get("response")))
    }
    numeric = [k for k, e in raw.items() if isinstance(e["item_id"], int) and k not in is_tech][:5]
    text = [k for k, e in raw.items() if isinstance(e["item_id"], str) and k not in is_tech][:4]
    picked = [*sorted(is_tech), *numeric, *text]
    return {str(i): copy.deepcopy(raw[key]) for i, key in enumerate(picked)}


@pytest.fixture(scope="module")
def cleaned_today() -> tuple[dict, pd.DataFrame]:
    """The raw answers and what today's refinement makes of them (computed once)."""
    raw = _raw_answers()
    frame = annotation_refinement.refine_one_raw_annotation_batch(
        raw_outputs_from_machine=copy.deepcopy(raw), raw_json_filename=RAW_FN, save=False
    )
    return raw, frame


def _with_old_error(frame: pd.DataFrame) -> pd.DataFrame:
    """Today's rows with the technology category collapsed, as the old cleaning did."""
    stored = frame.copy()
    stored["content_category"] = stored["content_category"].map(
        lambda labels: (
            [OTHER if label == TECH else label for label in labels]
            if isinstance(labels, (list, tuple)) or hasattr(labels, "tolist")
            else labels
        )
    )
    return stored


@pytest.fixture
def storage(cleaned_today):
    """Temp storage holding one raw file and its stored (old-error) refined rows."""
    keys = [
        "local_data",
        "machine_annotations",
        "machine_annotations_raw",
        "machine_annotations_refined",
        "recoded",
        "temp",
    ]
    original = {k: fyp_cf["paths"].get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="fyp_reclean_")
    try:
        fyp_cf["paths"]["local_data"] = tmp
        fyp_cf["paths"]["machine_annotations"] = os.path.join(tmp, "machine_annotations")
        for k in ("machine_annotations_raw", "machine_annotations_refined"):
            fyp_cf["paths"][k] = os.path.join(tmp, "machine_annotations", k)
        for k in ("recoded", "temp"):
            fyp_cf["paths"][k] = os.path.join(tmp, k)
        for k in keys:
            os.makedirs(fyp_cf["paths"][k], exist_ok=True)
        raw, frame = cleaned_today
        _plant(RAW_FN, raw, _with_old_error(frame))
        yield tmp
    finally:
        for k, v in original.items():
            fyp_cf["paths"][k] = v
        fyp_cf["paths"].pop(reclean.BACKUP_LOCATION, None)
        shutil.rmtree(tmp, ignore_errors=True)


def _plant(raw_fn: str, raw: dict, stored: pd.DataFrame) -> None:
    data_io.save_json(data=raw, storage_location="machine_annotations_raw", filename=raw_fn)
    data_io.save_parquet(
        df=stored,
        storage_location="machine_annotations_refined",
        filename=raw_fn.replace(".json", ".parquet"),
    )


def _stored(fn: str = PARQUET_FN) -> pd.DataFrame:
    frame = data_io.load_parquet(storage_location="machine_annotations_refined", filename=fn)
    return frame.assign(item_id=frame["item_id"].astype(str)).set_index("item_id")


def _first_labels(frame: pd.DataFrame) -> list[str]:
    return sorted(reclean._first_label(labels) for labels in frame["content_category"])


def _tree(root: str) -> dict:
    """Every file under ``root`` with its size and modification time."""
    out = {}
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            out[os.path.relpath(path, root)] = (os.path.getsize(path), os.path.getmtime(path))
    return out


REPLACE = ["content_category"]


def _run(dry_run: bool, variables=None) -> dict:
    if variables is None and not dry_run:
        variables = REPLACE
    return reclean.continue_run(
        reclean.start_run(dry_run=dry_run, started_by="tester", variables=variables)
    )


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_changes_and_writes_nothing(storage):
    before = _tree(storage)
    state = _run(dry_run=True)
    report = reclean.load_report()

    assert state["status"] == "complete" and report["dry_run"] is True
    assert report["files"] == 1 and report["files_changed"] == 1 and report["files_blocked"] == {}
    assert report["rows"] == 12 and report["rows_changed"] == 3 == report["n_videos_changed"]
    # Per variable: only the content category differs.
    assert report["values_changed"] == {"content_category": 3}
    assert report["rows_cleaning_fails_on"] == 0
    assert report["columns_only_stored"] == {} and report["columns_only_new"] == {}
    # Old first label -> new first label. One of the three technology answers
    # names it first; the other two keep their first label and change the second.
    assert report["crosstab"][OTHER] == {TECH: 1}
    assert sum(n for row in report["crosstab"].values() for n in row.values()) == 12

    after = _tree(storage)
    written = sorted(set(after) - set(before))
    assert written == [
        f"recoded/{reclean.REPORT_FILENAME}",
        f"recoded/{reclean.STATE_FILENAME}",
    ]
    assert {k: v for k, v in after.items() if k in before} == before  # nothing touched
    assert reclean.load_pending() is None
    assert TECH not in str(_stored()["content_category"].tolist())


# ---------------------------------------------------------------------------
# Real run
# ---------------------------------------------------------------------------


def test_real_run_replaces_only_the_named_variable(storage, cleaned_today):
    raw, frame = cleaned_today
    # Stored rows differ from today's cleaning in more than the old error: a
    # version, times and a platform the raw file would not give, another
    # variable with different values, and a variable today's cleaning no
    # longer produces. A re-clean of content_category must leave all of it be.
    stored = _with_old_error(frame)
    stored["annotation_version"] = ["v0_legacy", "av_kept_as_stored"] * (len(stored) // 2)
    stored["inference_ts"] = pd.array(range(5000, 5000 + len(stored)), dtype="int64[pyarrow]")
    stored["source_platform"] = "youtube"
    stored["video_story"] = "story as stored"
    stored["retired_variable"] = "only in the stored rows"
    _plant(RAW_FN, raw, stored)
    expected = stored.assign(item_id=stored["item_id"].astype(str)).set_index("item_id")
    today = frame.assign(item_id=frame["item_id"].astype(str)).set_index("item_id")

    dry = _run(dry_run=True)["report"]
    # The dry run shows every variable. One of the twelve answers is one the
    # cleaning cannot read (a failed annotation): its row is never compared.
    assert dry["values_changed"]["video_story"] == 11
    assert dry["columns_only_stored"] == {"retired_variable": 1}
    assert dry["rows_changed"] == 11  # no variable named: any difference counts
    assert _run(dry_run=True, variables=REPLACE)["report"]["rows_changed"] == 3

    state = _run(dry_run=False)
    after = _stored()

    assert state["status"] == "complete" and state["report"]["files_blocked"] == {}
    assert list(after.index) == list(expected.index)  # same videos, same order
    assert sorted(after.columns) == sorted(expected.columns)
    for column in sorted(set(expected.columns) - {"content_category"}):
        assert after[column].map(reclean._cell).tolist() == (
            expected[column].map(reclean._cell).tolist()
        ), column
    assert after["content_category"].map(reclean._cell).tolist() == (
        today["content_category"].map(reclean._cell).tolist()
    )
    assert _first_labels(after).count(TECH) == 1 and OTHER not in str(_first_labels(after))

    # The original is kept, and the marker names exactly the changed videos.
    backup = data_io.load_parquet(storage_location=reclean.BACKUP_LOCATION, filename=PARQUET_FN)
    assert TECH not in str(backup["content_category"].tolist()) and len(backup) == 12
    pending = reclean.load_pending()
    tech_ids = {
        i for i, labels in today["content_category"].items() if TECH in reclean._cell(labels)
    }
    assert set(pending["changed_item_ids"]) == tech_ids and len(tech_ids) == 3
    assert pending["variables"] == REPLACE

    # Until a consolidation has taken the changes in, another real run is refused...
    with pytest.raises(ValueError, match="consolidate first"):
        reclean.start_run(dry_run=False, variables=REPLACE)
    # ...and a dry run now finds nothing left to change in that variable.
    assert _run(dry_run=True, variables=REPLACE)["report"]["rows_changed"] == 0
    assert reclean.load_pending() is not None  # a dry run leaves the marker alone


def test_real_run_must_name_its_variables(storage):
    with pytest.raises(ValueError, match="must name the variables"):
        reclean.start_run(dry_run=False)
    with pytest.raises(ValueError, match="must name the variables"):
        reclean.start_run(dry_run=False, variables=[" "])
    for protected in ("inference_ts", "annotation_version", "item_id", "annotated_ok"):
        with pytest.raises(ValueError, match="never replaced"):
            reclean.start_run(dry_run=False, variables=["content_category", protected])
    # A variable today's cleaning does not produce: the file is left alone.
    state = _run(dry_run=False, variables=["no_such_variable"])
    assert "does not produce: no_such_variable" in state["report"]["files_blocked"][RAW_FN]
    assert reclean.load_pending() is None


def test_second_real_run_changes_nothing_and_keeps_the_first_backup(storage):
    _run(dry_run=False)
    reclean.clear_pending()
    backup_before = _tree(os.path.join(storage, "machine_annotations", reclean.BACKUP_LOCATION))
    refined_before = _tree(
        os.path.join(storage, "machine_annotations", "machine_annotations_refined")
    )

    state = _run(dry_run=False)

    assert state["report"]["rows_changed"] == 0 and state["changed_item_ids"] == []
    assert reclean.load_pending() is None  # nothing changed, nothing to consolidate
    assert _tree(os.path.join(storage, "machine_annotations", reclean.BACKUP_LOCATION)) == (
        backup_before
    )
    assert (
        _tree(os.path.join(storage, "machine_annotations", "machine_annotations_refined"))
        == refined_before
    )


def test_next_consolidation_takes_the_recleaned_rows_in(storage):
    """Through the normal path: full rebuild once, the changed videos reported."""
    ok, view, _ = annotation_refinement.consolidate_and_save_refined_annotations(incremental=True)
    assert ok and TECH not in str(view["content_category"].tolist())
    stamps_before = dict(zip(view["item_id"].astype(str), view["inference_ts"]))

    _run(dry_run=False)
    changed = set(reclean.load_pending()["changed_item_ids"])

    folds = []
    original_fold = annotation_refinement._fold_annotation_batch
    annotation_refinement._fold_annotation_batch = lambda **kw: (
        folds.append(1) or original_fold(**kw)
    )
    try:
        ok, view, ids = annotation_refinement.consolidate_and_save_refined_annotations(
            incremental=True, return_saved_data=False
        )
    finally:
        annotation_refinement._fold_annotation_batch = original_fold

    assert ok and not folds, "a re-clean is taken in by a full rebuild, not the fold"
    assert ids == changed and len(ids) == 3
    assert TECH in str(view["content_category"].tolist())
    assert dict(zip(view["item_id"].astype(str), view["inference_ts"])) == stamps_before
    assert set(view["annotation_version"]) == {"v0_legacy"}
    archive = data_io.load_parquet(
        storage_location="recoded",
        filename=f"{reclean._prefix()}_all_versions.parquet",
    )
    assert len(archive) == 12 and TECH in str(archive["content_category"].tolist())
    assert reclean.load_pending() is None

    ok, _, ids = annotation_refinement.consolidate_and_save_refined_annotations(
        incremental=True, return_saved_data=False
    )
    assert not ok and ids == set()  # quiet again


# ---------------------------------------------------------------------------
# Files that are left alone
# ---------------------------------------------------------------------------


def test_file_is_left_alone_when_videos_would_change(storage, cleaned_today):
    raw, frame = cleaned_today
    _plant(RAW_FN, raw, _with_old_error(frame).iloc[:-2])  # stored rows lack two videos

    state = _run(dry_run=False)

    reason = state["report"]["files_blocked"][RAW_FN]
    assert "0 only stored, 2 only in the raw file" in reason
    assert len(_stored()) == 10 and TECH not in str(_stored()["content_category"].tolist())
    assert reclean.load_pending() is None
    assert not os.path.isdir(os.path.join(storage, "machine_annotations", reclean.BACKUP_LOCATION))


def test_row_todays_cleaning_cannot_read_keeps_its_stored_values(storage, cleaned_today):
    raw, frame = cleaned_today
    today = frame.assign(item_id=frame["item_id"].astype(str)).set_index("item_id")
    tech_ids = [
        i for i, labels in today["content_category"].items() if TECH in reclean._cell(labels)
    ]
    broken = copy.deepcopy(raw)
    key = next(k for k, entry in broken.items() if str(entry["item_id"]) == tech_ids[0])
    broken[key]["response"] = ""  # today's refinement can make nothing of this answer
    _plant(RAW_FN, broken, _with_old_error(frame))  # ...but the stored row is a good one

    state = _run(dry_run=False)
    report, after = state["report"], _stored()

    assert report["rows_cleaning_fails_on"] == 1 and report["files_blocked"] == {}
    assert report["rows_changed"] == 2 and sorted(state["changed_item_ids"]) == sorted(tech_ids[1:])
    # The unreadable row is exactly as stored: still a successful annotation,
    # still carrying the old label. The other two are repaired.
    assert bool(after.loc[tech_ids[0], "annotated_ok"])
    assert OTHER in reclean._cell(after.loc[tech_ids[0], "content_category"])
    for item_id in tech_ids[1:]:
        assert TECH in reclean._cell(after.loc[item_id, "content_category"])


def test_candidates_are_legacy_files_that_still_have_their_raw_answers(storage, cleaned_today):
    raw, frame = cleaned_today
    versioned = "machine_annotations_20260801000000000002.json"
    _plant(versioned, raw, frame.assign(annotation_version="av_current"))
    orphan = "machine_annotations_json_is_missing.parquet"
    data_io.save_parquet(df=frame, storage_location="machine_annotations_refined", filename=orphan)

    assert reclean.candidate_files(only_legacy=True) == [RAW_FN]
    assert reclean.candidate_files(only_legacy=False) == [RAW_FN, versioned]
    assert reclean.files_without_raw() == [orphan]
    assert _run(dry_run=True)["files_without_raw"] == [orphan]
    assert reclean.load_report()["files_without_raw"] == [orphan]


# ---------------------------------------------------------------------------
# Long runs: pause, continue, cancel, crash
# ---------------------------------------------------------------------------


def _two_more_files(cleaned_today) -> list[str]:
    raw, frame = cleaned_today
    names = [f"machine_annotations_2025122{i}000000000001.json" for i in (1, 2)]
    for name in names:
        _plant(name, raw, _with_old_error(frame))
    return [RAW_FN, *names]


def test_run_pauses_after_a_file_and_continues_where_it_stopped(storage, cleaned_today):
    files = _two_more_files(cleaned_today)
    state = reclean.start_run(dry_run=True)
    assert state["files"] == files

    state = reclean.continue_run(state, should_pause=lambda: True)
    assert state["status"] == "running" and state["next_index"] == 1
    assert reclean.load_report() is None

    seen = []
    state = reclean.continue_run(
        reclean.load_state(), progress_cb=lambda done, total, fn: seen.append((done, total, fn))
    )
    assert state["status"] == "complete" and seen == [(2, 3, files[1]), (3, 3, files[2])]
    assert reclean.load_report()["files"] == 3 and reclean.load_report()["rows_changed"] == 9


def test_real_run_that_died_hands_its_changed_videos_to_the_next_run(storage, cleaned_today):
    _two_more_files(cleaned_today)
    state = reclean.continue_run(
        reclean.start_run(dry_run=False, variables=REPLACE), should_pause=lambda: True
    )
    done_before_it_died = set(state["changed_item_ids"])
    assert len(done_before_it_died) == 3 and reclean.load_pending() is None

    # Started again from the top: the first file shows nothing left to change,
    # yet its videos must still reach the consolidation.
    state = reclean.continue_run(reclean.start_run(dry_run=False, variables=REPLACE))
    assert state["status"] == "complete"
    assert set(reclean.load_pending()["changed_item_ids"]) == done_before_it_died


def test_worker_defaults_to_a_dry_run_chains_and_cancels(storage, cleaned_today, monkeypatch):
    _two_more_files(cleaned_today)

    class Reporter:
        def __init__(self):
            self.cancelled = False
            self.logs = []
            self.progress = []

        def update_progress(self, pct, message=""):
            self.progress.append(pct)

        def log(self, message):
            self.logs.append(message)

        def emit_data(self, data):
            pass

        def check_cancelled(self):
            return self.cancelled

    monkeypatch.setattr(worker, "LINK_BUDGET_SECONDS", -1)  # one file per link
    reporter, task_args, links = Reporter(), {}, 0
    while True:
        result = worker.run_annotation_reclean(reporter=reporter, task_args=task_args)
        links += 1
        if not result:
            break
        assert result["chain"] and result["dispatch_deadline_seconds"] == 1800
        task_args = result["next_task_args"]
        assert task_args["chunk_index"] == links
    assert links == 3 and reporter.progress == sorted(reporter.progress)
    assert reclean.load_report()["dry_run"] is True and reclean.load_pending() is None
    assert TECH not in str(_stored()["content_category"].tolist())
    assert any("Nothing was written" in line for line in reporter.logs)

    # A real run cancelled part-way still reports the files it replaced.
    reporter = Reporter()
    first = worker.run_annotation_reclean(
        reporter=reporter, task_args={"dry_run": False, "variables": "content_category"}
    )
    assert first["chain"]
    reporter.cancelled = True
    assert (
        worker.run_annotation_reclean(reporter=reporter, task_args=first["next_task_args"]) is None
    )
    state = reclean.load_state()
    assert state["status"] == "cancelled" and state["next_index"] == 2
    assert len(reclean.load_pending()["changed_item_ids"]) == 3
    assert reclean.load_report()["status"] == "cancelled"


def test_local_command_line_applies_only_when_told_to():
    from web_interface.tasks.process_manager import _task_args_to_cli

    assert _task_args_to_cli("annotation_reclean", {"started_by": "a"}) == ["--started-by", "a"]
    assert _task_args_to_cli("annotation_reclean", {"dry_run": True}) == []
    assert _task_args_to_cli("annotation_reclean", {"dry_run": None}) == []
    assert _task_args_to_cli(
        "annotation_reclean",
        {"dry_run": False, "only_legacy": False, "variables": ["content_category", "objects"]},
    ) == ["--apply", "--all-files", "--variables", "content_category,objects"]


def test_endpoint_starts_a_dry_run_unless_a_real_run_is_confirmed(storage, monkeypatch):
    from web_interface.routes.management import enrichment as routes

    started = []
    monkeypatch.setattr(
        routes, "start_process", lambda name, module, **kw: started.append(kw) or (True, "ok")
    )
    monkeypatch.setattr(routes, "is_worker_running", lambda name: False)
    monkeypatch.setattr(routes, "workers_blocking_consolidate", list)
    url = "/api/manage/annotation/reclean"
    with web_client(monkeypatch, {"admin@x.com": "admin", "viewer@x.com": "viewer"}) as client:
        from tests._web import login

        login(client, "viewer@x.com")
        assert client.post(url, json={}).status_code in (302, 401, 403)

        login(client, "admin@x.com")
        assert client.post(url, json={}).get_json()["dry_run"] is True
        assert started[-1]["task_args"]["dry_run"] is True

        refused = client.post(url, json={"dry_run": False})
        assert refused.status_code == 400 and len(started) == 1

        monkeypatch.setattr(routes, "workers_blocking_consolidate", lambda: ["queue_annotator"])
        confirmed = {"dry_run": False, "confirm": "replace stored annotations"}
        assert client.post(url, json=confirmed).status_code == 400  # no variables named
        assert client.post(url, json={**confirmed, "variables": ["nope"]}).status_code == 400
        confirmed["variables"] = ["content_category"]
        assert client.post(url, json=confirmed).status_code == 409
        monkeypatch.setattr(routes, "workers_blocking_consolidate", list)
        assert client.post(url, json=confirmed).get_json()["dry_run"] is False
        assert started[-1]["task_args"]["dry_run"] is False
        assert started[-1]["task_args"]["variables"] == ["content_category"]

        status = client.get(url).get_json()
        assert status["consolidation_owed"] is False and status["report"] is None
