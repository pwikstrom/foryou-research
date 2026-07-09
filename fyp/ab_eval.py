"""A/B evaluation harness for annotation contracts.

Runs one or more *candidate* annotation contracts (plus optionally the live
one) against a fixed, admin-curated evaluation set of already-downloaded
videos via real Gemini calls, refines each arm's raw responses through the
production recode pipeline **in memory**, and stores per-arm results plus a
field-type-aware comparison report — all in an isolated storage location.

Productionizes the ``tests/ab_eval`` spike seams: the per-video
annotate-without-persist call (``annotate_one``), the in-memory refine
(``refine_from_flat_dicts``), and the scale-aware comparison
(``compare_arms``), extended to thread an explicit contract through
``build_prompt`` / ``build_response_schema`` / ``flatten_structured``.

ISOLATION RULE: this module only ever writes run artifacts to the dedicated
``ab_eval`` location (plus the two admin config stores: the candidate TOMLs in
``ab_candidates`` and ``users/ab_eval_set.json``). It must never write to
``machine_annotations_*`` or ``recoded`` — eval results do not exist as far as
studies are concerned. ``tests/unit/test_ab_eval.py`` guards this.
"""

import contextlib
import datetime as _dt
import io
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp import annotation_contract as ac
from fyp import annotation_schema as sch
from fyp.fyp_config import fyp_cf
from fyp.types import convert_dtypes_to_pyarrow

# NOTE: fyp.machine_annotation, fyp.recode_variables and google.genai are
# imported lazily inside the functions that need them — they are heavy imports
# the candidate/eval-set CRUD endpoints should not pay for.

LOCATION = "ab_eval"                      # run artifacts (isolated)
CANDIDATES_LOCATION = "ab_candidates"     # candidate contract TOMLs (admin config)
EVAL_SET_LOCATION = "users"               # the curated eval-set id list
EVAL_SET_FILENAME = "ab_eval_set.json"
RUNS_INDEX_FILENAME = "runs_index.json"

MAX_EVAL_ITEMS = 50                       # hard cost cap (endpoints + worker)
MAX_WORKERS = 4
ADJUDICATION_CAP = 3000

CANDIDATE_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,40}$")

# Sentinels a normalized cell may collapse to that mean "no real value".
_NORM_SENTINELS = {"unable to detect", "-", "other category", "not coded", "", "<NA>", "no"}

_NUMERIC_SCALES = {"ratio", "interval", "ordinal"}
_ENUM_SCALES = {"categorical", "dichotomous", "factor"}
_LIST_SCALES = {"collection"}




class RunCancelled(Exception):
    """Raised inside :func:`execute_run` when the reporter requests cancel."""




def ensure_locations() -> None:
    """Register the harness's storage locations (idempotent).

    ``ab_eval`` holds run artifacts under ``<local_data>/ab_eval``;
    ``ab_candidates`` holds candidate contract TOMLs under
    ``<local_data>/users/ab_candidates`` (admin config, next to the other
    ``users`` stores).
    """
    local_data = fyp_cf["paths"]["local_data"]
    data_io.register_location(LOCATION, os.path.join(local_data, "ab_eval"))
    data_io.register_location(
        CANDIDATES_LOCATION, os.path.join(local_data, "users", "ab_candidates")
    )




# ---------------------------------------------------------------------------
# Candidate contracts (named TOMLs; byte-identical to what the upload flow
# consumes, so "activate candidate" is literally re-posting its text).
# ---------------------------------------------------------------------------


def _candidate_files(name: str) -> tuple[str, str]:
    """Return the (toml, meta) filenames for a candidate."""
    return f"{name}.toml", f"{name}.meta.json"




def validate_candidate_name(name: str) -> bool:
    """Return True when ``name`` is a legal candidate name."""
    return bool(isinstance(name, str) and CANDIDATE_NAME_RE.match(name))




def list_candidates() -> list[dict]:
    """Return every stored candidate's metadata, newest first."""
    ensure_locations()
    out: list[dict] = []
    try:
        files = data_io.listdir(storage_location=CANDIDATES_LOCATION)
    except Exception:
        return []
    for fn in files:
        if not fn.endswith(".meta.json"):
            continue
        try:
            meta = data_io.load_json(storage_location=CANDIDATES_LOCATION, filename=fn)
            if isinstance(meta, dict) and meta.get("name"):
                out.append(meta)
        except Exception:
            continue
    out.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return out




def load_candidate(name: str) -> dict:
    """Load one candidate: ``{name, text, contract, meta}``.

    Raises:
        FileNotFoundError: when the candidate does not exist.
        ValueError: when its stored TOML no longer validates (e.g. the live
            role/scale vocabularies changed under it).
    """
    ensure_locations()
    toml_fn, meta_fn = _candidate_files(name)
    text = data_io.load_text(storage_location=CANDIDATES_LOCATION, filename=toml_fn)
    if text is None:
        raise FileNotFoundError(f"candidate '{name}' not found")
    contract, errors = ac.parse_and_validate(text)
    if errors:
        raise ValueError(f"candidate '{name}' is no longer valid: {'; '.join(errors)}")
    meta: dict = {}
    try:
        meta = data_io.load_json(storage_location=CANDIDATES_LOCATION, filename=meta_fn) or {}
    except Exception:
        pass
    return {"name": name, "text": text, "contract": contract, "meta": meta}




