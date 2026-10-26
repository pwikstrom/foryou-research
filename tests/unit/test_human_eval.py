"""Tests for the human-input harness (``fyp.human_eval``).

Cost-free and storage-free: every ``data_io`` call is patched onto an
in-memory store, and a fake finished A/B run (manifest + report + two arm
parquets) is planted there. Covers the variable catalog, task CRUD +
validation, response saving/validation, submission, the ICR metric
computation (agreement / Cohen's kappa / Jaccard / correlation, human-vs-
machine and human-vs-human), the blindness of the task definition, and the
invitation gate.

The tests share one planted run and build on each other's state (responses
are saved in one test and scored in the next), so they run in file order as a
sequence — selecting a single test by name will not stand on its own.

Run:
    python -m pytest tests/unit/test_human_eval.py
"""

import copy
import csv
import io
import sys
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pandas as pd
import pytest

from fyp.annotation import ab_eval, human_eval
from fyp.core import data_io


def _check(name: str, ok: bool, detail: str = ""):
    assert ok, f"{name}  {detail}"
    print(f"  PASS  {name}")


RUN_ID = "20260710T000000Z_test01"
ITEMS = [f"70000000000000000{i}" for i in range(6)]

_STORE: dict = {}


def _patch_data_io() -> dict:
    """Route every data_io call human_eval/ab_eval make onto the in-memory store.

    Returns the original attributes so the fixture can put them back — these
    are module-level rebinds, so leaving them in place would follow the whole
    pytest session into every other test module.
    """

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

    patches = {
        (data_io, "save_json"): save_json,
        (data_io, "load_json"): load_json,
        (data_io, "exists"): exists,
        (data_io, "remove"): remove,
        (data_io, "save_parquet"): save_parquet,
        (data_io, "load_parquet"): load_parquet,
        (ab_eval, "ensure_locations"): lambda: None,
        (ab_eval, "resolve_items"): lambda ids: [
            {"item_id": str(i), "platform": "tiktok", "downloaded": True} for i in ids
        ],
    }
    originals = {(mod, attr): getattr(mod, attr) for mod, attr in patches}
    for (mod, attr), fn in patches.items():
        setattr(mod, attr, fn)
    return originals


@pytest.fixture(scope="module", autouse=True)
def _fake_run():
    """Plant the in-memory run once for the whole module, then restore data_io."""
    originals = _patch_data_io()
    _plant_fake_run()
    try:
        yield
    finally:
        for (mod, attr), fn in originals.items():
            setattr(mod, attr, fn)
        _STORE.clear()


def _plant_fake_run():
    """Store a complete run: manifest, report, and two arm parquets."""
    manifest = {
        "run_id": RUN_ID,
        "status": "complete",
        "started_by": "tester",
        "eval_set": "default",
        "item_ids": ITEMS,
        "n_items": len(ITEMS),
        "arms": [
            {"name": "live", "source": "live", "etag": "x"},
            {"name": "cand", "source": "candidate", "etag": "y"},
        ],
    }
    columns = {
        "multilingual": {"kind": "enum"},
        "objects": {"kind": "list"},
        "faces_age_estimate": {"kind": "numeric"},
        "transcript": {"kind": "freetext"},
    }
    report = {
        "run_id": RUN_ID,
        "arms": ["live", "cand"],
        "comparisons": {"live|cand": {"n_items": len(ITEMS), "columns": columns, "summary": {}}},
        "distributions": {
            "multilingual": {"column": "multilingual", "arms": {"live": {"Yes": 3, "No": 3}}},
        },
        "adjudication": [],
        "costs": {},
    }
    data_io.save_json(
        data=manifest,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(RUN_ID, "manifest.json"),
    )
    data_io.save_json(
        data=report,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(RUN_ID, "report.json"),
    )
    # NOTE: "Unclear" is an ab_eval NA sentinel ("nothing found"), so an
    # enum using it has few filled-both pairs and kappa comes back None.
    # Yes/No are both real answers, keeping all six pairs kappa-eligible.
    live = pd.DataFrame(
        {
            "item_id": ITEMS,
            "multilingual": ["Yes", "No", "Yes", "No", "Yes", "No"],
            "objects": [["car", "dog"], ["cat"], [], ["car"], ["tree"], ["dog"]],
            "faces_age_estimate": [20.0, 30.0, 40.0, 50.0, 60.0, 70.0],
            "transcript": ["a"] * 6,
            "annotated_ok": [True] * 6,
        }
    )
    cand = live.copy()
    cand["multilingual"] = ["Yes", "Yes", "Yes", "No", "Yes", "No"]
    data_io.save_parquet(
        df=live,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(RUN_ID, "arm_live.parquet"),
    )
    data_io.save_parquet(
        df=cand,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(RUN_ID, "arm_cand.parquet"),
    )


