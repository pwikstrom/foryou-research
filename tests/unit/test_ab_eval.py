"""Tests for the A/B contract-evaluation harness (``fyp.ab_eval``).

Cost-free: no Gemini calls — arms run through a stub runner. Covers candidate
CRUD, eval-set cap/sampling, contract-threaded arm rendering + flattening, the
in-memory production-recode refine, the scale-aware comparison, and — most
importantly — the ISOLATION GUARD: every write the harness makes must land in
the dedicated ``ab_eval`` location (or the two admin config stores), never in
``machine_annotations_*`` / ``recoded``.

Run:
    python tests/unit/test_ab_eval.py
"""

import copy
import json
import re
import sys
import tomllib
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd

from fyp import ab_eval
from fyp import annotation_contract as ac
from fyp import annotation_schema as sch
from fyp import data_io

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


_TEST_CANDIDATE = "unittest-cand"


class StubRunner:
    """Runner double: returns canned parsed responses, records the prompt/schema."""

    def __init__(self, parsed_by_item: dict):
        self.parsed_by_item = parsed_by_item
        self.seen_prompts: list[str] = []

    def run(self, prompt_text, response_schema, item_ids, platform_map, progress_cb=None):
        self.seen_prompts.append(prompt_text)
        rows = []
        for i, item_id in enumerate(item_ids):
            parsed = self.parsed_by_item.get(str(item_id))
            rows.append({
                "item_id": str(item_id), "model": "stub", "parsed": parsed,
                "response": json.dumps(parsed) if parsed else "",
                "finish_reason": "STOP" if parsed else "DNF - stub",
                "usage": {"prompt_tokens": 10, "candidates_tokens": 5,
                          "thoughts_tokens": 0, "total_tokens": 15},
                "inference_duration": 0.1, "error": "" if parsed else "stub error",
            })
            if progress_cb:
                progress_cb(i + 1, len(item_ids))
        return rows


def _fake_parsed(contract: dict, seed: int = 0) -> dict:
    """Build a parsed response conforming to a contract's field specs."""
    out: dict = {}
    for field in contract.get("fields", []):
        name = field["name"]
        ftype = field.get("type", "string")
        is_array = bool(field.get("array"))
        if ftype == "object":
            item = {}
            for key, spec in field.get("keys", {}).items():
                parts = ac.parse_key_spec(spec["spec"] if isinstance(spec, dict) else spec)
                if parts["kind"] == "enum":
                    item[key] = ac.enum_values(contract, parts["enum"])[0]
                elif parts["kind"] == "int":
                    item[key] = parts.get("min", 0) if parts.get("min") is not None else 1
                elif parts["kind"] == "list":
                    item[key] = ["alpha", "beta"]
                else:
                    item[key] = f"text {seed}"
            out[name] = [item] if is_array else item
        elif ftype == "int":
            out[name] = int(field.get("min", 0)) + seed % max(1, int(field.get("max", 10)))
        elif field.get("enum"):
            values = ac.enum_values(contract, field["enum"])
            picked = values[seed % len(values)]
            out[name] = [picked] if is_array else picked
        elif is_array:
            out[name] = [f"thing{seed}", "widget"]
        else:
            out[name] = f"some {name} text {seed}"
    return out


# ------- candidates -------

def test_candidate_name_validation():
    good = ["a", "cand-1", "my_candidate", "x" * 40]
    bad = ["", "UPPER", "with space", "x" * 41, "dots.bad", "../evil"]
    ok = all(ab_eval.validate_candidate_name(n) for n in good) and \
        not any(ab_eval.validate_candidate_name(n) for n in bad)
    _check("test_candidate_name_validation", ok)