def save_candidate(
    name: str, text: str, actor: str = "", note: str = "", overwrite: bool = False,
    candidate_version: str | None = None,
) -> dict:
    """Validate and store a candidate contract; return its metadata.

    Args:
        name: candidate name (``^[a-z0-9_\\-]{1,40}$``).
        text: the candidate's TOML text (stored verbatim).
        actor: the admin performing the save (audit).
        note: free-text note shown in the candidates table.
        overwrite: allow replacing an existing candidate.
        candidate_version: the pre-computed ``av_`` preview hash (the route
            computes it via the impact helper; optional).

    Raises:
        ValueError: on an illegal name or invalid contract text.
        FileExistsError: when the candidate exists and ``overwrite`` is False.
    """
    ensure_locations()
    if not validate_candidate_name(name):
        raise ValueError(
            "candidate name must be 1-40 chars of lowercase letters, digits, '_' or '-'"
        )
    contract, errors = ac.parse_and_validate(text)
    if contract is None or errors:
        raise ValueError("invalid contract: " + "; ".join(errors))
    toml_fn, meta_fn = _candidate_files(name)
    if not overwrite and data_io.exists(storage_location=CANDIDATES_LOCATION, filename=toml_fn):
        raise FileExistsError(f"candidate '{name}' already exists")
    meta = {
        "name": name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "created_by": actor,
        "note": note,
        "etag": ac._etag(text, "candidate"),
        "candidate_version": candidate_version,
        "n_fields": len(contract.get("fields", [])),
    }
    data_io.save_text(text, storage_location=CANDIDATES_LOCATION, filename=toml_fn)
    data_io.save_json(data=meta, storage_location=CANDIDATES_LOCATION, filename=meta_fn)
    return meta




def delete_candidate(name: str) -> bool:
    """Delete a candidate's TOML + meta; return True when something was removed."""
    ensure_locations()
    removed = False
    for fn in _candidate_files(name):
        try:
            if data_io.exists(storage_location=CANDIDATES_LOCATION, filename=fn):
                data_io.remove(storage_location=CANDIDATES_LOCATION, filename=fn)
                removed = True
        except Exception:
            pass
    return removed




# ---------------------------------------------------------------------------
# Eval set (curated item-id list + platform/downloaded resolution + sampling).
# ---------------------------------------------------------------------------


def load_eval_set() -> dict:
    """Return the persisted eval set: ``{item_ids, updated_at, updated_by, note}``."""
    try:
        if data_io.exists(storage_location=EVAL_SET_LOCATION, filename=EVAL_SET_FILENAME):
            stored = data_io.load_json(
                storage_location=EVAL_SET_LOCATION, filename=EVAL_SET_FILENAME
            )
            if isinstance(stored, dict):
                stored.setdefault("item_ids", [])
                return stored
    except Exception:
        pass
    return {"item_ids": [], "updated_at": None, "updated_by": None, "note": ""}




def save_eval_set(item_ids: list[str], actor: str = "", note: str = "") -> dict:
    """Persist the curated eval set (deduped, order-preserving, capped).

    Raises:
        ValueError: when more than ``MAX_EVAL_ITEMS`` ids are given.
    """
    ids = list(dict.fromkeys(str(i).strip() for i in item_ids if str(i).strip()))
    if len(ids) > MAX_EVAL_ITEMS:
        raise ValueError(f"eval set is capped at {MAX_EVAL_ITEMS} items (got {len(ids)})")
    stored = {
        "item_ids": ids,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "updated_by": actor,
        "note": note,
    }
    data_io.save_json(data=stored, storage_location=EVAL_SET_LOCATION, filename=EVAL_SET_FILENAME)
    return stored




# Short-lived in-process cache for the enrichment-status columns. The eval-set
# UI hits resolve/sample several times in a row (page load, add, sample, save);
# on Cloud Run each uncached call re-downloads a multi-million-row parquet from
# GCS, which is what made the buttons feel stuck.
_STATUS_CACHE: dict = {"ts": 0.0, "frame": None}
_STATUS_TTL_S = 60.0




def _enrichment_status_frame() -> pd.DataFrame | None:
    """Load the id/platform/flags columns of enrichment_status.parquet, or None.

    Cached in-process for ``_STATUS_TTL_S`` seconds; ``item_id`` is normalised
    to ``str`` once at load so callers never re-cast the full column.
    """
    now = time.monotonic()
    if _STATUS_CACHE["frame"] is not None and now - _STATUS_CACHE["ts"] < _STATUS_TTL_S:
        return _STATUS_CACHE["frame"]
    try:
        if not data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            return None
        frame = data_io.load_parquet_selective(
            storage_location="recoded", filename="enrichment_status.parquet",
            columns=["item_id", "source_platform", "video_downloaded", "scraped_ok"],
        )
        frame["item_id"] = frame["item_id"].astype(str)
        _STATUS_CACHE["frame"] = frame
        _STATUS_CACHE["ts"] = now
        return frame
    except Exception:
        return None