def test_available_variables():
    print("available_variables")
    catalog = {v["name"]: v for v in human_eval.available_variables(RUN_ID)}
    _check(
        "all compared columns present",
        set(catalog) == {"multilingual", "objects", "faces_age_estimate", "transcript"},
        str(set(catalog)),
    )
    _check("enum kind kept", catalog["multilingual"]["kind"] == "enum")
    _check(
        "enum values resolved (contract or observed)",
        bool(catalog["multilingual"]["values"]),
        str(catalog["multilingual"]["values"]),
    )
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
        RUN_ID,
        "coding",
        ["multilingual", "objects", "faces_age_estimate"],
        ["coder_a@x.com", "coder_b@x.com"],
        created_by="admin@x.com",
    )
    _check("task created", task["run_id"] == RUN_ID)
    _check(
        "items snapshotted with platform",
        len(task["items"]) == 6 and task["items"][0]["platform"] == "tiktok",
    )
    _check("field specs snapshotted", task["field_specs"]["multilingual"]["scale"] == "categorical")
    try:
        human_eval.create_task(RUN_ID, "coding", ["multilingual"], [], "t")
        _check("duplicate task rejected", False)
    except ValueError:
        _check("duplicate task rejected", True)
    index = human_eval.list_tasks()
    _check("task indexed", len(index) == 1 and index[0]["n_variables"] == 3)
    _check(
        "blindness: task definition holds no machine values",
        "arm" not in str(task) and "Yes" not in str(task.get("items")),
    )
    _check("invited coder allowed", human_eval.is_invited(task, "coder_a@x.com", is_admin=False))
    _check(
        "uninvited coder blocked", not human_eval.is_invited(task, "stranger@x.com", is_admin=False)
    )
    _check("admin always allowed", human_eval.is_invited(task, "stranger@x.com", is_admin=True))
    _check(
        "tasks_for_user filters",
        len(human_eval.tasks_for_user("coder_a@x.com")) == 1
        and not human_eval.tasks_for_user("stranger@x.com")
        and len(human_eval.tasks_for_user("stranger@x.com", is_admin=True)) == 1,
    )


def test_responses_and_validation():
    print("responses + validation")
    try:
        human_eval.save_response(RUN_ID, "coding", "coder_a@x.com", "999", {"multilingual": "Yes"})
        _check("foreign item rejected", False)
    except ValueError:
        _check("foreign item rejected", True)
    try:
        human_eval.save_response(
            RUN_ID, "coding", "coder_a@x.com", ITEMS[0], {"multilingual": "Banana"}
        )
        _check("off-vocabulary enum rejected", False)
    except ValueError:
        _check("off-vocabulary enum rejected", True)
    try:
        human_eval.save_response(
            RUN_ID, "coding", "coder_a@x.com", ITEMS[0], {"faces_age_estimate": "abc"}
        )
        _check("non-numeric rejected", False)
    except ValueError:
        _check("non-numeric rejected", True)

    # Coder A agrees with the live arm on 5 of 6 enum answers.
    a_enum = ["Yes", "No", "Yes", "No", "Yes", "Yes"]
    for i, item in enumerate(ITEMS):
        state = human_eval.save_response(
            RUN_ID,
            "coding",
            "coder_a@x.com",
            item,
            {
                "multilingual": a_enum[i],
                "objects": ["car"] if i % 2 == 0 else ["cat"],
                "faces_age_estimate": str(20 + 10 * i),
            },
        )
    _check("responses persisted", len(state["responses"]) == 6)
    reloaded = human_eval.load_coder_state(RUN_ID, "coding", "coder_a@x.com")
    _check(
        "state reload round-trips",
        len(reloaded["responses"]) == 6
        and reloaded["responses"][ITEMS[0]]["values"]["faces_age_estimate"] == 20.0,
    )

    # Coder B answers a strict subset (4 items), fully agreeing with coder A.
    for i, item in enumerate(ITEMS[:4]):
        human_eval.save_response(
            RUN_ID,
            "coding",
            "coder_b@x.com",
            item,
            {
                "multilingual": a_enum[i],
                "objects": ["car"] if i % 2 == 0 else ["cat"],
                "faces_age_estimate": str(20 + 10 * i),
            },
        )