def test_candidate_crud():
    text = ac._read_baked_text()
    ab_eval.delete_candidate(_TEST_CANDIDATE)   # clean slate
    meta = ab_eval.save_candidate(_TEST_CANDIDATE, text, actor="tester", note="unit")
    listed = [m["name"] for m in ab_eval.list_candidates()]
    loaded = ab_eval.load_candidate(_TEST_CANDIDATE)
    dup_blocked = False
    try:
        ab_eval.save_candidate(_TEST_CANDIDATE, text)
    except FileExistsError:
        dup_blocked = True
    ab_eval.save_candidate(_TEST_CANDIDATE, text, overwrite=True)   # overwrite OK
    invalid_blocked = False
    try:
        ab_eval.save_candidate("other-cand", "not [ valid toml ===")
    except ValueError:
        invalid_blocked = True
    removed = ab_eval.delete_candidate(_TEST_CANDIDATE)
    ok = (meta["etag"].startswith("candidate:") and _TEST_CANDIDATE in listed
          and loaded["text"] == text and isinstance(loaded["contract"], dict)
          and dup_blocked and invalid_blocked and removed)
    _check("test_candidate_crud", ok)


# ------- eval set -------

def test_eval_set_cap_and_dedupe():
    snap = ab_eval.load_eval_set()
    try:
        stored = ab_eval.save_eval_set(["1", "2", "1", " 3 ", ""], actor="tester")
        dedupe_ok = stored["item_ids"] == ["1", "2", "3"]
        cap_ok = False
        try:
            ab_eval.save_eval_set([str(i) for i in range(ab_eval.MAX_EVAL_ITEMS + 1)])
        except ValueError:
            cap_ok = True
        _check("test_eval_set_cap_and_dedupe", dedupe_ok and cap_ok)
    finally:
        ab_eval.save_eval_set(snap.get("item_ids", []), actor=snap.get("updated_by") or "",
                              note=snap.get("note") or "")


def test_sample_items_seeded():
    a = ab_eval.sample_items(5, seed=42)
    b = ab_eval.sample_items(5, seed=42)
    ok = a == b and len(a) <= 5 and isinstance(a, list)
    _check("test_sample_items_seeded", ok, f"n={len(a)}")


# ------- contract-threaded arm rendering -------

def test_run_arm_threads_candidate_contract():
    live = tomllib.loads(ac._read_baked_text())
    cand = copy.deepcopy(live)
    cand["fields"].append({
        "name": "ab_test_only_field", "section": "scoring",
        "desc": "A field only the candidate contract has.", "scale": "text",
    })
    live_prompt = sch.build_prompt(live)

    parsed = {"itemA": _fake_parsed(cand, seed=1), "itemB": _fake_parsed(cand, seed=2)}
    runner = StubRunner(parsed)
    flat_rows, raw_rows = ab_eval.run_arm(
        "cand", cand, ["itemA", "itemB"], {}, runner=runner)

    prompt_differs = runner.seen_prompts and runner.seen_prompts[0] != live_prompt
    prompt_has_field = "ab_test_only_field" in (runner.seen_prompts[0] if runner.seen_prompts else "")
    flat_has_field = all("ab_test_only_field" in r for r in flat_rows)
    ok = (prompt_differs and prompt_has_field and flat_has_field
          and len(flat_rows) == 2 and len(raw_rows) == 2
          and flat_rows[0]["item_id"] == "itemA")
    _check("test_run_arm_threads_candidate_contract", bool(ok),
           f"differs={prompt_differs} has_field={prompt_has_field} flat={flat_has_field}")


def test_run_arm_failed_item_yields_bare_row():
    live = tomllib.loads(ac._read_baked_text())
    runner = StubRunner({"good": _fake_parsed(live)})   # "bad" has no parsed
    flat_rows, raw_rows = ab_eval.run_arm("live", live, ["good", "bad"], {}, runner=runner)
    bad_flat = next(r for r in flat_rows if r["item_id"] == "bad")
    ok = set(bad_flat.keys()) == {"item_id"} and len(flat_rows) == 2
    _check("test_run_arm_failed_item_yields_bare_row", ok)


# ------- refine + compare -------