def resolve_items(item_ids: list[str]) -> list[dict]:
    """Resolve each id to ``{item_id, platform, downloaded}`` for the UI.

    Vectorised: filters the status frame down to the requested ids before any
    per-row work (the frame has millions of rows; the eval set has ≤50). Ids
    absent from ``enrichment_status.parquet`` get ``platform=None`` /
    ``downloaded=None`` (unknown — likely a typo, or an item not yet ingested).
    """
    lookup: dict[str, tuple] = {}
    status = _enrichment_status_frame()
    if status is not None and len(status):
        wanted = {str(i) for i in item_ids}
        sub = status[status["item_id"].isin(wanted)]
        for row in sub.itertuples(index=False):
            plat = str(row.source_platform) if pd.notna(row.source_platform) else None
            dl = bool(row.video_downloaded) if pd.notna(row.video_downloaded) else None
            lookup[row.item_id] = (plat, dl)
    out = []
    for item_id in item_ids:
        plat, dl = lookup.get(str(item_id), (None, None))
        out.append({"item_id": str(item_id), "platform": plat, "downloaded": dl})
    return out




def sample_items(n: int, platforms: list[str] | None = None, seed: int | None = None) -> list[str]:
    """Sample up to ``n`` downloaded item ids, stratified by platform.

    Draws proportionally to each platform's share among rows with
    ``video_downloaded & scraped_ok`` (restricted to ``platforms`` when given).
    Returns the ids without persisting anything — the UI merges/edits and then
    saves the set explicitly.
    """
    n = max(1, min(int(n), MAX_EVAL_ITEMS))
    status = _enrichment_status_frame()
    if status is None or status.empty:
        return []
    pool = status.copy()
    pool["item_id"] = pool["item_id"].astype(str)
    if "video_downloaded" in pool.columns:
        pool = pool[pool["video_downloaded"].fillna(False).astype(bool)]
    if "scraped_ok" in pool.columns:
        pool = pool[pool["scraped_ok"].fillna(False).astype(bool)]
    if "source_platform" in pool.columns and platforms:
        pool = pool[pool["source_platform"].astype(str).isin([str(p) for p in platforms])]
    if pool.empty:
        return []

    rng = np.random.default_rng(seed)
    if "source_platform" not in pool.columns or pool["source_platform"].isna().all():
        idx = rng.choice(len(pool), size=min(n, len(pool)), replace=False)
        return pool["item_id"].iloc[sorted(idx)].tolist()

    groups = list(pool.groupby(pool["source_platform"].astype(str)))
    total = len(pool)
    picked: list[str] = []
    # Proportional allocation, largest groups first; remainder tops up below.
    for plat, grp in sorted(groups, key=lambda g: -len(g[1])):
        quota = max(1, round(n * len(grp) / total))
        quota = min(quota, len(grp), n - len(picked))
        if quota <= 0:
            continue
        idx = rng.choice(len(grp), size=quota, replace=False)
        picked.extend(grp["item_id"].iloc[sorted(idx)].tolist())
    if len(picked) < n:
        remaining = pool[~pool["item_id"].isin(picked)]
        extra = min(n - len(picked), len(remaining))
        if extra > 0:
            idx = rng.choice(len(remaining), size=extra, replace=False)
            picked.extend(remaining["item_id"].iloc[sorted(idx)].tolist())
    return picked[:n]




# ---------------------------------------------------------------------------
# Per-arm annotation (annotate-without-persist + runner abstraction).
# ---------------------------------------------------------------------------


def _build_contents(item_id: str, platform: str | None):
    """Build the Gemini ``contents`` for one item via the media resolver.

    Uses ``media_paths.resolve_media`` (platform subpath + legacy-flat
    fallback), returning GCS-URI parts or inline local bytes exactly like the
    production ``call_machine`` path.

    Raises:
        FileNotFoundError: when no media object resolves for the item.
    """
    import google.genai.types as gt

    from fyp import media_paths

    resolved = media_paths.resolve_media(item_id, platform=platform)
    if resolved is None:
        raise FileNotFoundError(f"no media found for item {item_id}")
    prompt_part = gt.Part.from_text(text="Analyze this video")
    if resolved["kind"] == "gcs":
        uri = f"gs://{resolved['bucket_name']}/{resolved['blob_name']}"
        return [gt.Part.from_uri(file_uri=uri, mime_type="video/mp4"), prompt_part]
    with open(resolved["path"], "rb") as handle:
        video_bytes = handle.read()
    return [gt.Part(inline_data=gt.Blob(data=video_bytes, mime_type="video/mp4")), prompt_part]