def test_notes_and_coder_rows():
    print("notes + coder rows")
    state = human_eval.save_response(
        RUN_ID, "coding", "coder_a@x.com", ITEMS[0], values=None, note="check the audio"
    )
    _check(
        "note-only save keeps values",
        state["responses"][ITEMS[0]]["values"].get("multilingual") == "Yes"
        and state["responses"][ITEMS[0]]["note"] == "check the audio",
    )
    _check("note-only item does not change n_answered", human_eval._n_answered(state) == 6)
    # A values-save replaces the whole values dict (the UI always sends the
    # full form) but keeps the note.
    state = human_eval.save_response(
        RUN_ID,
        "coding",
        "coder_a@x.com",
        ITEMS[0],
        values={"multilingual": "Yes", "objects": ["car"], "faces_age_estimate": "20"},
        note=None,
    )
    _check(
        "values-save keeps the existing note",
        state["responses"][ITEMS[0]]["note"] == "check the audio",
    )
    state = human_eval.save_response(
        RUN_ID, "coding", "coder_a@x.com", ITEMS[1], values=None, note="x" * 3000
    )
    _check(
        "oversized note capped",
        len(state["responses"][ITEMS[1]]["note"]) == human_eval.MAX_NOTE_CHARS,
    )
    state = human_eval.save_response(
        RUN_ID, "coding", "coder_a@x.com", ITEMS[1], values=None, note=""
    )
    _check("empty note clears it", "note" not in state["responses"][ITEMS[1]])

    # A note on an item the coder never answered is context, not an answer.
    state = human_eval.save_response(
        RUN_ID, "coding", "coder_b@x.com", ITEMS[4], values=None, note="could not judge this one"
    )
    _check("note on unanswered item not counted", human_eval._n_answered(state) == 4)

    rows = human_eval.coder_rows(RUN_ID, "coding", "coder_a@x.com")
    row0 = next(r for r in rows if r["item_id"] == ITEMS[0])
    row1 = next(r for r in rows if r["item_id"] == ITEMS[1])
    _check(
        "coder_rows normalizes values",
        row0["multilingual"] == "Yes" and row0["objects"] == "[car]",
        str(row0),
    )
    _check(
        "coder_rows carries note only when set",
        row0.get("note") == "check the audio" and "note" not in row1,
    )

    notes = human_eval.collect_notes(RUN_ID, "coding")
    _check(
        "collect_notes aggregates across coders",
        {(n["username"], n["item_id"]) for n in notes}
        == {("coder_a@x.com", ITEMS[0]), ("coder_b@x.com", ITEMS[4])},
        str(notes),
    )


def test_submit_and_metrics():
    print("submit + metrics")
    outcome = human_eval.submit(RUN_ID, "coding", "coder_a@x.com")
    _check("submit outcome", outcome == {"n_answered": 6, "n_items": 6}, str(outcome))
    try:
        human_eval.save_response(
            RUN_ID, "coding", "coder_a@x.com", ITEMS[0], {"multilingual": "Yes"}
        )
        _check("post-submit saves rejected", False)
    except ValueError:
        _check("post-submit saves rejected", True)
    human_eval.submit(RUN_ID, "coding", "coder_b@x.com")

    results = human_eval.load_results(RUN_ID, "coding")
    _check("results stored", results is not None)
    hvm = results["human_vs_machine"]
    _check(
        "coder x arm comparisons",
        set(hvm)
        == {"coder_a@x.com|live", "coder_a@x.com|cand", "coder_b@x.com|live", "coder_b@x.com|cand"},
        str(set(hvm)),
    )
    a_live = hvm["coder_a@x.com|live"]["columns"]["multilingual"]
    _check(
        "enum agreement hand-checked (5/6)",
        abs(a_live["agreement"] - 5 / 6) < 1e-9,
        str(a_live["agreement"]),
    )
    _check(
        "kappa computed on enum column",
        "kappa" in a_live and a_live["kappa"] is not None,
        str(a_live),
    )
    _check(
        "kappa summary present", hvm["coder_a@x.com|live"]["summary"]["mean_enum_kappa"] is not None
    )
    num = hvm["coder_a@x.com|live"]["columns"]["faces_age_estimate"]
    _check("numeric correlation perfect", abs(num["correlation"] - 1.0) < 1e-9, str(num))

    hvh = results["human_vs_human"]
    _check("human pair present", list(hvh) == ["coder_a@x.com|coder_b@x.com"], str(list(hvh)))
    pair = hvh["coder_a@x.com|coder_b@x.com"]
    _check("human pair aligned on common items only", pair["n_items"] == 4, str(pair["n_items"]))
    _check("full agreement between coders", pair["columns"]["multilingual"]["agreement"] == 1.0)

    per_coder = results["summary"]["per_coder"]
    _check(
        "per-coder summary",
        per_coder["coder_a@x.com"]["n_items_coded"] == 6
        and per_coder["coder_b@x.com"]["n_items_coded"] == 4,
        str(per_coder),
    )

    status = human_eval.coder_status(RUN_ID, "coding")
    _check(
        "coder status derived",
        status["coder_a@x.com"]["status"] == "submitted"
        and status["coder_a@x.com"]["n_answered"] == 6,
        str(status),
    )

    human = human_eval.load_human(RUN_ID)
    _check(
        "load_human block",
        human is not None and "coding" in human and human["coding"]["results"] is not None,
    )

    alpha = results["alpha"]["multilingual"]
    _check("alpha level nominal for enums", alpha["level"] == "nominal", str(alpha))
    _check(
        "humans-only alpha: coders agree on all 4 shared items",
        alpha["humans"]["alpha"] == 1.0 and alpha["humans"]["n_units"] == 4,
        str(alpha["humans"]),
    )
    # Units: items 0-3 [a, b, live] agree; item 4 [Yes, Yes]; item 5 [Yes, No].
    # 9 Yes + 7 No over 16 values: D_o = 2/16, D_e = 126/240 -> alpha = 16/21.
    with_live = alpha["with_arm"]["live"]
    _check(
        "coders + live alpha hand-checked (16/21)",
        abs(with_live["alpha"] - 16 / 21) < 1e-9 and with_live["n_values"] == 16,
        str(with_live),
    )
    _check("one alpha per contract", set(alpha["with_arm"]) == {"live", "cand"})
    _check(
        "interval alpha for numbers",
        results["alpha"]["faces_age_estimate"]["level"] == "interval"
        and results["alpha"]["faces_age_estimate"]["with_arm"]["live"]["alpha"] == 1.0,
        str(results["alpha"]["faces_age_estimate"]),
    )
    _check("set alpha for lists", results["alpha"]["objects"]["level"] == "set")


