"""Stored human codings scored against another run of the same videos.

Coders code a run's videos once. ``human_eval.score_against_run`` then scores
those stored answers against a second finished run that annotated the same
videos (under another annotation version), with the calculation the task's own
results use. Cost-free and storage-free: an in-memory store, two planted runs.

The planted answers reuse the hand-checked case of ``test_human_eval.py``:
two coders who agree with each other, and a contract that agrees with coder A
on five of six videos, give alpha = 16/21 with the contract added.
"""

import copy

import pandas as pd
import pytest

from fyp.annotation import ab_eval, human_eval
from fyp.core import data_io
from tests._web import web_client

TASK_RUN = "20261001T000000Z_first0"
OTHER_RUN = "20261101T000000Z_second"
ITEMS = [f"71000000000000000{i}" for i in range(6)]
EXTRA = ["719999999999999991", "719999999999999992"]
VARIABLES = ["multilingual", "objects", "faces_age_estimate"]
CODER_A, CODER_B = "coder_a@x.com", "coder_b@x.com"

_A_ANSWERS = ["Yes", "No", "Yes", "No", "Yes", "Yes"]
# What only the OTHER run's contracts answered: must never reach a coder.
_OTHER_RUN_MARKERS = ("zeppelin", "av_other_new", "av_other_partial", "new-contract")


@pytest.fixture
def store(monkeypatch):
    files: dict = {}

    def _save(data=None, storage_location="cache", filename="", **kw):
        files[(storage_location, filename)] = copy.deepcopy(data)

    def _save_parquet(df=None, storage_location="cache", filename="", **kw):
        files[(storage_location, filename)] = df.copy()

    def _load(storage_location="cache", filename="", **kw):
        return copy.deepcopy(files.get((storage_location, filename)))

    def _load_parquet(storage_location="cache", filename="", **kw):
        return files[(storage_location, filename)].copy()

    def _exists(storage_location="cache", filename="", **kw):
        return (storage_location, filename) in files

    def _remove(storage_location="cache", filename="", **kw):
        files.pop((storage_location, filename), None)

    monkeypatch.setattr(data_io, "save_json", _save)
    monkeypatch.setattr(data_io, "load_json", _load)
    monkeypatch.setattr(data_io, "save_parquet", _save_parquet)
    monkeypatch.setattr(data_io, "load_parquet", _load_parquet)
    monkeypatch.setattr(data_io, "exists", _exists)
    monkeypatch.setattr(data_io, "remove", _remove)
    monkeypatch.setattr(ab_eval, "ensure_locations", lambda: None)
    monkeypatch.setattr(
        ab_eval,
        "resolve_items",
        lambda ids: [{"item_id": str(i), "platform": "tiktok", "downloaded": True} for i in ids],
    )
    return files


def _arm_frame(items: list[str], multilingual: list[str], objects=None) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "item_id": items,
            "multilingual": multilingual,
            "faces_age_estimate": [20.0 + 10 * i for i in range(len(items))],
            "annotated_ok": [True] * len(items),
        }
    )
    if objects is not None:
        frame["objects"] = objects
    return frame


def _plant_run(run_id: str, items: list[str], arms: dict, status: str = "complete") -> None:
    """``arms``: ``{name: (annotation_version, frame)}``."""
    manifest = {
        "run_id": run_id,
        "name": f"run {run_id[-6:]}",
        "status": status,
        "item_ids": items,
        "n_items": len(items),
        "arms": [
            {"name": name, "source": "live", "etag": name, "annotation_version": version}
            for name, (version, _) in arms.items()
        ],
    }
    columns = {
        "multilingual": {"kind": "enum"},
        "objects": {"kind": "list"},
        "faces_age_estimate": {"kind": "numeric"},
    }
    names = list(arms)
    comparisons = (
        {f"{names[0]}|{names[1]}": {"n_items": len(items), "columns": columns, "summary": {}}}
        if len(names) > 1
        else {}
    )
    data_io.save_json(
        data=manifest,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(run_id, "manifest.json"),
    )
    data_io.save_json(
        data={"run_id": run_id, "arms": names, "comparisons": comparisons},
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(run_id, "report.json"),
    )
    for name, (_, frame) in arms.items():
        data_io.save_parquet(
            df=frame,
            storage_location=ab_eval.LOCATION,
            filename=ab_eval._run_file(run_id, f"arm_{name}.parquet"),
        )