def annotate_one(item_id: str, platform: str | None, prompt_text: str, response_schema) -> dict:
    """Annotate ONE video against an explicit prompt + response schema.

    The A/B analogue of ``machine_annotation.call_machine``: same client, same
    ``[machine]`` generation parameters (so per-call cost matches production),
    but the prompt/schema come from the caller's candidate contract and the
    result is returned — NEVER persisted, stamped, or queued.

    Returns:
        ``{item_id, model, parsed, response, finish_reason, usage,
        inference_duration, error}`` (``parsed`` is None on any failure).
    """
    import google.genai.types as gt

    from fyp.machine_annotation import initialize_machine

    initialize_machine()
    machine = fyp_cf["machine"]
    config = gt.GenerateContentConfig(
        system_instruction=prompt_text,
        temperature=machine["temperature"],
        max_output_tokens=machine["max_output_tokens"],
        response_mime_type="application/json",
        response_schema=response_schema,
        thinking_config=gt.ThinkingConfig(thinking_budget=machine["thinking_budget"]),
    )

    out: dict = {
        "item_id": str(item_id),
        "model": machine["model"],
        "parsed": None,
        "response": "",
        "finish_reason": "did not start",
        "usage": {},
        "inference_duration": -1.0,
        "error": "",
    }

    try:
        contents = _build_contents(item_id, platform)
    except Exception as exc:
        out["error"] = f"contents: {exc}"
        out["finish_reason"] = "DNF - media not found"
        return out

    start = _dt.datetime.now()
    try:
        resp = machine["client"].models.generate_content(
            model=machine["model"], config=config, contents=contents,
        )
    except Exception as exc:
        out["error"] = f"generate: {exc}"
        out["finish_reason"] = "DNF - see error"
        out["inference_duration"] = (_dt.datetime.now() - start).total_seconds()
        return out

    out["inference_duration"] = (_dt.datetime.now() - start).total_seconds()
    try:
        out["finish_reason"] = str(resp.candidates[0].finish_reason)
    except (IndexError, AttributeError, TypeError):
        out["finish_reason"] = "unknown"

    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        out["usage"] = {
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "candidates_tokens": getattr(usage, "candidates_token_count", None),
            "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
            "total_tokens": getattr(usage, "total_token_count", None),
        }

    try:
        out["response"] = resp.text or ""
        out["parsed"] = json.loads(out["response"]) if out["response"] else None
    except Exception as exc:
        out["error"] = f"parse: {exc}"
    return out




class SyncThreadedRunner:
    """Synchronous arm runner: ThreadPoolExecutor over :func:`annotate_one`.

    The only runner today. A future ``BatchApiRunner`` (Gemini batch API, ~50%
    cheaper, async) can implement the same ``run()`` signature and slot into
    :func:`run_arm` without touching the worker.
    """

    def __init__(self, max_workers: int = MAX_WORKERS, cancel_cb=None):
        self.max_workers = max_workers
        self.cancel_cb = cancel_cb




    def run(self, prompt_text: str, response_schema, item_ids: list[str],
            platform_map: dict[str, str], progress_cb=None) -> list[dict]:
        """Annotate every item; returns raw rows in ``item_ids`` order.

        Raises:
            RunCancelled: when ``cancel_cb`` returns True between completions.
        """
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(
                    annotate_one, item_id, platform_map.get(str(item_id)),
                    prompt_text, response_schema,
                ): str(item_id)
                for item_id in item_ids
            }
            done = 0
            for fut in as_completed(futures):
                item_id = futures[fut]
                try:
                    results[item_id] = fut.result()
                except Exception as exc:
                    results[item_id] = {
                        "item_id": item_id, "parsed": None, "response": "",
                        "finish_reason": "DNF - runner error", "usage": {},
                        "inference_duration": -1.0, "error": str(exc),
                        "model": fyp_cf["machine"].get("model"),
                    }
                done += 1
                if progress_cb:
                    progress_cb(done, len(item_ids))
                if self.cancel_cb and self.cancel_cb():
                    for pending in futures:
                        pending.cancel()
                    raise RunCancelled()
        return [results[str(i)] for i in item_ids if str(i) in results]




def run_arm(arm_name: str, contract: dict, item_ids: list[str],
            platform_map: dict[str, str], runner=None, progress_cb=None) -> tuple[list[dict], list[dict]]:
    """Run one contract arm over the eval set.

    Renders the arm's prompt + response schema ONCE from ``contract`` (the
    explicit-contract seams in ``annotation_schema``), fans out the calls via
    the runner, and flattens each parsed response against the same contract.

    Args:
        arm_name: label for logging only.
        contract: the arm's parsed contract dict.
        item_ids: eval-set ids.
        platform_map: ``{item_id: source_platform}`` for media resolution.
        runner: an object with the ``SyncThreadedRunner.run`` signature.
        progress_cb: ``(done, total)`` per completed item.

    Returns:
        ``(flat_rows, raw_rows)`` — flat rows feed
        :func:`refine_from_flat_dicts`; raw rows carry usage/errors for the
        report.
    """
    prompt_text = sch.build_prompt(contract)
    response_schema = sch.build_response_schema(contract)
    runner = runner or SyncThreadedRunner()
    raw_rows = runner.run(prompt_text, response_schema, item_ids, platform_map, progress_cb)

    flat_rows: list[dict] = []
    for raw in raw_rows:
        flat = {"item_id": str(raw.get("item_id"))}
        parsed = raw.get("parsed")
        if isinstance(parsed, dict):
            flat.update(sch.flatten_structured(parsed, contract))
        flat_rows.append(flat)
    return flat_rows, raw_rows




# ---------------------------------------------------------------------------
# In-memory refine + comparison (ports of tests/ab_eval/_ab_common.py).
# ---------------------------------------------------------------------------