def test_vote_task_crud():
    print("vote task CRUD")
    try:
        human_eval.create_task(RUN_ID, "vote", [], [], "t", arms=["live"])
        _check("single-arm vote rejected", False)
    except ValueError:
        _check("single-arm vote rejected", True)
    try:
        human_eval.create_task(RUN_ID, "vote", [], [], "t", arms=["live", "nope"])
        _check("foreign arm rejected", False)
    except ValueError:
        _check("foreign arm rejected", True)
    task = human_eval.create_task(
        RUN_ID,
        "vote",
        [],
        ["coder_a@x.com", "coder_b@x.com"],
        created_by="admin@x.com",
        arms=["cand", "live"],
    )  # any order in; canonical order out
    _check(
        "empty variables defaults to all",
        set(task["variables"]) == {"multilingual", "objects", "faces_age_estimate", "transcript"},
        str(task["variables"]),
    )
    _check(
        "order_seed is 32 hex",
        len(task["order_seed"]) == 32 and all(c in "0123456789abcdef" for c in task["order_seed"]),
    )
    _check(
        "arms subset stored in canonical order", task["arms"] == ["live", "cand"], str(task["arms"])
    )
    try:
        human_eval.create_task(RUN_ID, "vote", [], [], "t")
        _check("duplicate vote task rejected", False)
    except ValueError:
        _check("duplicate vote task rejected", True)
    _check(
        "coding + vote coexist in index",
        {(t["run_id"], t["task_type"]) for t in human_eval.list_tasks()}
        == {(RUN_ID, "coding"), (RUN_ID, "vote")},
    )

    payload = human_eval.vote_options_payload(task, "coder_a@x.com")
    blob = str(payload)
    _check(
        "blindness: no arm names/etags in options payload",
        "live" not in blob and "cand" not in blob and "etag" not in blob,
    )
    first = payload[ITEMS[0]]
    _check(
        "options carry only option + selected variables",
        all(
            set(o) == {"option", "values"} and set(o["values"]) == set(task["variables"])
            for o in first
        ),
        str(first),
    )


def test_vote_permutation():
    print("vote permutation")
    task = human_eval.load_task(RUN_ID, "vote")
    p1 = human_eval._vote_permutation(task, ITEMS[0], "coder_a@x.com")
    p2 = human_eval._vote_permutation(task, ITEMS[0], "coder_a@x.com")
    _check("deterministic per (item, coder)", p1 == p2)
    _check("always a permutation of arms", sorted(p1) == sorted(task["arms"]))
    orders = {
        tuple(human_eval._vote_permutation(task, item, user))
        for item in ITEMS
        for user in ("coder_a@x.com", "coder_b@x.com")
    }
    _check("orderings vary across items/coders", len(orders) > 1, str(orders))
    arm = human_eval.resolve_vote_choice(task, ITEMS[0], "coder_a@x.com", "A")
    _check("letter A resolves to first permuted arm", arm == p1[0])
    _check(
        "letter round-trips", human_eval.letter_for_arm(task, ITEMS[0], "coder_a@x.com", arm) == "A"
    )
    _check(
        "tie passes through",
        human_eval.resolve_vote_choice(task, ITEMS[0], "coder_a@x.com", "tie") == "tie",
    )


