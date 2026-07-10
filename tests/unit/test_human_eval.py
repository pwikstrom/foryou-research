"""Tests for the human-input harness (``fyp.human_eval``).

Cost-free and storage-free: every ``data_io`` call is patched onto an
in-memory store, and a fake finished A/B run (manifest + report + two arm
parquets) is planted there. Covers the variable catalog, task CRUD +
validation, response saving/validation, submission, the ICR metric
computation (agreement / Cohen's kappa / Jaccard / correlation, human-vs-
machine and human-vs-human), the blindness of the task definition, and the
invitation gate.

Run:
    python tests/unit/test_human_eval.py
"""

import copy
import sys
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd

from fyp import ab_eval, data_io, human_eval

PASS = 0
FAIL = 0


def _check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


RUN_ID = "20260710T000000Z_test01"
ITEMS = [f"70000000000000000{i}" for i in range(6)]

_STORE: dict = {}


def _patch_data_io():
    """Route every data_io call human_eval/ab_eval make onto the in-memory store."""
    def save_json(data=None, storage_location="cache", filename="", **kw):
        _STORE[(storage_location, filename)] = copy.deepcopy(data)

    def load_json(storage_location="cache", filename="", **kw):
        return copy.deepcopy(_STORE.get((storage_location, filename)))

    def exists(storage_location="cache", filename="", **kw):
        return (storage_location, filename) in _STORE

    def remove(storage_location="cache", filename="", **kw):
        _STORE.pop((storage_location, filename), None)

    def save_parquet(df=None, storage_location="cache", filename="", **kw):
        _STORE[(storage_location, filename)] = df.copy()

    def load_parquet(storage_location="cache", filename="", **kw):
        return _STORE[(storage_location, filename)].copy()

    data_io.save_json = save_json
    data_io.load_json = load_json
    data_io.exists = exists
    data_io.remove = remove
    data_io.save_parquet = save_parquet
    data_io.load_parquet = load_parquet
    ab_eval.ensure_locations = lambda: None
    ab_eval.resolve_items = lambda ids: [
        {"item_id": str(i), "platform": "tiktok", "downloaded": True} for i in ids
    ]


def _plant_fake_run():
    """Store a complete run: manifest, report, and two arm parquets."""
    manifest = {
        "run_id": RUN_ID, "status": "complete", "started_by": "tester",
        "eval_set": "default", "item_ids": ITEMS, "n_items": len(ITEMS),
        "arms": [{"name": "live", "source": "live", "etag": "x"},
                 {"name": "cand", "source": "candidate", "etag": "y"}],
    }
    columns = {
        "multilingual": {"kind": "enum"},
        "objects": {"kind": "list"},
        "faces_age_estimate": {"kind": "numeric"},
        "transcript": {"kind": "freetext"},
    }
    report = {
        "run_id": RUN_ID, "arms": ["live", "cand"],
        "comparisons": {"live|cand": {"n_items": len(ITEMS), "columns": columns,
                                      "summary": {}}},
        "distributions": {
            "multilingual": {"column": "multilingual",
                             "arms": {"live": {"Yes": 3, "No": 3}}},
        },
        "adjudication": [], "costs": {},
    }
    data_io.save_json(data=manifest, storage_location=ab_eval.LOCATION,
                      filename=ab_eval._run_file(RUN_ID, "manifest.json"))
    data_io.save_json(data=report, storage_location=ab_eval.LOCATION,
                      filename=ab_eval._run_file(RUN_ID, "report.json"))
    # NOTE: "No" is an ab_eval sentinel ("nothing found"), so a Yes/No enum has
    # few filled-both pairs; Yes/Unclear keeps all six pairs kappa-eligible.
    live = pd.DataFrame({
        "item_id": ITEMS,
        "multilingual": ["Yes", "Unclear", "Yes", "Unclear", "Yes", "Unclear"],
        "objects": [["car", "dog"], ["cat"], [], ["car"], ["tree"], ["dog"]],
        "faces_age_estimate": [20.0, 30.0, 40.0, 50.0, 60.0, 70.0],
        "transcript": ["a"] * 6,
        "annotated_ok": [True] * 6,
    })
    cand = live.copy()
    cand["multilingual"] = ["Yes", "Yes", "Yes", "Unclear", "Yes", "Unclear"]
    data_io.save_parquet(df=live, storage_location=ab_eval.LOCATION,
                         filename=ab_eval._run_file(RUN_ID, "arm_live.parquet"))
    data_io.save_parquet(df=cand, storage_location=ab_eval.LOCATION,
                         filename=ab_eval._run_file(RUN_ID, "arm_cand.parquet"))