def _normalize_cell(value) -> str:
    """Canonical, comparison-stable string for any cell value.

    Reimplementation of ``tests/golden/_harness._normalize_cell`` (``fyp/``
    must not import from ``tests/``): handles NA, lists/arrays, dicts and
    floats so pyarrow- and object-backed frames compare equal on content.
    """
    if isinstance(value, (list, tuple, np.ndarray)):
        return "[" + ", ".join(_normalize_cell(v) for v in value) + "]"
    if isinstance(value, dict):
        return json.dumps({k: _normalize_cell(v) for k, v in sorted(value.items())})
    try:
        if pd.isna(value):
            return "<NA>"
    except (TypeError, ValueError):
        pass
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.6f}"
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return str(int(value))
    return str(value)




def refine_from_flat_dicts(records: list[dict], quiet: bool = True) -> pd.DataFrame:
    """Run the production recode downstream on already-flattened row dicts.

    Identical chain to a real annotation batch (``consolidate_rare_columns`` →
    transcript de-dup → ``rename_columns`` → ``recode_events_df`` →
    ``clean_up_machine_annotations`` → flags → pyarrow), entirely in memory —
    nothing is saved. Drifts *with* production by construction.
    """
    import fyp.machine_annotation as ma
    from fyp.recode_variables import recode_events_df, rename_columns

    df = pd.DataFrame(records)
    sink = io.StringIO()
    ctx = contextlib.redirect_stdout(sink) if quiet else contextlib.nullcontext()
    with ctx:
        df = ma.consolidate_rare_columns_from_gemini_output(df)
        if "transcript" in df.columns:
            df = ma.remove_repetitions_from_transcripts(df)
        df = rename_columns(df)
        df = recode_events_df(study_dataset=df, drop_single_value_cols=False)
        df = ma.clean_up_machine_annotations(some_events=df)
        if "type_of_story" in df.columns:
            df["annotated_ok"] = ~df["type_of_story"].isna()
            df["annotated_fail"] = df["type_of_story"].isna()
        df = convert_dtypes_to_pyarrow(df)
    return df




def _reattach_contract_columns(refined: pd.DataFrame, flat_rows: list[dict],
                               contract: dict) -> pd.DataFrame:
    """Re-attach contract output columns the production recode dropped.

    ``recode_events_df`` keeps only columns known to the LIVE var_schema, so a
    candidate contract's NEW fields (the very thing an A/B run exists to
    evaluate) silently vanish during refinement. For every output column the
    arm's contract declares that is missing from the refined frame but present
    in the raw flatten, merge the raw (unrecoded) values back in, keyed on
    ``item_id`` — new fields stay visible and comparable in the results.
    """
    flat_df = pd.DataFrame(flat_rows)
    if refined.empty or flat_df.empty or "item_id" not in flat_df.columns:
        return refined

    # Map each flatten-time column to its final (post-rename) column name.
    final_by_flat: dict[str, str] = {}
    for field in contract.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        if field.get("type") == "object":
            for key in (field.get("keys") or {}):
                final_by_flat[f"{name}_{key}"] = ac.contract_output_column(name, key)
        else:
            final_by_flat[name] = ac.contract_output_column(name)

    missing = {
        flat_col: final_col
        for flat_col, final_col in final_by_flat.items()
        if final_col not in refined.columns and flat_col in flat_df.columns
    }
    if not missing:
        return refined

    add = flat_df[["item_id", *missing.keys()]].rename(columns=missing).copy()
    add["item_id"] = add["item_id"].astype(str)
    out = refined.copy()
    out["item_id"] = out["item_id"].astype(str)
    out = out.merge(add.drop_duplicates("item_id"), on="item_id", how="left")
    return convert_dtypes_to_pyarrow(out)




def _scale_map() -> dict[str, str]:
    """Return ``{variable_name: scale}`` from the live var_schema."""
    vs = fyp_cf["var_schema"]
    return {
        str(n): str(s)
        for n, s in zip(vs["variable_name"], vs["scale"], strict=False)
    }




def _classify(col: str, sa: pd.Series, sb: pd.Series, scales: dict[str, str]) -> str:
    """Classify a column as numeric / enum / list / freetext for comparison."""
    if pd.api.types.is_numeric_dtype(sa) and pd.api.types.is_numeric_dtype(sb):
        return "numeric"
    scale = scales.get(col, "")
    if scale in _NUMERIC_SCALES:
        return "numeric"
    if scale in _LIST_SCALES:
        return "list"
    has_list = sa.map(lambda x: isinstance(x, (list, np.ndarray))).any() or sb.map(
        lambda x: isinstance(x, (list, np.ndarray))
    ).any()
    if has_list:
        return "list"
    if scale in _ENUM_SCALES:
        return "enum"
    avg_len = sa.dropna().astype(str).str.len().mean()
    return "enum" if (pd.notna(avg_len) and avg_len < 25) else "freetext"




def _coverage(norm_series: pd.Series) -> float:
    """Share of normalized cells carrying a real (non-sentinel) value."""
    return float((~norm_series.isin(_NORM_SENTINELS)).mean())




