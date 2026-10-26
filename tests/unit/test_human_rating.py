"""Tests for quality-rating tasks (``fyp.annotation.human_rating``).

Storage-free like ``test_human_eval``: data_io is patched onto an in-memory
store and a finished single-contract run is planted. The tests run in file
order as one scenario: a coding task on a random subset supplies human
answers, then a rating draft gets traps, starts, is rated, and is scored.

Run:
    python -m pytest tests/unit/test_human_rating.py
"""

import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd
import pytest

from fyp.annotation import ab_eval, human_eval, human_rating
from fyp.core import data_io
from tests.unit import test_human_eval as base

RUN_ID = "20261026T000000Z_rate01"
ITEMS = [f"71000000000000000{i:02d}" for i in range(12)]
VARIABLES = ["video_story", "main_activity", "objects", "political_score", "main_gender"]
CODERS = ["coder_a@x.com", "coder_b@x.com"]
RATERS = ["r1@x.com", "r2@x.com", "r3@x.com", "coder_a@x.com"]


def _check(name: str, ok: bool, detail: str = ""):
    assert ok, f"{name}  {detail}"
    print(f"  PASS  {name}")


@pytest.fixture(scope="module", autouse=True)
def _planted_run():
    originals = base._patch_data_io()
    manifest = {
        "run_id": RUN_ID,
        "status": "complete",
        "item_ids": ITEMS,
        "n_items": len(ITEMS),
        "arms": [{"name": "live", "source": "live", "etag": "x"}],
    }
    report = {"run_id": RUN_ID, "arms": ["live"], "comparisons": {}, "distributions": {}}
    data_io.save_json(data=manifest, storage_location=ab_eval.LOCATION, filename=ab_eval._run_file(RUN_ID, "manifest.json"))  # fmt: skip
    data_io.save_json(data=report, storage_location=ab_eval.LOCATION, filename=ab_eval._run_file(RUN_ID, "report.json"))  # fmt: skip
    arm = pd.DataFrame(
        {
            "item_id": ITEMS,
            "video_story": [f"A person does thing number {i} in a kitchen." for i in range(12)],
            "main_activity": [f"cooking dish {i}" for i in range(12)],
            "objects": [np.array(["pan", f"knife{i}"]) for i in range(12)],
            "political_score": [0.1 * (i % 5) for i in range(12)],
            "main_gender": ["female" if i % 2 else "male" for i in range(12)],
            "annotated_ok": [True] * 12,
        }
    )
    data_io.save_parquet(df=arm, storage_location=ab_eval.LOCATION, filename=ab_eval._run_file(RUN_ID, "arm_live.parquet"))  # fmt: skip
    try:
        yield
    finally:
        for (mod, attr), fn in originals.items():
            setattr(mod, attr, fn)
        base._STORE.clear()


STATE: dict = {}


def test_coding_subset_supplies_human_answers():
    print("coding task on a random subset")
    task = human_eval.create_task(RUN_ID, "coding", VARIABLES, CODERS, "admin", n_videos=4)
    _check("random subset of 4 videos", len(task["item_ids"]) == 4, str(task["item_ids"]))
    for coder in CODERS:
        for item in task["item_ids"]:
            human_eval.save_response(
                RUN_ID,
                "coding",
                coder,
                item,
                {
                    "video_story": f"Someone cooks in video {item[-2:]}.",
                    "main_activity": "cooking",
                    "objects": ["pan"],
                    "political_score": "20",
                    "main_gender": "Female",
                },
            )
        human_eval.submit(RUN_ID, "coding", coder)
    STATE["coded"] = task["item_ids"]


def test_draft_and_traps():
    print("draft, traps")
    task = human_rating.create_task(
        RUN_ID,
        VARIABLES,
        RATERS,
        "admin",
        coders_included=CODERS,
        n_practice=2,
        ratings_per_human=2,
        swapped_per_rater=1,
        exclusion_rule="missed_swap",
    )
    _check("created as a draft", task["status"] == "draft")
    _check(
        "coded videos kept in the rated sample", set(STATE["coded"]) <= set(task["rated_item_ids"])
    )
    _check(
        "practice videos outside the rated sample",
        len(task["practice_item_ids"]) == 2
        and not set(task["practice_item_ids"]) & set(task["rated_item_ids"]),
    )
    _check("rate kinds by type", {v: s["rate_kind"] for v, s in task["field_specs"].items()} == {
        "video_story": "text", "main_activity": "phrase", "objects": "list",
        "political_score": "number", "main_gender": "category",
    }, str({v: s["rate_kind"] for v, s in task["field_specs"].items()}))  # fmt: skip
    _check("a draft is nobody's task", not human_eval.tasks_for_user("r1@x.com"))
    _check("a draft is closed to raters", not human_eval.is_invited(task, "r1@x.com"))

    target = next(i for i in task["rated_item_ids"] if i not in STATE["coded"])
    shown = human_rating.model_answers_for(RUN_ID, target)
    _check("scores shown on the codebook scale", shown["political_score"] in {0.0, 10.0, 20.0, 30.0, 40.0}, str(shown))  # fmt: skip
    _check("lists shown as entries", shown["objects"][0] == "pan", str(shown["objects"]))
    trap = human_rating.add_trap(RUN_ID, target, "video_story", "A dog does thing in a garage.", "person swapped")  # fmt: skip
    again = human_rating.add_trap(RUN_ID, target, "video_story", "A cat does a thing in a kitchen.", "animal")  # fmt: skip
    _check("one trap per video and variable", again["trap_id"] == trap["trap_id"])
    list_trap = human_rating.add_trap(
        RUN_ID, target, "objects", ["pan", "chainsaw"], "added chainsaw"
    )
    try:
        human_rating.add_trap(RUN_ID, task["practice_item_ids"][0], "video_story", "x")
        _check("trap on a practice video refused", False)
    except ValueError:
        _check("trap on a practice video refused", True)
    _check("trap deleted", human_rating.delete_trap(RUN_ID, list_trap["trap_id"]))
    human_rating.add_trap(RUN_ID, target, "objects", ["pan", "chainsaw"], "added chainsaw")
    STATE["trap_item"] = target