def test_available_variables():
    print("available_variables")
    catalog = {v["name"]: v for v in human_eval.available_variables(RUN_ID)}
    _check("all compared columns present",
           set(catalog) == {"multilingual", "objects", "faces_age_estimate", "transcript"},
           str(set(catalog)))
    _check("enum kind kept", catalog["multilingual"]["kind"] == "enum")
    _check("enum values resolved (contract or observed)",
           bool(catalog["multilingual"]["values"]),
           str(catalog["multilingual"]["values"]))
    _check("numeric has no values list", catalog["faces_age_estimate"]["values"] is None)


def test_task_crud():
    print("task CRUD + validation")
    try:
        human_eval.create_task("nope", "coding", ["multilingual"], [], "t")
        _check("missing run rejected", False)
    except Exception:
        _check("missing run rejected", True)
    try:
        human_eval.create_task(RUN_ID, "coding", ["not_a_var"], [], "t")
        _check("unknown variable rejected", False)
    except ValueError:
        _check("unknown variable rejected", True)
    task = human_eval.create_task(
        RUN_ID, "coding", ["multilingual", "objects", "faces_age_estimate"],
        ["coder_a@x.com", "coder_b@x.com"], created_by="admin@x.com")
    _check("task created", task["run_id"] == RUN_ID)
    _check("items snapshotted with platform",
           len(task["items"]) == 6 and task["items"][0]["platform"] == "tiktok")
    _check("field specs snapshotted",
           task["field_specs"]["multilingual"]["scale"] == "categorical")
    try:
        human_eval.create_task(RUN_ID, "coding", ["multilingual"], [], "t")
        _check("duplicate task rejected", False)
    except ValueError:
        _check("duplicate task rejected", True)
    index = human_eval.list_tasks()
    _check("task indexed", len(index) == 1 and index[0]["n_variables"] == 3)
    _check("blindness: task definition holds no machine values",
           "arm" not in str(task) and "Yes" not in str(task.get("items")))
    _check("invited coder allowed",
           human_eval.is_invited(task, "coder_a@x.com", is_admin=False))
    _check("uninvited coder blocked",
           not human_eval.is_invited(task, "stranger@x.com", is_admin=False))
    _check("admin always allowed",
           human_eval.is_invited(task, "stranger@x.com", is_admin=True))
    _check("tasks_for_user filters",
           len(human_eval.tasks_for_user("coder_a@x.com")) == 1
           and not human_eval.tasks_for_user("stranger@x.com")
           and len(human_eval.tasks_for_user("stranger@x.com", is_admin=True)) == 1)


def test_responses_and_validation():
    print("responses + validation")
    try:
        human_eval.save_response(RUN_ID, "coding", "coder_a@x.com", "999",
                                 {"multilingual": "Yes"})
        _check("foreign item rejected", False)
    except ValueError:
        _check("foreign item rejected", True)
    try:
        human_eval.save_response(RUN_ID, "coding", "coder_a@x.com", ITEMS[0],
                                 {"multilingual": "Banana"})
        _check("off-vocabulary enum rejected", False)
    except ValueError:
        _check("off-vocabulary enum rejected", True)
    try:
        human_eval.save_response(RUN_ID, "coding", "coder_a@x.com", ITEMS[0],
                                 {"faces_age_estimate": "abc"})
        _check("non-numeric rejected", False)
    except ValueError:
        _check("non-numeric rejected", True)

    # Coder A agrees with the live arm on 5 of 6 enum answers.
    a_enum = ["Yes", "Unclear", "Yes", "Unclear", "Yes", "Yes"]
    for i, item in enumerate(ITEMS):
        state = human_eval.save_response(RUN_ID, "coding", "coder_a@x.com", item, {
            "multilingual": a_enum[i],
            "objects": ["car"] if i % 2 == 0 else ["cat"],
            "faces_age_estimate": str(20 + 10 * i),
        })
    _check("responses persisted", len(state["responses"]) == 6)
    reloaded = human_eval.load_coder_state(RUN_ID, "coding", "coder_a@x.com")
    _check("state reload round-trips", len(reloaded["responses"]) == 6
           and reloaded["responses"][ITEMS[0]]["values"]["faces_age_estimate"] == 20.0)

    # Coder B answers a strict subset (4 items), fully agreeing with coder A.
    for i, item in enumerate(ITEMS[:4]):
        human_eval.save_response(RUN_ID, "coding", "coder_b@x.com", item, {
            "multilingual": a_enum[i],
            "objects": ["car"] if i % 2 == 0 else ["cat"],
            "faces_age_estimate": str(20 + 10 * i),
        })