def _to_set(value) -> set[str]:
    """Normalize a list-ish cell to a set of real values for Jaccard."""
    if isinstance(value, (list, np.ndarray)):
        return {_normalize_cell(v) for v in value} - _NORM_SENTINELS
    norm = _normalize_cell(value)
    return ({norm} - _NORM_SENTINELS) if norm else set()




def compare_arms(df_a: pd.DataFrame, df_b: pd.DataFrame) -> dict:
    """Field-type-aware comparison of two recoded arms aligned on ``item_id``.

    Per column: numeric → correlation + mean-abs-diff, enum → exact agreement,
    list → mean Jaccard, freetext → coverage only; plus per-arm coverage and a
    cross-kind summary block.
    """
    a = df_a.drop_duplicates("item_id").copy()
    b = df_b.drop_duplicates("item_id").copy()
    a["item_id"] = a["item_id"].astype(str)
    b["item_id"] = b["item_id"].astype(str)
    a = a.set_index("item_id")
    b = b.set_index("item_id")
    common = sorted(set(a.index) & set(b.index))
    a = a.loc[common]
    b = b.loc[common]

    scales = _scale_map()
    skip = {"annotated_ok", "annotated_fail"}
    cols = sorted((set(a.columns) & set(b.columns)) - skip)
    report: dict = {"n_items": len(common), "columns": {}}

    enum_agree, list_jaccard, num_corr, ft_cov_delta = [], [], [], []
    for c in cols:
        sa, sb = a[c], b[c]
        kind = _classify(c, sa, sb, scales)

        if kind == "numeric":
            xa = pd.to_numeric(sa, errors="coerce")
            xb = pd.to_numeric(sb, errors="coerce")
            both = xa.notna() & xb.notna()
            corr = (
                float(xa[both].corr(xb[both]))
                if both.sum() >= 3 and xa[both].std() > 0 and xb[both].std() > 0
                else None
            )
            mad = float((xa[both] - xb[both]).abs().mean()) if both.any() else None
            report["columns"][c] = {
                "kind": "numeric", "correlation": corr, "mean_abs_diff": mad,
                "coverage_a": float(xa.notna().mean()), "coverage_b": float(xb.notna().mean()),
            }
            if corr is not None:
                num_corr.append(corr)

        elif kind == "list":
            sets_a = sa.map(_to_set)
            sets_b = sb.map(_to_set)
            jac, exact = [], []
            for ia, ib in zip(sets_a, sets_b, strict=False):
                union = ia | ib
                jac.append(1.0 if not union else len(ia & ib) / len(union))
                exact.append(1.0 if ia == ib else 0.0)
            report["columns"][c] = {
                "kind": "list", "mean_jaccard": float(np.mean(jac)),
                "exact_set_agreement": float(np.mean(exact)),
                "coverage_a": float(sets_a.map(bool).mean()),
                "coverage_b": float(sets_b.map(bool).mean()),
            }
            list_jaccard.append(float(np.mean(jac)))

        elif kind == "enum":
            na = sa.map(_normalize_cell)
            nb = sb.map(_normalize_cell)
            agreement = float((na.values == nb.values).mean())
            report["columns"][c] = {
                "kind": "enum", "agreement": agreement,
                "coverage_a": _coverage(na), "coverage_b": _coverage(nb),
            }
            enum_agree.append(agreement)

        else:  # freetext
            na = sa.map(_normalize_cell)
            nb = sb.map(_normalize_cell)
            cov_a, cov_b = _coverage(na), _coverage(nb)
            report["columns"][c] = {
                "kind": "freetext", "coverage_a": cov_a, "coverage_b": cov_b,
            }
            ft_cov_delta.append(cov_b - cov_a)

    report["summary"] = {
        "n_columns": len(cols),
        "mean_enum_agreement": float(np.mean(enum_agree)) if enum_agree else None,
        "mean_list_jaccard": float(np.mean(list_jaccard)) if list_jaccard else None,
        "mean_numeric_correlation": float(np.mean(num_corr)) if num_corr else None,
        "mean_freetext_coverage_delta_b_minus_a": (
            float(np.mean(ft_cov_delta)) if ft_cov_delta else None
        ),
        "annotated_ok_rate_a": (
            float(df_a["annotated_ok"].fillna(False).mean())
            if "annotated_ok" in df_a.columns else None
        ),
        "annotated_ok_rate_b": (
            float(df_b["annotated_ok"].fillna(False).mean())
            if "annotated_ok" in df_b.columns else None
        ),
    }
    return report




def distribution_tables(frames: dict[str, pd.DataFrame], column: str, top: int = 8) -> dict:
    """Per-arm top value counts for one column (N-arm generalization)."""
    out: dict = {"column": column, "arms": {}}
    for arm, df in frames.items():
        if column not in df.columns:
            continue
        is_list = df[column].map(lambda x: isinstance(x, (list, np.ndarray))).any()
        series = df[column].explode() if is_list else df[column]
        counts = series.map(_normalize_cell).value_counts().head(top)
        out["arms"][arm] = {str(k): int(v) for k, v in counts.items()}
    return out