def test_candidate_field_survives_refine():
    """A candidate-only field must survive refinement (prod bug 2026-07-09).

    ``recode_events_df`` keeps only live-var_schema columns, so a candidate's
    new field was silently dropped; ``_reattach_contract_columns`` merges the
    raw flattened values back in.
    """
    live = tomllib.loads(ac._read_baked_text())
    cand = copy.deepcopy(live)
    cand["fields"].append({"name": "funniness", "section": "scoring",
                           "scale": "text", "desc": "How funny the video is."})
    records = []
    for i, item in enumerate(["11111", "22222"]):
        flat = {"item_id": item}
        flat.update(sch.flatten_structured(_fake_parsed(cand, seed=i), cand))
        records.append(flat)
    dropped = ab_eval.refine_from_flat_dicts(records)
    reattached = ab_eval._reattach_contract_columns(dropped, records, cand)
    ok = ("funniness" not in dropped.columns          # documents the recode drop
          and "funniness" in reattached.columns
          and reattached["funniness"].notna().all()
          and len(reattached) == 2
          and "transcript_no_repetitions" in reattached.columns)
    _check("test_candidate_field_survives_refine", bool(ok),
           f"dropped={'funniness' in dropped.columns} cols={[c for c in reattached.columns if 'funn' in c]}")


def test_resolve_items_unknown_ids():
    resolved = ab_eval.resolve_items(["__definitely_not_an_item__", "also-nope"])
    ok = (len(resolved) == 2
          and all(r["platform"] is None and r["downloaded"] is None for r in resolved))
    _check("test_resolve_items_unknown_ids", ok, f"resolved={resolved}")


def test_refine_from_flat_dicts():
    live = tomllib.loads(ac._read_baked_text())
    records = []
    for i, item in enumerate(["11111", "22222", "33333"]):
        flat = {"item_id": item}
        flat.update(sch.flatten_structured(_fake_parsed(live, seed=i), live))
        records.append(flat)
    df = ab_eval.refine_from_flat_dicts(records)
    ok = (len(df) == 3 and "item_id" in df.columns
          and "annotated_ok" in df.columns and bool(df["annotated_ok"].all()))
    _check("test_refine_from_flat_dicts", ok,
           f"cols={sorted(df.columns)[:8]}... n={len(df)}")


def test_compare_arms_metrics():
    a = pd.DataFrame({
        "item_id": ["1", "2", "3", "4"],
        "score": [10, 20, 30, 40],
        "category": ["Cat", "Dog", "Cat", "Bird"],
        "tags": [["a", "b"], ["c"], ["d"], ["e", "f"]],
        "essay": ["long text about something interesting " * 3] * 4,
    })
    b = pd.DataFrame({
        "item_id": ["1", "2", "3", "4"],
        "score": [12, 22, 28, 41],                    # highly correlated
        "category": ["Cat", "Dog", "Bird", "Bird"],   # 3/4 agree
        "tags": [["a", "b"], ["c"], ["x"], ["e"]],
        "essay": ["different long text about other things " * 3] * 4,
    })
    report = ab_eval.compare_arms(a, b)
    cols = report["columns"]
    ok = (report["n_items"] == 4
          and cols["score"]["kind"] == "numeric" and cols["score"]["correlation"] > 0.9
          and cols["category"]["kind"] == "enum" and abs(cols["category"]["agreement"] - 0.75) < 1e-9
          and cols["tags"]["kind"] == "list" and 0 < cols["tags"]["mean_jaccard"] < 1
          and cols["essay"]["kind"] == "freetext")
    _check("test_compare_arms_metrics", ok, f"cols={ {k: v['kind'] for k, v in cols.items()} }")


def test_adjudication_and_distributions():
    a = pd.DataFrame({"item_id": ["1", "2"], "category": ["Cat", "Dog"]})
    b = pd.DataFrame({"item_id": ["1", "2"], "category": ["Cat", "Bird"]})
    adj = ab_eval.build_adjudication({"a": a, "b": b}, ["category"])
    dist = ab_eval.distribution_tables({"a": a, "b": b}, "category")
    ok = (len(adj) == 1 and adj[0]["item_id"] == "2"
          and adj[0]["values"] == {"a": "Dog", "b": "Bird"}
          and dist["arms"]["a"] == {"Cat": 1, "Dog": 1})
    _check("test_adjudication_and_distributions", ok, f"adj={adj}")


# ------- execute_run plumbing + ISOLATION GUARD -------