def test_submit_and_metrics():
    print("submit + metrics")
    outcome = human_eval.submit(RUN_ID, "coding", "coder_a@x.com")
    _check("submit outcome", outcome == {"n_answered": 6, "n_items": 6}, str(outcome))
    try:
        human_eval.save_response(RUN_ID, "coding", "coder_a@x.com", ITEMS[0],
                                 {"multilingual": "Yes"})
        _check("post-submit saves rejected", False)
    except ValueError:
        _check("post-submit saves rejected", True)
    human_eval.submit(RUN_ID, "coding", "coder_b@x.com")

    results = human_eval.load_results(RUN_ID, "coding")
    _check("results stored", results is not None)
    hvm = results["human_vs_machine"]
    _check("coder x arm comparisons",
           set(hvm) == {"coder_a@x.com|live", "coder_a@x.com|cand",
                        "coder_b@x.com|live", "coder_b@x.com|cand"}, str(set(hvm)))
    a_live = hvm["coder_a@x.com|live"]["columns"]["multilingual"]
    _check("enum agreement hand-checked (5/6)",
           abs(a_live["agreement"] - 5 / 6) < 1e-9, str(a_live["agreement"]))
    _check("kappa computed on enum column", "kappa" in a_live and a_live["kappa"] is not None,
           str(a_live))
    _check("kappa summary present",
           hvm["coder_a@x.com|live"]["summary"]["mean_enum_kappa"] is not None)
    num = hvm["coder_a@x.com|live"]["columns"]["faces_age_estimate"]
    _check("numeric correlation perfect", abs(num["correlation"] - 1.0) < 1e-9, str(num))

    hvh = results["human_vs_human"]
    _check("human pair present", list(hvh) == ["coder_a@x.com|coder_b@x.com"], str(list(hvh)))
    pair = hvh["coder_a@x.com|coder_b@x.com"]
    _check("human pair aligned on common items only", pair["n_items"] == 4,
           str(pair["n_items"]))
    _check("full agreement between coders",
           pair["columns"]["multilingual"]["agreement"] == 1.0)

    per_coder = results["summary"]["per_coder"]
    _check("per-coder summary", per_coder["coder_a@x.com"]["n_items_coded"] == 6
           and per_coder["coder_b@x.com"]["n_items_coded"] == 4, str(per_coder))

    status = human_eval.coder_status(RUN_ID, "coding")
    _check("coder status derived",
           status["coder_a@x.com"]["status"] == "submitted"
           and status["coder_a@x.com"]["n_answered"] == 6, str(status))

    human = human_eval.load_human(RUN_ID)
    _check("load_human block", human is not None and "coding" in human
           and human["coding"]["results"] is not None)


def test_delete():
    print("delete")
    removed = human_eval.delete_task(RUN_ID, "coding")
    _check("delete removed files", removed)
    _check("task gone", human_eval.load_task(RUN_ID, "coding") is None)
    _check("index emptied", human_eval.list_tasks() == [])
    leftovers = [k for k in _STORE if "/human/" in k[1]]
    _check("no human artifacts left", not leftovers, str(leftovers))


def main():
    _patch_data_io()
    _plant_fake_run()
    test_available_variables()
    test_task_crud()
    test_responses_and_validation()
    test_submit_and_metrics()
    test_delete()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