def test_vote_responses_and_results():
    print("vote responses + results")
    task = human_eval.load_task(RUN_ID, "vote")
    for bad in ({"choice": "Z"}, {"choice": ""}, {"choice": "A", "extra": 1}, {}):
        try:
            human_eval.save_response(RUN_ID, "vote", "coder_a@x.com", ITEMS[0], bad)
            _check(f"bad vote rejected: {bad}", False)
        except ValueError:
            _check(f"bad vote rejected: {bad}", True)

    def vote(user, item, arm_or_tie):
        letter = (
            "tie"
            if arm_or_tie == "tie"
            else human_eval.letter_for_arm(task, item, user, arm_or_tie)
        )
        human_eval.save_response(RUN_ID, "vote", user, item, {"choice": letter})

    # Coder A: cand 4, live 1, tie 1. Coder B (4 items): cand 2, live 1, tie 1.
    a_pattern = ["cand", "cand", "cand", "cand", "live", "tie"]
    for item, arm in zip(ITEMS, a_pattern):
        vote("coder_a@x.com", item, arm)
    state = human_eval.load_coder_state(RUN_ID, "vote", "coder_a@x.com")
    _check(
        "votes stored as arm names",
        state["responses"][ITEMS[0]]["values"] == {"choice": "cand"}
        and state["responses"][ITEMS[5]]["values"] == {"choice": "tie"},
        str(state["responses"]),
    )
    for item, arm in zip(ITEMS[:4], ["cand", "cand", "live", "tie"]):
        vote("coder_b@x.com", item, arm)

    human_eval.submit(RUN_ID, "vote", "coder_a@x.com")
    human_eval.submit(RUN_ID, "vote", "coder_b@x.com")
    results = human_eval.load_results(RUN_ID, "vote")
    _check("vote results stored", results is not None)
    _check(
        "per-coder tallies",
        results["per_coder"]["coder_a@x.com"]
        == {"wins": {"live": 1, "cand": 4}, "ties": 1, "n_votes": 6}
        and results["per_coder"]["coder_b@x.com"]
        == {"wins": {"live": 1, "cand": 2}, "ties": 1, "n_votes": 4},
        str(results["per_coder"]),
    )
    pooled = results["pooled"]
    _check("pooled wins", pooled["wins"] == {"live": 2, "cand": 6}, str(pooled))
    _check(
        "win rates over non-tie",
        abs(pooled["win_rates"]["cand"] - 0.75) < 1e-9
        and abs(pooled["win_rates"]["live"] - 0.25) < 1e-9,
    )
    _check("tie rate", abs(results["tie_rate"] - 0.2) < 1e-9, str(results["tie_rate"]))
    from scipy.stats import binomtest

    expected_p = float(binomtest(2, 8, 0.5).pvalue)
    _check(
        "sign test matches direct binomtest",
        abs(results["sign_test"]["p_value"] - expected_p) < 1e-12,
        str(results["sign_test"]),
    )

    human = human_eval.load_human(RUN_ID)
    _check(
        "load_human returns both blocks",
        set(human) == {"coding", "vote"} and human["vote"]["results"] is not None,
    )


def test_notifications():
    print("notifications")
    human_eval.set_notified(RUN_ID, "vote", "coder_a@x.com")
    task = human_eval.load_task(RUN_ID, "vote")
    _check(
        "set_notified flips + persists",
        task["coders"]["coder_a@x.com"]["notified"] is True
        and task["coders"]["coder_a@x.com"].get("notified_at"),
    )
    _check("other coder untouched", task["coders"]["coder_b@x.com"]["notified"] is False)
    human_eval.set_notified(RUN_ID, "vote", "stranger@x.com")  # no-op, no raise
    _check(
        "unknown coder is a no-op",
        "stranger@x.com" not in human_eval.load_task(RUN_ID, "vote")["coders"],
    )

    import os

    from web_interface.integrations import mail_utils

    _check("is_email accepts a@b.co", mail_utils.is_email("a@b.co"))
    _check(
        "is_email rejects bad values",
        not mail_utils.is_email("admin")
        and not mail_utils.is_email("a@b")
        and not mail_utils.is_email("a b@c.d")
        and not mail_utils.is_email(""),
    )
    old = os.environ.pop("MAIL_PASSWORD", None)
    try:
        sent = mail_utils.send_invitation_email("a@b.co", RUN_ID, "vote", "t", 6, 4)
        _check("invitation without MAIL_PASSWORD returns False", sent is False)
    finally:
        if old is not None:
            os.environ["MAIL_PASSWORD"] = old


def test_delete():
    print("delete")
    removed = human_eval.delete_task(RUN_ID, "vote")
    _check("vote delete removed files", removed)
    _check("vote task gone", human_eval.load_task(RUN_ID, "vote") is None)
    _check("coding task survives vote delete", human_eval.load_task(RUN_ID, "coding") is not None)
    _check(
        "no vote artifacts left", not [k for k in _STORE if "/human/" in k[1] and "vote" in k[1]]
    )
    removed = human_eval.delete_task(RUN_ID, "coding")
    _check("delete removed files", removed)
    _check("task gone", human_eval.load_task(RUN_ID, "coding") is None)
    _check("index emptied", human_eval.list_tasks() == [])
    leftovers = [k for k in _STORE if "/human/" in k[1]]
    _check("no human artifacts left", not leftovers, str(leftovers))


SOLO_RUN_ID = "20260710T000000Z_solo01"