def test_start_plan():
    print("start and plan")
    summary = human_rating.start(RUN_ID, "admin")
    task = human_eval.load_task(RUN_ID, "rating")
    STATE["task"] = task
    answers, assignment = task["answers"], task["assignment"]
    rated = task["rated_item_ids"]
    _check("started", task["status"] == "started" and set(summary["per_rater"]) == set(RATERS))
    _check(
        "every rater rates every video once per variable",
        all(set(assignment[r].get(i, {})) == set(VARIABLES) for r in RATERS for i in rated),
    )
    own = [
        (r, a)
        for r in RATERS
        for by_var in assignment[r].values()
        for a in by_var.values()
        if answers[a]["source"] == f"human:{r}"
    ]
    _check("nobody rates their own coding", not own, str(own))
    traps = [a for a, meta in answers.items() if meta["source"].startswith("trap:")]
    trap_raters = [sum(a in by_var.values() for r in RATERS for by_var in assignment[r].values()) for a in traps]  # fmt: skip
    _check("each hand trap goes to exactly one rater", len(traps) == 2 and trap_raters == [1, 1], str(trap_raters))  # fmt: skip
    model_ratings = {
        (meta["item_id"], meta["variable"]): sum(a in by_var.values() for r in RATERS for by_var in assignment[r].values())
        for a, meta in answers.items()
        if meta["source"] == "model" and not meta.get("practice")
    }  # fmt: skip
    _check("every model answer is rated", len(model_ratings) == len(rated) * len(VARIABLES) and min(model_ratings.values()) >= 1)  # fmt: skip
    human_counts = [
        sum(a in by_var.values() for r in RATERS for by_var in assignment[r].values())
        for a, meta in answers.items()
        if meta["source"].startswith("human:")
    ]
    _check("human answers rated by 1-2 raters", human_counts and set(human_counts) <= {1, 2}, str(Counter_(human_counts)))  # fmt: skip
    swaps = [meta for meta in answers.values() if meta["source"].startswith("swap:")]
    _check("a swapped trap per rater and variable", len(swaps) == len(RATERS) * len(VARIABLES), str(len(swaps)))  # fmt: skip
    _check("swaps come from another video", all(s["swapped_from"] != s["item_id"] for s in swaps))
    practice = task["practice_item_ids"]
    _check("practice first in every order", all(task["order"][r][:2] == practice for r in RATERS))
    _check("started task is the raters' task", human_eval.tasks_for_user("r1@x.com"))

    payload = human_rating.rater_payload(task, "r1@x.com", human_eval.load_coder_state(RUN_ID, "rating", "r1@x.com"))  # fmt: skip
    blob = json.dumps(payload)
    _check("payload carries no source or note", "human:" not in blob and "swap" not in blob and "person swapped" not in blob and "model" not in json.dumps(payload["items"]))  # fmt: skip
    _check("payload has a guess sample of free text", payload["guesses"] and all(g["variable"] in ("video_story", "main_activity") for g in payload["guesses"]))  # fmt: skip
    for rater, sample in task["guess_sample"].items():
        groups = [human_rating.source_group(answers[a]["source"]) for a in sample]
        _check(f"guess sample balanced for {rater}", groups.count("human") == groups.count("model") and "swap" not in groups and "trap" not in groups, str(groups))  # fmt: skip
    try:
        human_eval.add_coders(RUN_ID, "rating", ["late@x.com"])
        _check("raters fixed after start", False)
    except ValueError:
        _check("raters fixed after start", True)


def Counter_(values):
    from collections import Counter

    return dict(Counter(values))