def _plant_everything(submit_b: bool = True) -> None:
    """The coded run (two old contracts), its submitted codings, and a later run.

    The later run annotated the six coded videos plus two more. Its ``new``
    contract agrees with coder A on five of six videos; ``partial`` does not
    produce ``objects`` at all.
    """
    listed = [["car"], ["cat"], ["car"], ["cat"], ["car"], ["cat"]]
    _plant_run(
        TASK_RUN,
        ITEMS,
        {
            "old": ("av_task_old", _arm_frame(ITEMS, ["No"] * 6, listed)),
            "older": ("av_task_older", _arm_frame(ITEMS, ["No"] * 6, listed)),
        },
    )
    human_eval.create_task(TASK_RUN, "coding", VARIABLES, [CODER_A, CODER_B], created_by="admin")
    for coder, items in ((CODER_A, ITEMS), (CODER_B, ITEMS[:4])):
        for i, item in enumerate(items):
            human_eval.save_response(
                TASK_RUN,
                "coding",
                coder,
                item,
                {
                    "multilingual": _A_ANSWERS[i],
                    "objects": ["car"] if i % 2 == 0 else ["cat"],
                    "faces_age_estimate": str(20 + 10 * i),
                },
            )
    human_eval.submit(TASK_RUN, "coding", CODER_A)
    if submit_b:
        human_eval.submit(TASK_RUN, "coding", CODER_B)

    later_items = ITEMS + EXTRA
    later_objects = [*listed, ["zeppelin"], ["zeppelin"]]
    _plant_run(
        OTHER_RUN,
        later_items,
        {
            "new-contract": (
                "av_other_new",
                _arm_frame(
                    later_items, ["Yes", "No", "Yes", "No", "Yes", "No", "No", "No"], later_objects
                ),
            ),
            "partial": (
                "av_other_partial",
                _arm_frame(later_items, ["Yes", "No", "Yes", "No", "Yes", "No", "No", "No"]),
            ),
        },
    )


def test_codings_are_scored_against_the_other_runs_contracts(store):
    _plant_everything()
    own = human_eval.load_results(TASK_RUN, "coding")
    result = human_eval.score_against_run(TASK_RUN, OTHER_RUN)

    assert result["task_run_id"] == TASK_RUN and result["run_id"] == OTHER_RUN
    assert result["coders"] == [CODER_A, CODER_B]
    assert result["variables"] == VARIABLES and result["n_items"] == 6
    assert result["arms"] == ["new-contract", "partial"]
    # The figures are tied to annotation versions on both sides.
    assert result["arm_versions"] == {
        "new-contract": "av_other_new",
        "partial": "av_other_partial",
    }
    assert result["task_arm_versions"] == {"old": "av_task_old", "older": "av_task_older"}

    alpha = result["alpha"]["multilingual"]
    # The coders alone: the same figure as the task's own results.
    assert alpha["humans"] == own["alpha"]["multilingual"]["humans"]
    assert alpha["humans"]["alpha"] == 1.0 and alpha["humans"]["n_units"] == 4
    # With the other run's contract added: videos 0-3 [A, B, contract] agree,
    # video 4 [Yes, Yes], video 5 [Yes, No] -> alpha = 16/21 (hand-checked in
    # test_human_eval.py). The two extra videos of the other run are not units.
    with_new = alpha["with_arm"]["new-contract"]
    assert with_new["alpha"] == pytest.approx(16 / 21)
    assert with_new["n_units"] == 6 and with_new["n_values"] == 16
    # It is the OTHER run that is scored: the task's own contracts disagree far more.
    assert set(alpha["with_arm"]) == {"new-contract", "partial"}
    assert own["alpha"]["multilingual"]["with_arm"]["old"]["alpha"] < 0.5

    # Coder answers went through the database's cleaning, like the task's own
    # results: the same check figures and "no answer" shares are reported.
    assert {"humans_check", "with_arm_check", "no_answer_share"} <= set(alpha)
    assert result["alpha"]["faces_age_estimate"]["with_arm"]["new-contract"]["alpha"] == 1.0
    assert result["alpha"]["objects"]["level"] == "set"
    assert result["alpha"]["objects"]["with_arm"]["new-contract"]["alpha"] == 1.0


def test_contract_without_a_variable_is_not_counted_as_a_coder_of_it(store):
    _plant_everything()
    result = human_eval.score_against_run(TASK_RUN, OTHER_RUN)
    assert result["missing_variables"] == {"partial": ["objects"]}
    assert result["alpha"]["objects"]["with_arm"]["partial"] is None
    # ...while the variables it does produce are scored.
    assert result["alpha"]["multilingual"]["with_arm"]["partial"]["alpha"] == pytest.approx(16 / 21)


def test_result_is_stored_with_the_task_and_removed_with_it(store):
    _plant_everything()
    own_before = human_eval.load_results(TASK_RUN, "coding")
    result = human_eval.score_against_run(TASK_RUN, OTHER_RUN)

    assert human_eval.load_cross_run_results(TASK_RUN) == {OTHER_RUN: result}
    assert human_eval.load_results(TASK_RUN, "coding") == own_before  # untouched
    written = [name for location, name in store if "other_runs" in name]
    assert written == [f"runs/{TASK_RUN}/human/results_coding_other_runs.json"]
    assert all(location == ab_eval.LOCATION for location, _ in store)

    human_eval.delete_task(TASK_RUN, "coding")
    assert human_eval.load_cross_run_results(TASK_RUN) == {}