def test_single_contract_run():
    print("single-contract run")
    # One arm means no pairwise comparisons: the report names no columns, so
    # the catalog has to come from the arm's own frame.
    manifest = {
        "run_id": SOLO_RUN_ID,
        "status": "complete",
        "started_by": "tester",
        "eval_set": "default",
        "item_ids": ITEMS,
        "n_items": len(ITEMS),
        "arms": [{"name": "live", "source": "live", "etag": "x"}],
    }
    report = {
        "run_id": SOLO_RUN_ID,
        "arms": ["live"],
        "comparisons": {},
        "distributions": {},
        "adjudication": [],
        "costs": {},
    }
    data_io.save_json(
        data=manifest,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(SOLO_RUN_ID, "manifest.json"),
    )
    data_io.save_json(
        data=report,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(SOLO_RUN_ID, "report.json"),
    )
    live = pd.DataFrame(
        {
            "item_id": ITEMS,
            "multilingual": ["Yes", "No", "Yes", "No", "Yes", "No"],
            "objects": [["car", "dog"], ["cat"], [], ["car"], ["tree"], ["dog"]],
            "faces_age_estimate": [20.0, 30.0, 40.0, 50.0, 60.0, 70.0],
            "annotated_ok": [True] * 6,
        }
    )
    data_io.save_parquet(
        df=live,
        storage_location=ab_eval.LOCATION,
        filename=ab_eval._run_file(SOLO_RUN_ID, "arm_live.parquet"),
    )

    catalog = {v["name"]: v for v in human_eval.available_variables(SOLO_RUN_ID)}
    _check(
        "arm columns listed, bookkeeping skipped",
        set(catalog) == {"multilingual", "objects", "faces_age_estimate"},
        str(set(catalog)),
    )
    _check(
        "kinds classified like compare_arms",
        (
            catalog["multilingual"]["kind"],
            catalog["objects"]["kind"],
            catalog["faces_age_estimate"]["kind"],
        )
        == ("enum", "list", "numeric"),
        str({n: v["kind"] for n, v in catalog.items()}),
    )
    _check(
        "enum values resolved (contract or observed)",
        set(catalog["multilingual"]["values"] or []) >= {"Yes", "No"},
        str(catalog["multilingual"]["values"]),
    )

    human_eval.create_task(
        SOLO_RUN_ID, "coding", ["multilingual", "faces_age_estimate"], ["coder_a@x.com"], "t"
    )
    a_enum = ["Yes", "No", "Yes", "No", "Yes", "Yes"]
    for i, item in enumerate(ITEMS):
        human_eval.save_response(
            SOLO_RUN_ID,
            "coding",
            "coder_a@x.com",
            item,
            {"multilingual": a_enum[i], "faces_age_estimate": str(20 + 10 * i)},
        )
    human_eval.save_response(
        SOLO_RUN_ID, "coding", "coder_a@x.com", ITEMS[1], values=None, note="=HYPERLINK(1)"
    )
    human_eval.submit(SOLO_RUN_ID, "coding", "coder_a@x.com")
    hvm = human_eval.load_results(SOLO_RUN_ID, "coding")["human_vs_machine"]
    _check("coder scored against the one arm", set(hvm) == {"coder_a@x.com|live"}, str(set(hvm)))
    agreement = hvm["coder_a@x.com|live"]["columns"]["multilingual"]["agreement"]
    _check("enum agreement hand-checked (5/6)", abs(agreement - 5 / 6) < 1e-9, str(agreement))

    alpha = human_eval.load_results(SOLO_RUN_ID, "coding")["alpha"]["multilingual"]
    _check("no humans-only alpha with one coder", alpha["humans"] is None, str(alpha))
    # 7 Yes + 5 No over 12 values, one split item: D_o = 2/12, D_e = 70/132 -> 24/35.
    _check(
        "coder + contract alpha hand-checked (24/35)",
        abs(alpha["with_arm"]["live"]["alpha"] - 24 / 35) < 1e-9,
        str(alpha["with_arm"]),
    )

    text = human_eval.export_codings_csv(SOLO_RUN_ID, "coding")
    _check("export starts with a byte-order mark", text.startswith("\ufeff"))
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    coder = "human:coder_a@x.com"
    _check(
        "export header: contract then coder per variable, notes last",
        rows[0]
        == [
            "item_id",
            "platform",
            "multilingual__live",
            f"multilingual__{coder}",
            "faces_age_estimate__live",
            f"faces_age_estimate__{coder}",
            f"note__{coder}",
        ],
        str(rows[0]),
    )
    _check("one row per video", len(rows) == 1 + len(ITEMS), str(len(rows)))
    _check(
        "values as given, side by side",
        rows[1] == [ITEMS[0], "tiktok", "Yes", "Yes", "20", "20", ""]
        and rows[6][2:4] == ["No", "Yes"],
        str(rows[1]) + str(rows[6]),
    )
    _check("formula in a note neutralized", rows[2][6] == "'=HYPERLINK(1)", str(rows[2]))
    try:
        human_eval.export_codings_csv(SOLO_RUN_ID, "vote")
        _check("vote export rejected", False)
    except ValueError:
        _check("vote export rejected", True)
    _check("solo task deleted", human_eval.delete_task(SOLO_RUN_ID, "coding"))