def test_execute_run_isolation():
    live_text = ac._read_baked_text()
    live = tomllib.loads(live_text)
    cand = copy.deepcopy(live)
    cand["prompt"]["footer"] = "CANDIDATE FOOTER (ab_eval unit test)"
    cand["fields"].append({"name": "funniness", "section": "scoring",
                           "scale": "text", "desc": "How funny the video is."})
    cand_text = ac.serialize_contract(cand, base_text=live_text)

    # Canned responses conform to the CANDIDATE schema (superset of live) —
    # each arm's flatten then keeps only its own contract's fields.
    parsed = {i: _fake_parsed(cand, seed=int(i)) for i in ("1", "2", "3")}
    runner = StubRunner(parsed)

    # Record every data_io write (location, filename) while delegating to the
    # real functions — the artifacts land in the isolated local ab_eval dir.
    writes: list[tuple[str, str]] = []
    orig_save_json, orig_save_parquet = data_io.save_json, data_io.save_parquet

    def rec_save_json(*args, **kwargs):
        writes.append((kwargs.get("storage_location"), kwargs.get("filename")))
        return orig_save_json(*args, **kwargs)

    def rec_save_parquet(*args, **kwargs):
        writes.append((kwargs.get("storage_location"), kwargs.get("filename")))
        return orig_save_parquet(*args, **kwargs)

    run_id = ab_eval.new_run_id()
    data_io.save_json, data_io.save_parquet = rec_save_json, rec_save_parquet
    try:
        summary = ab_eval.execute_run(
            run_id=run_id,
            arms=[{"name": "live", "source": "live", "text": live_text},
                  {"name": "cand", "source": "candidate", "text": cand_text}],
            item_ids=["1", "2", "3"],
            started_by="tester",
            runner=runner,
        )
    finally:
        data_io.save_json, data_io.save_parquet = orig_save_json, orig_save_parquet

    bad_writes = [w for w in writes if w[0] != ab_eval.LOCATION]
    run = ab_eval.load_run(run_id)
    report = run.get("report") or {}
    manifest = run.get("manifest") or {}
    rows = ab_eval.load_run_rows(run_id, "cand")
    live_rows = ab_eval.load_run_rows(run_id, "live")
    index_entry = next((r for r in ab_eval.load_runs_index() if r["run_id"] == run_id), None)

    ok = (summary["status"] == "complete"
          and not bad_writes
          and manifest.get("status") == "complete"
          and set(report.get("arms", [])) == {"live", "cand"}
          and "live|cand" in report.get("comparisons", {})
          and report["comparisons"]["live|cand"]["n_items"] == 3
          and report["costs"]["cand"]["total_tokens"] == 45
          and len(rows) == 3
          # The candidate-only field survives end-to-end (refine reattach)…
          and all("funniness" in r for r in rows)
          # …and does not leak into the live arm.
          and all("funniness" not in r for r in live_rows)
          and index_entry and index_entry["status"] == "complete"
          and {a["source"] for a in manifest["arms"]} == {"live", "candidate"})

    deleted = ab_eval.delete_run(run_id)
    gone = ab_eval.load_run(run_id).get("manifest") is None or \
        not data_io.exists(storage_location=ab_eval.LOCATION,
                           filename=f"runs/{run_id}/manifest.json")
    _check("test_execute_run_isolation", bool(ok and deleted and gone),
           f"bad_writes={bad_writes} status={summary.get('status')}")


def test_execute_run_cap():
    ok = False
    try:
        ab_eval.execute_run("capcheck", [{"name": "x", "source": "live", "text": ac._read_baked_text()}],
                            [str(i) for i in range(ab_eval.MAX_EVAL_ITEMS + 1)])
    except ValueError as e:
        ok = "cap" in str(e)
    _check("test_execute_run_cap", ok)


def test_source_isolation_guard():
    """Static guard: every data_io write in fyp/ab_eval.py targets an allowed location.

    Save calls may only use the module's own location CONSTANTS (never a string
    literal — so a literal ``"recoded"`` / ``"machine_annotations_raw"`` can
    never slip in), and the production archive save entry points are never
    referenced.
    """
    source = (project_root / "fyp" / "ab_eval.py").read_text()
    allowed = {"LOCATION", "CANDIDATES_LOCATION", "EVAL_SET_LOCATION"}
    save_calls = re.findall(
        r"data_io\.save_\w+\([^)]*?storage_location=([\"']?\w+[\"']?)", source, re.DOTALL)
    bad_saves = [loc for loc in save_calls if loc not in allowed]
    forbidden_calls = ("consolidate_and_save_refined_annotations",
                       "refine_one_raw_annotation_batch",
                       "rebuild_active_annotations_from_archive")
    call_hits = [c for c in forbidden_calls if c in source]
    ok = save_calls and not bad_saves and not call_hits
    _check("test_source_isolation_guard", bool(ok),
           f"bad_saves={bad_saves} call_hits={call_hits}")