def build_adjudication(frames: dict[str, pd.DataFrame], columns: list[str]) -> list[dict]:
    """Per-(item, column) rows where the arms' normalized values differ.

    N-arm generalization of the spike's disagreement table: one row per
    ``(item_id, column)`` whose normalized values are not all equal, carrying
    each arm's value. Capped at ``ADJUDICATION_CAP`` rows.
    """
    indexed = {}
    for arm, df in frames.items():
        d = df.drop_duplicates("item_id").copy()
        d["item_id"] = d["item_id"].astype(str)
        indexed[arm] = d.set_index("item_id")
    arms = list(indexed)
    common = sorted(set.intersection(*(set(d.index) for d in indexed.values()))) if indexed else []
    rows: list[dict] = []
    for item in common:
        for col in columns:
            values = {}
            for arm in arms:
                d = indexed[arm]
                values[arm] = _normalize_cell(d.loc[item, col]) if col in d.columns else "<absent>"
            if len(set(values.values())) > 1:
                rows.append({"item_id": item, "column": col, "values": values})
                if len(rows) >= ADJUDICATION_CAP:
                    return rows
    return rows




# ---------------------------------------------------------------------------
# Runs (execution + artifact storage + index).
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """Return a sortable, collision-safe run id."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]




def _run_file(run_id: str, name: str) -> str:
    """Return the storage filename for one run artifact."""
    return f"runs/{run_id}/{name}"




def load_runs_index() -> list[dict]:
    """Return the runs index (newest first). Never raises."""
    ensure_locations()
    try:
        if data_io.exists(storage_location=LOCATION, filename=RUNS_INDEX_FILENAME):
            index = data_io.load_json(storage_location=LOCATION, filename=RUNS_INDEX_FILENAME)
            if isinstance(index, list):
                return sorted(index, key=lambda r: r.get("run_id") or "", reverse=True)
    except Exception:
        pass
    return []




def _update_runs_index(entry: dict) -> None:
    """Insert/replace one run's entry in the index (keyed by run_id)."""
    index = [r for r in load_runs_index() if r.get("run_id") != entry.get("run_id")]
    index.append(entry)
    data_io.save_json(data=index, storage_location=LOCATION, filename=RUNS_INDEX_FILENAME)




def load_run(run_id: str) -> dict:
    """Load one run's manifest + report (report may be None while running)."""
    ensure_locations()
    manifest = data_io.load_json(storage_location=LOCATION, filename=_run_file(run_id, "manifest.json"))
    report = None
    report_fn = _run_file(run_id, "report.json")
    if data_io.exists(storage_location=LOCATION, filename=report_fn):
        report = data_io.load_json(storage_location=LOCATION, filename=report_fn)
    return {"manifest": manifest, "report": report}




def load_run_rows(run_id: str, arm: str) -> list[dict]:
    """Return one arm's refined rows as JSON-safe records (normalized cells)."""
    ensure_locations()
    df = data_io.load_parquet(storage_location=LOCATION, filename=_run_file(run_id, f"arm_{arm}.parquet"))
    records = []
    for _, row in df.iterrows():
        records.append({col: _normalize_cell(row[col]) for col in df.columns})
    return records




def delete_run(run_id: str) -> bool:
    """Delete a run's artifacts and drop it from the index."""
    ensure_locations()
    manifest = None
    try:
        manifest = data_io.load_json(storage_location=LOCATION, filename=_run_file(run_id, "manifest.json"))
    except Exception:
        pass
    arm_names = [a.get("name") for a in (manifest or {}).get("arms", []) if a.get("name")]
    filenames = ["manifest.json", "report.json"]
    filenames += [f"raw_{a}.json" for a in arm_names] + [f"arm_{a}.parquet" for a in arm_names]
    removed = False
    for name in filenames:
        fn = _run_file(run_id, name)
        try:
            if data_io.exists(storage_location=LOCATION, filename=fn):
                data_io.remove(storage_location=LOCATION, filename=fn)
                removed = True
        except Exception:
            pass
    index = [r for r in load_runs_index() if r.get("run_id") != run_id]
    data_io.save_json(data=index, storage_location=LOCATION, filename=RUNS_INDEX_FILENAME)
    return removed