def test_krippendorff_alpha_reference():
    print("Krippendorff's alpha reference values")
    # Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability": four
    # observers, twelve units, missing data. Published: nominal .743, interval .849.
    na = None
    observers = [
        [1, 2, 3, 3, 2, 1, 4, 1, 2, na, na, na],
        [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, na, 3],
        [na, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, na],
        [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, na],
    ]
    units = [[v for v in unit if v is not None] for unit in zip(*observers, strict=True)]
    nominal = human_eval.krippendorff_alpha([[str(v) for v in u] for u in units], "nominal")
    interval = human_eval.krippendorff_alpha([[float(v) for v in u] for u in units], "interval")
    _check("nominal .743", round(nominal["alpha"], 3) == 0.743, str(nominal))
    _check("interval .849", round(interval["alpha"], 3) == 0.849, str(interval))
    _check("single-value unit dropped", nominal["n_units"] == 11 and nominal["n_values"] == 40)
    _check(
        "undefined without variation",
        human_eval.krippendorff_alpha([["a", "a"], ["a", "a"]], "nominal")["alpha"] is None,
    )
    same = frozenset({"car"})
    _check(
        "set level: identical sets agree",
        human_eval.krippendorff_alpha([[same, same], [frozenset(), frozenset()]], "set")["alpha"]
        == 1.0,
    )


def test_alpha_answers_as_given():
    print("alpha observations: answers as given")
    value = human_eval._alpha_value
    _check("'None' is an answer", value("None", "enum") == "none")
    _check("'Other' is an answer", value(" Other ", "enum") == "other")
    _check("blank is missing", value("", "enum") is None and value(None, "enum") is None)
    _check("NaN number is missing", value(np.nan, "numeric") is None)
    _check("empty list is the answer 'nothing'", value([], "list") == frozenset())
    _check("['none'] equals the empty list", value(["none"], "list") == frozenset())
    _check("list keeps 'unknown'", value(["Unknown"], "list") == frozenset({"unknown"}))
    failed = pd.DataFrame(
        {"item_id": ["1", "2"], "objects": [["car"], []], "annotated_ok": [True, False]}
    )
    _check(
        "a failed annotation is missing, not 'nothing'",
        human_eval._alpha_column(failed, "objects", "list") == {"1": frozenset({"car"})},
    )
    _check(
        "a spreadsheet keeps numbers and a bare dash as given",
        human_eval._export_cell(-5.0) == "-5"
        and human_eval._export_cell("-3.5") == "-3.5"
        and human_eval._export_cell("-") == "-",
    )
    _check(
        "a formula-like answer is neutralized",
        human_eval._export_cell("+cmd") == "'+cmd"
        and human_eval._export_cell(["=x", "y"]) == "'=x; y",
    )


def test_coding_options():
    print("coder options per variable")
    opts = human_eval._coding_options
    yes_no = opts("aigc", "enum", ["Yes", "No", "Unclear"], None)
    _check("codebook 'Unclear' means no extra can't-tell", yes_no["cant_tell"] is False)
    gender = opts("main_gender", "enum", ["Female", "Male", "Nonbinary", "Multiple", "-"], None)
    _check(
        "gender: '-' reads 'No person in the video', can't tell offered",
        gender["option_labels"] == {"-": "No person in the video"} and gender["cant_tell"],
        str(gender),
    )
    language = opts("spoken_language", "enum", None, None)
    _check(
        "open category: typed, with its codebook '-' and can't tell",
        language["open"]
        and language["dash_label"] == "No speech or singing"
        and language["cant_tell"]
        and "suggestions" not in language,
        str(language),
    )
    _check(
        "open category without a codebook '-'",
        opts("main_activity", "enum", None, None)["dash_label"] is None,
    )
    score = opts("political_score", "numeric", None, (0, 100))
    _check(
        "numbers: codebook scale + can't tell", score["range"] == [0, 100] and score["cant_tell"]
    )
    _check("lists: no can't tell", opts("objects", "list", None, None)["cant_tell"] is False)


def test_cant_tell_validation():
    print("can't tell validation")
    task = {
        "field_specs": {
            "main_gender": {"kind": "enum", "values": ["Female", "Male", "-"], "cant_tell": True},
            "aigc": {"kind": "enum", "values": ["Yes", "No", "Unclear"], "cant_tell": False},
            "spoken_language": {"kind": "enum", "values": None, "open": True, "cant_tell": True},
            "political_score": {"kind": "numeric", "cant_tell": True},
        }
    }
    ok = human_eval._validate_values(
        task,
        {
            "main_gender": human_eval.CANT_TELL,
            "spoken_language": "French",
            "political_score": human_eval.CANT_TELL,
        },
    )
    _check(
        "can't tell and typed answers accepted",
        ok
        == {
            "main_gender": "can't tell",
            "spoken_language": "French",
            "political_score": "can't tell",
        },
        str(ok),
    )
    try:
        human_eval._validate_values(task, {"aigc": human_eval.CANT_TELL})
        _check("can't tell refused where the codebook has its own", False)
    except ValueError:
        _check("can't tell refused where the codebook has its own", True)