def test_runs_that_cannot_be_scored_against_are_refused(store):
    _plant_everything(submit_b=False)
    frame = _arm_frame(ITEMS[:4], ["Yes", "No", "Yes", "No"])
    _plant_run("unfinished", ITEMS, {"x": ("av_x", _arm_frame(ITEMS, ["No"] * 6))}, "running")
    _plant_run("fewer-videos", ITEMS[:4], {"x": ("av_x", frame)})

    with pytest.raises(ValueError, match="not complete"):
        human_eval.score_against_run(TASK_RUN, "unfinished")
    with pytest.raises(ValueError, match="lacks 2 of the 6 coded videos"):
        human_eval.score_against_run(TASK_RUN, "fewer-videos")
    with pytest.raises(ValueError, match="not found"):
        human_eval.score_against_run(TASK_RUN, "no-such-run")
    with pytest.raises(ValueError, match="different run"):
        human_eval.score_against_run(TASK_RUN, TASK_RUN)
    with pytest.raises(ValueError, match="no coding task"):
        human_eval.score_against_run(OTHER_RUN, TASK_RUN)

    # One submitted coder is enough: no "coders alone" figure, but the
    # contract is still scored against that coder.
    result = human_eval.score_against_run(TASK_RUN, OTHER_RUN)
    assert result["coders"] == [CODER_A]
    assert result["alpha"]["multilingual"]["humans"] is None
    assert result["alpha"]["multilingual"]["with_arm"]["new-contract"]["n_units"] == 6


def test_nobody_submitted_is_refused(store):
    _plant_run(TASK_RUN, ITEMS, {"old": ("av_task_old", _arm_frame(ITEMS, ["No"] * 6))})
    _plant_run(OTHER_RUN, ITEMS, {"new": ("av_other_new", _arm_frame(ITEMS, ["No"] * 6))})
    human_eval.create_task(TASK_RUN, "coding", ["multilingual"], [CODER_A], created_by="admin")
    with pytest.raises(ValueError, match="no coder has submitted"):
        human_eval.score_against_run(TASK_RUN, OTHER_RUN)


def test_scoring_shows_coders_nothing_from_either_run(store, monkeypatch):
    """Scoring changes no coder-facing record, and the coder endpoints stay blind."""
    _plant_everything()

    def _coder_facing() -> dict:
        return {
            key: copy.deepcopy(value)
            for key, value in store.items()
            if "/human/task_" in key[1] or "/human/coding_" in key[1]
        }

    before = _coder_facing()
    human_eval.score_against_run(TASK_RUN, OTHER_RUN)
    assert _coder_facing() == before

    users = {CODER_A: "viewer", "admin@x.com": "admin"}
    with web_client(monkeypatch, users, login_as=CODER_A) as client:
        payload = client.get(f"/api/human-eval/tasks/{TASK_RUN}/coding")
        assert payload.status_code == 200
        body = payload.get_json()
        assert set(body) == {
            "run_id",
            "task_type",
            "items",
            "variables",
            "field_specs",
            "status",
            "responses",
        }
        text = payload.get_data(as_text=True)
        tasks_text = client.get("/api/human-eval/my-tasks").get_data(as_text=True)
        for marker in (*_OTHER_RUN_MARKERS, "av_task_old", "alpha", OTHER_RUN):
            assert marker not in text and marker not in tasks_text, marker

        # The scoring endpoint and the stored result are for admins only.
        url = f"/api/manage/human-eval/tasks/{TASK_RUN}/coding/score-against/{OTHER_RUN}"
        assert client.post(url).status_code in (302, 401, 403)
        assert client.get(f"/api/manage/human-eval/tasks/{TASK_RUN}/coding").status_code in (
            302,
            401,
            403,
        )

    with web_client(monkeypatch, users, login_as="admin@x.com") as client:
        response = client.post(url)
        assert response.status_code == 200
        scored = response.get_json()["result"]
        assert scored["arm_versions"]["new-contract"] == "av_other_new"
        assert scored["alpha"]["multilingual"]["with_arm"]["new-contract"]["alpha"] == (
            pytest.approx(16 / 21)
        )
        refused = client.post(
            f"/api/manage/human-eval/tasks/{TASK_RUN}/coding/score-against/no-such-run"
        )
        assert refused.status_code == 400 and "not found" in refused.get_json()["error"]
        task = client.get(f"/api/manage/human-eval/tasks/{TASK_RUN}/coding").get_json()
        assert OTHER_RUN in task["other_runs"]