# ------- web-layer smoke (route wiring; no Gemini) -------

def test_api_smoke():
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User

    admin = "__abe_test_admin__"
    orig = security.user_manager.get_user

    def _fake_get(uid):
        if uid == admin:
            return User(username=admin, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig(uid)

    security.user_manager.get_user = _fake_get
    set_snap = ab_eval.load_eval_set()
    try:
        from web_interface.fyp_data_hub import app
        app.testing = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_user_id"] = admin
                sess["_fresh"] = True

            # Candidate CRUD through the API.
            r1 = client.post("/api/manage/ab-candidates",
                             json={"name": "api-smoke", "text": ac._read_baked_text(),
                                   "overwrite": True})
            r2 = client.get("/api/manage/ab-candidates")
            r3 = client.get("/api/manage/ab-candidates/api-smoke")
            r4 = client.post("/api/manage/ab-candidates/api-smoke/activate")
            bad = client.post("/api/manage/ab-candidates",
                              json={"name": "Bad Name!", "text": ac._read_baked_text()})

            # Eval set + estimate.
            r5 = client.post("/api/manage/ab-eval-set", json={"item_ids": ["111", "222"]})
            r6 = client.get("/api/manage/ab-eval-set")
            r7 = client.post("/api/manage/ab-eval/estimate",
                             json={"candidate_names": ["api-smoke"], "include_live": True})
            r8 = client.get("/api/manage/ab-eval/runs")

            rdel = client.delete("/api/manage/ab-candidates/api-smoke")

            b4 = r4.get_json() or {}
            b7 = r7.get_json() or {}
            ok = (r1.status_code == 200
                  and "api-smoke" in [m["name"] for m in (r2.get_json() or {}).get("candidates", [])]
                  and r3.status_code == 200
                  and r4.status_code == 200 and b4.get("text") and "impact" in b4
                  and b4["impact"].get("metadata_only") is True
                  and bad.status_code == 400
                  and r5.status_code == 200
                  and (r6.get_json() or {}).get("item_ids") == ["111", "222"]
                  and b7 == {"n_items": 2, "n_arms": 2, "n_calls": 4,
                             "max_items": ab_eval.MAX_EVAL_ITEMS}
                  and r8.status_code == 200
                  and rdel.status_code == 200)
            _check("test_api_smoke", bool(ok),
                   f"codes={[r.status_code for r in (r1, r2, r3, r4, bad, r5, r6, r7, r8, rdel)]}")
    finally:
        security.user_manager.get_user = orig
        ab_eval.save_eval_set(set_snap.get("item_ids", []), actor=set_snap.get("updated_by") or "",
                              note=set_snap.get("note") or "")
        ab_eval.delete_candidate("api-smoke")
        try:
            if data_io.exists(storage_location="users", filename="__abe_test_admin___log.json"):
                data_io.remove(storage_location="users", filename="__abe_test_admin___log.json")
        except Exception:
            pass


def main():
    print("\nRunning ab_eval tests...\n")
    ab_eval.ensure_locations()
    tests = [
        test_candidate_name_validation,
        test_candidate_crud,
        test_eval_set_cap_and_dedupe,
        test_sample_items_seeded,
        test_run_arm_threads_candidate_contract,
        test_run_arm_failed_item_yields_bare_row,
        test_candidate_field_survives_refine,
        test_resolve_items_unknown_ids,
        test_refine_from_flat_dicts,
        test_compare_arms_metrics,
        test_adjudication_and_distributions,
        test_execute_run_isolation,
        test_execute_run_cap,
        test_source_isolation_guard,
        test_api_smoke,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            print(f"  ERROR {t.__name__}  ({e})")
            import traceback
            traceback.print_exc()
    print(f"\nSummary: {PASS} passed, {FAIL} failed\n")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