def test_database_frame_matches_recode():
    print("coder answers recoded like the contract's")
    variables = ["main_gender", "spoken_language", "content_category", "political_score", "aigc"]
    task = {
        "variables": variables,
        "field_specs": {
            "main_gender": {"kind": "enum"},
            "spoken_language": {"kind": "enum", "open": True},
            "content_category": {"kind": "list"},
            "political_score": {"kind": "numeric"},
            "aigc": {"kind": "enum"},
        },
    }
    coder = [
        {"main_gender": "Female", "spoken_language": "French",
         "content_category": ["Fashion & Beauty"], "political_score": 50.0, "aigc": "No"},
        {"main_gender": "-", "spoken_language": "-", "content_category": ["Film & TV"],
         "political_score": human_eval.CANT_TELL, "aigc": "Unclear"},
        {"main_gender": human_eval.CANT_TELL, "spoken_language": human_eval.CANT_TELL,
         "content_category": ["Comedy"], "political_score": 0.0, "aigc": ""},
    ]  # fmt: skip
    state = {"responses": {str(i): {"values": v} for i, v in enumerate(coder)}}
    frame = human_eval._database_frame(task, state).set_index("item_id")
    model = ab_eval.refine_from_flat_dicts(
        [
            {"item_id": "0", "main_gender": "Female", "spoken_language": "French",
             "content_category": "Fashion & Beauty", "political_score": 50, "aigc": "No",
             "type_of_story": "Descriptive"},
        ]
    ).astype(object).set_index("item_id")  # fmt: skip

    def _plain(v):
        return list(v) if isinstance(v, (list, np.ndarray)) else v

    _check(
        "same answer, same stored value (case, '&', 0-100 scale)",
        all(_plain(frame.at["0", v]) == _plain(model.at["0", v]) for v in variables),
        str({v: (_plain(frame.at["0", v]), _plain(model.at["0", v])) for v in variables}),
    )
    _check(
        "'-' and can't tell merge like the database",
        frame.at["1", "main_gender"] == "other category"
        and frame.at["2", "main_gender"] == "other category"
        and frame.at["1", "spoken_language"] == "unable to detect"
        and frame.at["2", "spoken_language"] == "unable to detect",
        str(frame[["main_gender", "spoken_language"]].to_dict()),
    )
    _check(
        "skips and can't tell on numbers stay missing",
        frame.at["2", "aigc"] is None and frame.at["1", "political_score"] is None,
    )
    _check("codebook 'Unclear' kept", frame.at["1", "aigc"] == "unclear")


def test_alpha_check_and_no_answer_share():
    print("check alpha and can't-tell shares")
    task = {
        "item_ids": ["0", "1", "2", "3"],
        "variables": ["main_gender"],
        "field_specs": {"main_gender": {"kind": "enum"}},
    }

    def _frame(values):
        return pd.DataFrame({"item_id": ["0", "1", "2", "3"], "main_gender": values})

    coders = {
        "a": _frame(["female", "male", "other category", "male"]),
        "b": _frame(["female", "male", "female", "male"]),
    }
    arms = {"live": _frame(["female", "male", "other category", "female"])}
    entry = human_eval._alpha_results(task, coders, arms)["main_gender"]
    _check(
        "main alpha pairs all 8 coder answers",
        entry["humans"]["n_values"] == 8,
        str(entry["humans"]),
    )
    _check(
        "check drops the merged label (item 2 left unpaired)",
        entry["humans_check"]["n_units"] == 3 and entry["humans_check"]["n_values"] == 6,
        str(entry["humans_check"]),
    )
    _check("check with the contract present", "live" in entry["with_arm_check"])
    shares = entry["no_answer_share"]
    _check(
        "shares: coders pooled, per coder, per contract",
        shares["coders"] == 1 / 8
        and shares["by_coder"] == {"a": 0.25, "b": 0.0}
        and shares["arms"] == {"live": 0.25},
        str(shares),
    )


def test_readable_answer():
    print("export spells out '-'")
    spec = {"option_labels": {"-": "No person in the video"}}
    _check("scalar", human_eval._readable_answer("-", spec) == "No person in the video")
    _check(
        "list",
        human_eval._readable_answer(["-", "Male"], {"option_labels": {"-": "Can't tell"}})
        == ["Can't tell", "Male"],
    )
    _check(
        "open category",
        human_eval._readable_answer("-", {"dash_label": "No speech or singing"})
        == "No speech or singing",
    )
    _check("other answers untouched", human_eval._readable_answer("Female", spec) == "Female")


def test_category_definitions_reach_the_coder():
    print("codebook category definitions")
    from fyp.annotation import annotation_contract as ac

    described = human_eval._contract_enum_descriptions(ac.load_contract())
    categories = described.get("content_category") or {}
    _check(
        "every content category carries its definition",
        len(categories) == len(ac.enum_values(ac.load_contract(), "content_category"))
        and all(categories.values()),
        str(len(categories)),
    )
    _check("bare enums carry none", "main_gender" not in described)
    _check("snapshotted with the task", "value_descriptions" in human_eval._OPTION_KEYS)