def _arm_cost(raw_rows: list[dict]) -> dict:
    """Aggregate token/error/latency stats for one arm's raw rows."""
    totals = {"prompt_tokens": 0, "candidates_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0}
    errors = 0
    durations = []
    for row in raw_rows:
        if row.get("error"):
            errors += 1
        usage = row.get("usage") or {}
        for key in totals:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                totals[key] += int(value)
        dur = row.get("inference_duration")
        if isinstance(dur, (int, float)) and dur >= 0:
            durations.append(float(dur))
    return {
        **totals,
        "n_errors": errors,
        "mean_inference_duration": float(np.mean(durations)) if durations else None,
    }




def execute_run(run_id: str, arms: list[dict], item_ids: list[str],
                started_by: str = "", runner=None, progress_cb=None, cancel_cb=None) -> dict:
    """Execute a full A/B run and persist its artifacts.

    Args:
        run_id: the run's id (see :func:`new_run_id`).
        arms: ``[{name, source: "candidate"|"live", text}]`` — each arm's
            contract TOML text, snapshotted by the caller at start.
        item_ids: the eval-set ids (hard-capped at ``MAX_EVAL_ITEMS``).
        started_by: audit actor.
        runner: optional runner override (tests inject a stub here).
        progress_cb: ``(arm_name, done, total)`` per completed item.
        cancel_cb: zero-arg callable; True aborts with ``RunCancelled``.

    Returns:
        The run summary written to the index.

    Raises:
        ValueError: bad arms / oversized eval set / invalid arm contract.
        RunCancelled: when cancelled mid-run (manifest marked ``cancelled``).
    """
    from fyp.machine_annotation import platform_map_for

    ensure_locations()
    if not arms or len(arms) < 1:
        raise ValueError("at least one arm is required")
    if not item_ids:
        raise ValueError("the eval set is empty")
    if len(item_ids) > MAX_EVAL_ITEMS:
        raise ValueError(f"eval set exceeds the {MAX_EVAL_ITEMS}-item cap ({len(item_ids)})")
    names = [a.get("name") for a in arms]
    if len(set(names)) != len(names):
        raise ValueError("arm names must be unique")

    # Parse every arm up front (fail fast before any Gemini call is made).
    parsed_arms = []
    for arm in arms:
        contract, errors = ac.parse_and_validate(arm["text"])
        if contract is None or errors:
            raise ValueError(f"arm '{arm.get('name')}' contract invalid: {'; '.join(errors)}")
        parsed_arms.append({
            "name": arm["name"],
            "source": arm.get("source", "candidate"),
            "etag": ac._etag(arm["text"], arm.get("source", "candidate")),
            "contract": contract,
            "text": arm["text"],
        })

    item_ids = [str(i) for i in item_ids]
    platform_map = platform_map_for(item_ids)

    manifest = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "started_by": started_by,
        "status": "running",
        "item_ids": item_ids,
        "n_items": len(item_ids),
        "arms": [
            {"name": a["name"], "source": a["source"], "etag": a["etag"]}
            for a in parsed_arms
        ],
    }
    data_io.save_json(data=manifest, storage_location=LOCATION,
                      filename=_run_file(run_id, "manifest.json"))
    _update_runs_index({**{k: manifest[k] for k in
                           ("run_id", "started_at", "started_by", "status", "n_items")},
                        "arms": [a["name"] for a in parsed_arms]})

    frames: dict[str, pd.DataFrame] = {}
    costs: dict[str, dict] = {}
    try:
        for arm in parsed_arms:
            def _cb(done, total, _name=arm["name"]):
                if progress_cb:
                    progress_cb(_name, done, total)

            arm_runner = runner or SyncThreadedRunner(cancel_cb=cancel_cb)
            flat_rows, raw_rows = run_arm(
                arm["name"], arm["contract"], item_ids, platform_map,
                runner=arm_runner, progress_cb=_cb,
            )
            data_io.save_json(data=raw_rows, storage_location=LOCATION,
                              filename=_run_file(run_id, f"raw_{arm['name']}.json"))
            refined = refine_from_flat_dicts(flat_rows)
            # The recode keeps only live-var_schema columns — bring the arm's
            # own (e.g. candidate-only) fields back so they show in results.
            refined = _reattach_contract_columns(refined, flat_rows, arm["contract"])
            data_io.save_parquet(df=refined, storage_location=LOCATION,
                                 filename=_run_file(run_id, f"arm_{arm['name']}.parquet"))
            frames[arm["name"]] = refined
            costs[arm["name"]] = _arm_cost(raw_rows)

        # Pairwise comparisons + N-arm distributions/adjudication.
        arm_names = [a["name"] for a in parsed_arms]
        comparisons: dict[str, dict] = {}
        compared_cols: set[str] = set()
        for i in range(len(arm_names)):
            for j in range(i + 1, len(arm_names)):
                key = f"{arm_names[i]}|{arm_names[j]}"
                comparisons[key] = compare_arms(frames[arm_names[i]], frames[arm_names[j]])
                compared_cols |= set(comparisons[key]["columns"].keys())

        dist_cols = sorted(
            c for c in compared_cols
            if any(comp["columns"].get(c, {}).get("kind") in ("enum", "list")
                   for comp in comparisons.values())
        )
        distributions = {c: distribution_tables(frames, c) for c in dist_cols}
        adjudication = build_adjudication(frames, sorted(compared_cols))

        report = {
            "run_id": run_id,
            "arms": arm_names,
            "comparisons": comparisons,
            "distributions": distributions,
            "adjudication": adjudication,
            "costs": costs,
        }
        data_io.save_json(data=report, storage_location=LOCATION,
                          filename=_run_file(run_id, "report.json"))

        manifest["status"] = "complete"
        manifest["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    except RunCancelled:
        manifest["status"] = "cancelled"
        manifest["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        raise
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        raise
    finally:
        data_io.save_json(data=manifest, storage_location=LOCATION,
                          filename=_run_file(run_id, "manifest.json"))
        _update_runs_index({**{k: manifest[k] for k in
                               ("run_id", "started_at", "started_by", "status", "n_items")},
                            "arms": [a["name"] for a in parsed_arms],
                            "error": manifest.get("error")})

    return {
        "run_id": run_id,
        "status": manifest["status"],
        "n_items": len(item_ids),
        "arms": [a["name"] for a in parsed_arms],
        "costs": costs,
    }