def _rating_for(task, rater, item_id):
    """What our simulated raters say: the model is right, humans have slips,
    r3 waves every trap through."""
    ratings = {}
    for variable, answer_id in task["assignment"][rater][item_id].items():
        meta = task["answers"][answer_id]
        group = human_rating.source_group(meta["source"])
        kind = task["field_specs"][variable]["rate_kind"]
        trapped = group in ("swap", "trap") and rater != "r3@x.com"
        if kind == "text":
            ratings[variable] = (
                {"accuracy": "major_error", "main_point": "no", "overall": "1"}
                if trapped
                else {"accuracy": "minor_error" if group == "human" else "no_errors", "main_point": "yes", "overall": "4" if group == "human" else "5"}
            )  # fmt: skip
        elif kind == "phrase":
            ratings[variable] = {"fit": "no" if trapped else "yes"}
        elif kind == "category":
            ratings[variable] = {"verdict": "wrong" if trapped else "correct"}
        elif kind == "number":
            ratings[variable] = {"closeness": "far_off" if trapped else "about_right"}
        else:
            marks = dict.fromkeys(meta["value"], "not_there" if trapped else "there")
            ratings[variable] = {"items": marks, "missing": "none"}
    return {"cant_rate": False, "ratings": ratings}


def test_validation():
    print("rating validation")
    task = STATE["task"]
    item = task["order"]["r1@x.com"][2]
    try:
        human_eval.save_response(RUN_ID, "rating", "r1@x.com", item, {"ratings": {"video_story": {"accuracy": "great"}}})  # fmt: skip
        _check("invalid option refused", False)
    except ValueError:
        _check("invalid option refused", True)
    try:
        human_eval.save_response(RUN_ID, "rating", "r1@x.com", item, {"ratings": {"objects": {"items": {"spoon": "there"}}}})  # fmt: skip
        _check("list entry not shown refused", False)
    except ValueError:
        _check("list entry not shown refused", True)
    state = human_eval.save_response(RUN_ID, "rating", "r1@x.com", item, {"cant_rate": True, "cant_rate_reason": "wont_play"})  # fmt: skip
    _check("can't rate saved", state["responses"][item]["values"] == {"cant_rate": True, "cant_rate_reason": "wont_play", "ratings": {}})  # fmt: skip


def test_rate_submit_and_results():
    print("rating, guesses, results")
    task = STATE["task"]
    for rater in RATERS:
        for item_id in task["order"][rater]:
            human_eval.save_response(
                RUN_ID, "rating", rater, item_id, _rating_for(task, rater, item_id)
            )
        guesses = {
            g: (
                "person"
                if human_rating.source_group(task["answers"][g]["source"]) == "human"
                else "model"
            )
            for g in task["guess_sample"][rater]
        }
        human_rating.save_guesses(RUN_ID, rater, guesses)
        human_eval.submit(RUN_ID, "rating", rater)
    results = human_eval.load_results(RUN_ID, "rating")
    _check("rater who missed swaps excluded", results["excluded_raters"] == ["r3@x.com"], str(results["excluded_raters"]))  # fmt: skip
    r1 = results["raters"]["r1@x.com"]
    _check(
        "swaps caught by attentive rater",
        r1["swaps"] == r1["swaps_caught"] == len(VARIABLES),
        str(r1),
    )
    story = results["variables"]["video_story"]
    model = story["by_source"]["model"]["headline"]
    human = story["by_source"]["human"]["headline"]
    _check("model stories judged error-free", model["share_no_errors"]["value"] == 1.0)
    _check("human stories had minor slips", human["share_no_errors"]["value"] == 0.0)
    _check("interval present", model["share_no_errors"]["ci_low"] is not None)
    diff = story["model_vs_human"]["share_no_errors"]
    _check(
        "model minus human per video",
        diff["difference"] == 1.0 and diff["n_videos"] >= 1,
        str(diff),
    )
    agree = story["agreement"]["model"]["accuracy"]
    _check("full agreement, alpha undefined without variation", agree["pct_agreement"] == 1.0 and agree["alpha"] is None, str(agree))  # fmt: skip
    _check("list precision", results["variables"]["objects"]["by_source"]["model"]["headline"]["item_precision"]["value"] == 1.0)  # fmt: skip
    _check("guesses scored", results["guesses"]["share_correct"] == 1.0, str(results["guesses"]))
    model_videos = story["by_source"]["model"]["n_videos"]
    _check(
        "practice videos not analysed",
        model_videos <= len(task["rated_item_ids"]),
        str(model_videos),
    )

    rows = list(csv.reader(io.StringIO(human_rating.export_csv(RUN_ID).lstrip("﻿"))))
    header = rows[0]
    _check("export header", {"rater", "source_group", "answer", "accuracy", "list_marks", "source_guess"} <= set(header))  # fmt: skip
    n_expected = sum(len(v) for r in RATERS for v in task["assignment"][r].values())
    _check(
        "one export row per judgement",
        len(rows) - 1 == n_expected,
        f"{len(rows) - 1} vs {n_expected}",
    )
    excluded_col = header.index("rater_excluded")
    _check(
        "exclusion flagged in export",
        {r[excluded_col] for r in rows[1:] if r[0] == "r3@x.com"} == {"True"},
    )
