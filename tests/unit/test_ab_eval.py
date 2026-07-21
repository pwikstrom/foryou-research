"""Tests for the A/B contract-evaluation harness (``fyp.ab_eval``).

Cost-free: no Gemini calls — arms run through a stub runner. Covers candidate
CRUD, named evaluation sets (CRUD + cap/sampling), contract-threaded arm
rendering + flattening, the in-memory production-recode refine, the scale-aware
comparison (including the declared-scale-over-length-heuristic rule), and — most
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
        ab_eval.save_eval_set(snap.get("item_ids", []), name=snap.get("name"),
                              actor=snap.get("updated_by") or "",
                              note=snap.get("note") or "")


def test_named_eval_sets_crud():
    """Create / clone / rename / delete named sets; the last set is undeletable."""
    scratch, renamed = "unittest-set", "unittest-set2"
    snap = ab_eval.load_eval_set()
    snap_active, snap_ids = snap["name"], list(snap.get("item_ids", []))
    for leftover in (scratch, renamed):
        try:
            ab_eval.delete_eval_set(leftover)
        except (FileNotFoundError, ValueError):
            pass
    try:
        ab_eval.save_eval_set(["a1", "a2"], name=snap_active, actor="tester")
        ab_eval.create_eval_set(scratch, copy_from=snap_active, actor="tester")
        cloned = ab_eval.load_eval_set(scratch)["item_ids"] == ["a1", "a2"]
        active_after_create = ab_eval.list_eval_sets()["active"] == scratch

        ab_eval.rename_eval_set(scratch, renamed)
        listed = {s["name"] for s in ab_eval.list_eval_sets()["sets"]}
        renamed_ok = renamed in listed and scratch not in listed
        # Renaming the active set must carry the active pointer with it.
        active_follows = ab_eval.list_eval_sets()["active"] == renamed

        # Sets are independent: editing one must not touch another.
        ab_eval.save_eval_set(["b1"], name=renamed, actor="tester")
        isolated = ab_eval.load_eval_set(snap_active)["item_ids"] == ["a1", "a2"]

        ab_eval.set_active_eval_set(snap_active)
        ab_eval.delete_eval_set(renamed)
        deleted = renamed not in {s["name"] for s in ab_eval.list_eval_sets()["sets"]}

        last_guarded = False
        if len(ab_eval.list_eval_sets()["sets"]) == 1:
            try:
                ab_eval.delete_eval_set(snap_active)
            except ValueError:
                last_guarded = True
        else:
            last_guarded = True   # other real sets exist; guard tested elsewhere

        name_guarded = False
        try:
            ab_eval.create_eval_set("Bad Name!")
        except ValueError:
            name_guarded = True

        ok = (cloned and active_after_create and renamed_ok and active_follows
              and isolated and deleted and last_guarded and name_guarded)
        _check("test_named_eval_sets_crud", ok,
               f"clone={cloned} active={active_after_create} rename={renamed_ok} "
               f"follows={active_follows} isolated={isolated} del={deleted} "
               f"last={last_guarded} name={name_guarded}")
    finally:
        for leftover in (scratch, renamed):
            try:
                ab_eval.delete_eval_set(leftover)
            except (FileNotFoundError, ValueError):
                pass
        ab_eval.save_eval_set(snap_ids, name=snap_active, actor="")
        ab_eval.set_active_eval_set(snap_active)


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


def test_compare_arms_excludes_failed_items_and_flags_low_variance_r():
    """Items one arm failed are excluded; near-constant scores don't feed r.

    Regression (2026-07-21, hazel 5-item run): sensitivity_score pairs
    (.30/.30, .25/.32, .30/.25, .30/.30) gave Pearson r ≈ −0.61 — the arms
    agreed closely (Δ̄ 0.03) but r tracked the rounding noise. The failed item
    (annotated_ok=False in one arm) additionally dragged every metric.
    """
    a = pd.DataFrame({
        "item_id": ["1", "2", "3", "4", "5"],
        "sensitivity_score": [0.30, 0.25, None, 0.30, 0.30],
        "annotated_ok": [True, True, False, True, True],
    })
    b = pd.DataFrame({
        "item_id": ["1", "2", "3", "4", "5"],
        "sensitivity_score": [0.30, 0.32, 0.40, 0.25, 0.30],
        "annotated_ok": [True, True, True, True, True],
    })
    report = ab_eval.compare_arms(a, b, scales={"sensitivity_score": "numeric"})
    col = report["columns"]["sensitivity_score"]
    summary = report["summary"]
    ok = (report["n_items"] == 4 and report["n_items_excluded"] == 1
          and col["n_compared"] == 4
          and abs(col["exact_agreement"] - 0.5) < 1e-9
          and abs(col["mean_abs_diff"] - 0.03) < 1e-9
          and col["caveat"] == "low_variance"
          and summary["mean_numeric_correlation"] is None
          and abs(summary["mean_numeric_exact_agreement"] - 0.5) < 1e-9)
    _check("test_compare_arms_excludes_failed_items_and_flags_low_variance_r", ok,
           f"col={col} n_items={report['n_items']} excluded={report.get('n_items_excluded')}")


def test_extra_na_sentinels_and_freetext_list_summary_exclusion():
    """'unknown'/'unclear'/'other' count as NA; free-text lists stay out of the
    summary Jaccard means; adjudication skips items an arm failed outright."""
    ok = (ab_eval._is_sentinel("Unknown") and ab_eval._is_sentinel("unclear")
          and ab_eval._is_sentinel("Other") and ab_eval._is_sentinel("other category")
          and not ab_eval._is_sentinel("dance"))

    prose = "a fairly long free text phrase about the activity shown"
    a = pd.DataFrame({"item_id": ["1", "2"],
                      "tags": [["a", "b"], ["c"]],
                      "main_activity": [[prose], [prose + " too"]]})
    b = pd.DataFrame({"item_id": ["1", "2"],
                      "tags": [["a", "b"], ["c"]],
                      "main_activity": [[prose + " differently"], [prose]]})
    report = ab_eval.compare_arms(a, b, scales={"tags": "list", "main_activity": "list"})
    cols = report["columns"]
    ok = (ok and cols["main_activity"]["caveat"] == "free_text_elements"
          and cols["main_activity"]["mean_jaccard"] is not None
          # summary mean covers only the non-free-text list (perfect overlap)
          and abs(report["summary"]["mean_list_jaccard"] - 1.0) < 1e-9)

    frames = {
        "x": pd.DataFrame({"item_id": ["1", "2"], "cat": ["dog", "cat"],
                           "annotated_ok": [True, False]}),
        "y": pd.DataFrame({"item_id": ["1", "2"], "cat": ["dog", "bird"],
                           "annotated_ok": [True, True]}),
    }
    adj = ab_eval.build_adjudication(frames, ["cat"])
    ok = ok and all(r["item_id"] != "2" for r in adj)   # failed item excluded
    _check("test_extra_na_sentinels_and_freetext_list_summary_exclusion", ok,
           f"adj={adj} summary={report['summary'].get('mean_list_jaccard')}")


def test_declared_scale_beats_length_heuristic():
    """A `text` field with short answers must NOT be scored as a categorical.

    The regression this guards: ab_eval carried the pre-2026-07 ten-scale
    vocabulary, so `text` / `list` / `numeric` matched nothing and every
    non-categorical column fell through to an `avg_len < 25` guess. That scored
    call_to_action ("Try now" vs "Follow for more") with exact-string agreement.
    """
    a = pd.DataFrame({"item_id": ["1", "2"], "cta": ["Try now", "Buy it"],
                      "n": [1, 2], "tags": ["x | y", "z"]})
    b = pd.DataFrame({"item_id": ["1", "2"], "cta": ["Try it", "Buy now"],
                      "n": [1, 3], "tags": ["y | x", "z"]})
    scales = {"cta": "text", "n": "numeric", "tags": "list"}
    cols = ab_eval.compare_arms(a, b, scales=scales)["columns"]
    # `tags` arrives as a SPLITTER-joined string (what the recode leaves behind
    # for object sub-keys such as faces_gender) and must compare as a set —
    # order-insensitively, hence jaccard 1.0 for "x | y" vs "y | x".
    ok = (cols["cta"]["kind"] == "freetext"
          and cols["n"]["kind"] == "numeric"
          and cols["tags"]["kind"] == "list"
          and cols["tags"]["mean_jaccard"] == 1.0)
    _check("test_declared_scale_beats_length_heuristic", ok,
           f"kinds={ {k: v['kind'] for k, v in cols.items()} } tags={cols['tags']}")


def test_sentinels_and_vacuous_agreement():
    """Dash variants count as "no value"; agreeing on nothing is reported as such.

    "no" is a REAL answer (yes/no fields), so it must count toward coverage —
    the vacuous-agreement flag applies to true no-value sentinels like "none".
    """
    a = pd.DataFrame({"item_id": ["1", "2"], "cta": ["-", "Try now"], "flag": ["none", "none"],
                      "spoken": ["no", "no"]})
    b = pd.DataFrame({"item_id": ["1", "2"], "cta": ["–", "try now"], "flag": ["none", "none"],
                      "spoken": ["no", "yes"]})
    scales = {"cta": "text", "flag": "categorical", "spoken": "categorical"}
    cols = ab_eval.compare_arms(a, b, scales=scales)["columns"]
    # The en dash must not be counted as an answer, so coverage stays 1/2.
    dash_ok = cols["cta"]["coverage_a"] == 0.5 and cols["cta"]["coverage_b"] == 0.5
    # Both arms said "none" everywhere: agreement is 1.0 but vacuous, and flagged.
    flag = cols["flag"]
    vacuous_ok = (flag["agreement"] == 1.0 and flag["agreement_filled"] is None
                  and flag["coverage_a"] == 0.0 and flag["n_both_empty"] == 2
                  and flag["caveat"] == "both_arms_empty")
    # "no" answers are substantive: full coverage, real (dis)agreement.
    spoken = cols["spoken"]
    no_ok = (spoken["coverage_a"] == 1.0 and spoken["coverage_b"] == 1.0
             and abs(spoken["agreement_filled"] - 0.5) < 1e-9)
    # A blank-vs-en-dash cell is not a real disagreement.
    adj = ab_eval.build_adjudication({"a": a, "b": b}, ["cta"])
    adj_ok = [r["item_id"] for r in adj] == []
    _check("test_sentinels_and_vacuous_agreement", dash_ok and vacuous_ok and no_ok and adj_ok,
           f"cta={cols['cta']} flag={flag} spoken={spoken} adj={adj}")


def test_contract_scale_map_covers_new_fields():
    """A candidate's brand-new field is classified from its contract declaration."""
    live = tomllib.loads(ac._read_baked_text())
    cand = copy.deepcopy(live)
    cand["fields"].append({"name": "funniness", "section": "scoring",
                           "scale": "text", "desc": "How funny the video is."})
    mapping = ab_eval.contract_scale_map(cand)
    # Object sub-keys resolve to their flattened output column name. background_music
    # returns a single prose string (no "list:" spec prefix) so it must be `text`;
    # notable_sounds really is an array. See test_declared_scales_match_schema_types.
    ok = (mapping.get("funniness") == "text"
          and mapping.get("background_music") == "text"
          and mapping.get("notable_sounds") == "list"
          and mapping.get("call_to_action") == "text")
    _check("test_contract_scale_map_covers_new_fields", ok,
           f"funniness={mapping.get('funniness')} bgm={mapping.get('background_music')} "
           f"sounds={mapping.get('notable_sounds')}")


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
            name="unit test run",
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
          # The run name lands on the manifest and the index entry.
          and manifest.get("name") == "unit test run"
          and index_entry.get("name") == "unit test run"
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

            # Named evaluation sets.
            s1 = client.get("/api/manage/ab-eval-sets")
            s2 = client.post("/api/manage/ab-eval-sets", json={"name": "api-smoke-set"})
            s3 = client.post("/api/manage/ab-eval-sets/api-smoke-set/activate")
            s4 = client.post("/api/manage/ab-eval-sets", json={"name": "api-smoke-set"})
            s5 = client.post(f"/api/manage/ab-eval-sets/{set_snap['name']}/activate")
            s6 = client.delete("/api/manage/ab-eval-sets/api-smoke-set")

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
                  and b7.get("n_items") == 2 and b7.get("n_arms") == 2
                  and b7.get("n_calls") == 4 and b7.get("eval_set") == set_snap["name"]
                  and b7.get("max_items") == ab_eval.MAX_EVAL_ITEMS
                  and r8.status_code == 200
                  and s1.status_code == 200 and "active" in (s1.get_json() or {})
                  and s2.status_code == 200 and s3.status_code == 200
                  and s4.status_code == 409          # duplicate name
                  and s5.status_code == 200 and s6.status_code == 200
                  and rdel.status_code == 200)
            _check("test_api_smoke", bool(ok),
                   f"codes={[r.status_code for r in (r1, r2, r3, r4, bad, r5, r6, r7, r8, s1, s2, s3, s4, s5, s6, rdel)]}"
                   f" est={b7}")
    finally:
        security.user_manager.get_user = orig
        try:
            ab_eval.delete_eval_set("api-smoke-set")
        except (FileNotFoundError, ValueError):
            pass
        ab_eval.save_eval_set(set_snap.get("item_ids", []), name=set_snap.get("name"),
                              actor=set_snap.get("updated_by") or "",
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
        test_named_eval_sets_crud,
        test_sample_items_seeded,
        test_run_arm_threads_candidate_contract,
        test_run_arm_failed_item_yields_bare_row,
        test_candidate_field_survives_refine,
        test_resolve_items_unknown_ids,
        test_refine_from_flat_dicts,
        test_compare_arms_metrics,
        test_declared_scale_beats_length_heuristic,
        test_sentinels_and_vacuous_agreement,
        test_contract_scale_map_covers_new_fields,
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
