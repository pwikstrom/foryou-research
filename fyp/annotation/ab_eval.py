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
``ab_candidates`` and the named evaluation sets in ``users/ab_eval_sets.json``).
It must never write to ``machine_annotations_*`` or ``recoded`` — eval results
do not exist as far as studies are concerned. ``tests/unit/test_ab_eval.py``
guards this.

METRIC RULE: a column's comparison kind comes from its DECLARED scale (the
contract / var_schema), never from how long its answers happen to be. Scoring a
free-text field with exact-string agreement produces a number that looks like a
disagreement rate but is not one.
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
from fyp.types import convert_dtypes_to_pyarrow

# NOTE: fyp.machine_annotation, fyp.recode_variables and google.genai are
# imported lazily inside the functions that need them — they are heavy imports
# the candidate/eval-set CRUD endpoints should not pay for.


def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf

LOCATION = "ab_eval"                      # run artifacts (isolated)
CANDIDATES_LOCATION = "ab_candidates"     # candidate contract TOMLs (admin config)
EVAL_SET_LOCATION = "users"               # the curated eval-set id lists
EVAL_SET_FILENAME = "ab_eval_set.json"    # legacy single-set file (migrated in)
EVAL_SETS_FILENAME = "ab_eval_sets.json"  # named sets + active pointer
RUNS_INDEX_FILENAME = "runs_index.json"

MAX_EVAL_ITEMS = 50                       # hard cost cap (endpoints + worker)
MAX_WORKERS = 4
ADJUDICATION_CAP = 3000

CANDIDATE_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,40}$")
EVAL_SET_NAME_RE = CANDIDATE_NAME_RE
DEFAULT_EVAL_SET = "default"

# Sentinels a normalized cell may collapse to that mean "no real value". Compared
# case-insensitively, after unicode dashes are folded onto "-" — Gemini answers
# the contract's "or '-' if none" instruction with an en dash often enough that a
# literal-only match badly overstates a candidate's coverage.
_NORM_SENTINELS = {
    "unable to detect", "-", "--", "other category", "not coded", "", "<NA>".lower(),
    # "no" is deliberately NOT here: for yes/no fields it is a real answer.
    "none", "n/a", "unknown", "unclear", "other",
}
_DASH_FOLD = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2212": "-"})   # en dash, em dash, minus

# Scales as declared by the four TOML contracts / the synthesized var_schema.
# (The pre-2026-07 ten-scale vocabulary — ratio/interval/ordinal/dichotomous/
# factor/collection — is gone; 'factor' is a ROLE now, not a scale.)
_NUMERIC_SCALES = {"numeric"}
_ENUM_SCALES = {"categorical"}
_LIST_SCALES = {"list"}
_TEXT_SCALES = {"text", "raw", "datetime"}

# A `list`-scale column whose elements average longer than this is really free
# prose in a one-element list (e.g. background_music: "upbeat and rhythmic
# electronic"). Set agreement on such elements is exact-phrase matching, so the
# Jaccard we report is flagged rather than silently read as disagreement.
_FREE_TEXT_ELEMENT_CHARS = 25

# Minimum paired observations before a Pearson r is worth reporting.
_MIN_CORR_N = 3




class RunCancelled(Exception):
    """Raised inside :func:`execute_run` when the reporter requests cancel."""




def ensure_locations() -> None:
    """Register the harness's storage locations (idempotent).

    ``ab_eval`` holds run artifacts under ``<local_data>/ab_eval``;
    ``ab_candidates`` holds candidate contract TOMLs under
    ``<local_data>/users/ab_candidates`` (admin config, next to the other
    ``users`` stores).
    """
    local_data = _cf()["paths"]["local_data"]
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
# Eval sets (named curated item-id lists + platform/downloaded resolution +
# sampling). The store is ``{"active": <name>, "sets": {<name>: {...}}}``; the
# pre-2026-07 single-set file is migrated into it on first read.
# ---------------------------------------------------------------------------


def validate_eval_set_name(name: str) -> bool:
    """Return True when ``name`` is a legal evaluation-set name."""
    return bool(isinstance(name, str) and EVAL_SET_NAME_RE.match(name))




def _blank_set(item_ids: list[str] | None = None, actor: str = "", note: str = "") -> dict:
    """Return a fresh set record."""
    return {
        "item_ids": list(item_ids or []),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "updated_by": actor,
        "note": note,
    }




def _load_sets_store() -> dict:
    """Load the named-set store, migrating the legacy single-set file if needed.

    Never raises: an unreadable/absent store yields an empty ``default`` set.
    """
    try:
        if data_io.exists(storage_location=EVAL_SET_LOCATION, filename=EVAL_SETS_FILENAME):
            store = data_io.load_json(
                storage_location=EVAL_SET_LOCATION, filename=EVAL_SETS_FILENAME
            )
            if isinstance(store, dict) and isinstance(store.get("sets"), dict) and store["sets"]:
                for record in store["sets"].values():
                    record.setdefault("item_ids", [])
                if store.get("active") not in store["sets"]:
                    store["active"] = next(iter(store["sets"]))
                return store
    except Exception:
        pass

    legacy = None
    try:
        if data_io.exists(storage_location=EVAL_SET_LOCATION, filename=EVAL_SET_FILENAME):
            stored = data_io.load_json(
                storage_location=EVAL_SET_LOCATION, filename=EVAL_SET_FILENAME
            )
            if isinstance(stored, dict):
                stored.setdefault("item_ids", [])
                legacy = stored
    except Exception:
        pass
    return {"active": DEFAULT_EVAL_SET, "sets": {DEFAULT_EVAL_SET: legacy or _blank_set()}}




def _save_sets_store(store: dict) -> dict:
    """Persist the named-set store."""
    data_io.save_json(data=store, storage_location=EVAL_SET_LOCATION,
                      filename=EVAL_SETS_FILENAME)
    return store




def list_eval_sets() -> dict:
    """Return ``{active, sets: [{name, n_items, updated_at, updated_by, note}]}``."""
    store = _load_sets_store()
    sets = [
        {
            "name": name,
            "n_items": len(record.get("item_ids") or []),
            "updated_at": record.get("updated_at"),
            "updated_by": record.get("updated_by"),
            "note": record.get("note") or "",
        }
        for name, record in store["sets"].items()
    ]
    sets.sort(key=lambda s: s["name"])
    return {"active": store["active"], "sets": sets}




def load_eval_set(name: str | None = None) -> dict:
    """Return one eval set (the active one when ``name`` is None).

    The returned shape stays ``{name, item_ids, updated_at, updated_by, note}``
    so pre-existing callers (the worker, the estimate endpoint) keep working.
    """
    store = _load_sets_store()
    key = name or store["active"]
    record = store["sets"].get(key)
    if record is None:
        return {**_blank_set(), "name": key}
    return {**record, "name": key}




def save_eval_set(item_ids: list[str], actor: str = "", note: str = "",
                  name: str | None = None) -> dict:
    """Persist one eval set's items (deduped, order-preserving, capped).

    Args:
        item_ids: the curated ids; blanks dropped, duplicates collapsed.
        actor: the admin performing the save (audit).
        note: free-text note shown next to the set.
        name: the target set; the active set when omitted. A name that does not
            exist yet is created.

    Raises:
        ValueError: on an illegal name, or more than ``MAX_EVAL_ITEMS`` ids.
    """
    ids = list(dict.fromkeys(str(i).strip() for i in item_ids if str(i).strip()))
    if len(ids) > MAX_EVAL_ITEMS:
        raise ValueError(f"eval set is capped at {MAX_EVAL_ITEMS} items (got {len(ids)})")
    store = _load_sets_store()
    key = name or store["active"]
    if not validate_eval_set_name(key):
        raise ValueError(
            "set name must be 1-40 chars of lowercase letters, digits, '_' or '-'"
        )
    store["sets"][key] = _blank_set(ids, actor=actor, note=note)
    store["active"] = key
    _save_sets_store(store)
    return {**store["sets"][key], "name": key}




def create_eval_set(name: str, copy_from: str | None = None, actor: str = "") -> dict:
    """Create a new (optionally cloned) eval set and make it active.

    Raises:
        ValueError: on an illegal name.
        FileExistsError: when the set already exists.
    """
    if not validate_eval_set_name(name):
        raise ValueError(
            "set name must be 1-40 chars of lowercase letters, digits, '_' or '-'"
        )
    store = _load_sets_store()
    if name in store["sets"]:
        raise FileExistsError(f"evaluation set '{name}' already exists")
    source = store["sets"].get(copy_from) if copy_from else None
    store["sets"][name] = _blank_set(list(source.get("item_ids", [])) if source else [],
                                     actor=actor)
    store["active"] = name
    _save_sets_store(store)
    return {**store["sets"][name], "name": name}




def rename_eval_set(name: str, new_name: str) -> dict:
    """Rename an eval set, preserving its items and the active pointer.

    Raises:
        ValueError: on an illegal new name.
        FileNotFoundError: when ``name`` does not exist.
        FileExistsError: when ``new_name`` is taken.
    """
    if not validate_eval_set_name(new_name):
        raise ValueError(
            "set name must be 1-40 chars of lowercase letters, digits, '_' or '-'"
        )
    store = _load_sets_store()
    if name not in store["sets"]:
        raise FileNotFoundError(f"evaluation set '{name}' not found")
    if new_name != name and new_name in store["sets"]:
        raise FileExistsError(f"evaluation set '{new_name}' already exists")
    # Rebuild in place so the insertion order (and thus the UI order) is stable.
    store["sets"] = {(new_name if k == name else k): v for k, v in store["sets"].items()}
    if store["active"] == name:
        store["active"] = new_name
    _save_sets_store(store)
    return {**store["sets"][new_name], "name": new_name}




def delete_eval_set(name: str) -> dict:
    """Delete an eval set; the last remaining set cannot be deleted.

    Raises:
        FileNotFoundError: when ``name`` does not exist.
        ValueError: when it is the only set left.
    """
    store = _load_sets_store()
    if name not in store["sets"]:
        raise FileNotFoundError(f"evaluation set '{name}' not found")
    if len(store["sets"]) == 1:
        raise ValueError("cannot delete the only evaluation set")
    del store["sets"][name]
    if store["active"] == name:
        store["active"] = next(iter(store["sets"]))
    _save_sets_store(store)
    return {"active": store["active"]}




def set_active_eval_set(name: str) -> dict:
    """Point the active-set marker at ``name``.

    Raises:
        FileNotFoundError: when ``name`` does not exist.
    """
    store = _load_sets_store()
    if name not in store["sets"]:
        raise FileNotFoundError(f"evaluation set '{name}' not found")
    store["active"] = name
    _save_sets_store(store)
    return {"active": name}




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
        if frame is None or "item_id" not in frame.columns:
            return None
        # Status parquets from before multi-platform lack source_platform (and
        # load_parquet_selective silently skips absent columns) — normalise to
        # the full schema so resolve/sample can rely on every column existing.
        for col in ("source_platform", "video_downloaded", "scraped_ok"):
            if col not in frame.columns:
                frame[col] = pd.NA
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

    from fyp.core import media_paths

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




def annotate_one(item_id: str, platform: str | None, prompt_text: str, response_schema,
                 gen_overrides: dict | None = None) -> dict:
    """Annotate ONE video against an explicit prompt + response schema.

    The A/B analogue of ``machine_annotation.call_machine``: same client, same
    ``[machine]`` generation parameters (so per-call cost matches production),
    but the prompt/schema come from the caller's candidate contract and the
    result is returned — NEVER persisted, stamped, or queued.

    Args:
        item_id: The item to annotate.
        platform: The item's source platform (media resolution).
        prompt_text: The arm's rendered system instruction.
        response_schema: The arm's rendered response schema.
        gen_overrides: Optional per-arm overrides of the ``[machine]``
            generation parameters (``model`` / ``temperature`` /
            ``thinking_budget`` / ``max_output_tokens``); absent keys fall
            back to the production values, so old callers are unaffected.

    Returns:
        ``{item_id, model, parsed, response, finish_reason, usage,
        inference_duration, error}`` (``parsed`` is None on any failure).
    """
    import google.genai.types as gt

    # Canonical subpackage path, NOT the fyp.machine_annotation alias shim:
    # SyncThreadedRunner calls this from MAX_WORKERS pool threads. Until now it
    # was safe only because execute_run happens to import platform_map_for off
    # the same shim first — delete that line and the pool would race a cold
    # shim. See tests/unit/test_pool_import_race.py.
    from fyp.annotation.machine_annotation import initialize_machine

    initialize_machine()
    machine = _cf()["machine"]["gemini"]
    effective = {key: machine[key] for key in
                 ("model", "temperature", "thinking_budget", "max_output_tokens")}
    effective.update({k: v for k, v in (gen_overrides or {}).items() if v is not None})
    config = gt.GenerateContentConfig(
        system_instruction=prompt_text,
        temperature=effective["temperature"],
        max_output_tokens=effective["max_output_tokens"],
        response_mime_type="application/json",
        response_schema=response_schema,
        thinking_config=gt.ThinkingConfig(thinking_budget=effective["thinking_budget"]),
    )

    out: dict = {
        "item_id": str(item_id),
        "model": effective["model"],
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
            model=effective["model"], config=config, contents=contents,
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

    def __init__(self, max_workers: int = MAX_WORKERS, cancel_cb=None,
                 gen_overrides: dict | None = None):
        self.max_workers = max_workers
        self.cancel_cb = cancel_cb
        # Per-arm generation overrides (model/temperature/...) threaded through
        # the constructor so run()'s signature stays stable for other runners.
        self.gen_overrides = gen_overrides




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
                    prompt_text, response_schema, self.gen_overrides,
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
                        "model": _cf()["machine"]["gemini"].get("model"),
                    }
                done += 1
                if progress_cb:
                    progress_cb(done, len(item_ids))
                if self.cancel_cb and self.cancel_cb():
                    for pending in futures:
                        pending.cancel()
                    raise RunCancelled()
        return [results[str(i)] for i in item_ids if str(i) in results]




def _validate_arm_backend(arm_name: str | None, backend_name: str) -> None:
    """Fail fast when an arm names a backend that cannot run right now.

    Args:
        arm_name: The arm's display name (error messages only).
        backend_name: The requested backend selection id (an implementation
            id or a config-declared variant name).

    Raises:
        ValueError: For an unknown/unimplemented backend or one whose
            availability check fails (the reason is included verbatim).
    """
    from fyp.annotation.backends import get_backend

    try:
        backend = get_backend(backend_name)
    except ValueError as exc:
        raise ValueError(f"arm '{arm_name}': {exc}") from exc
    if backend.name == "gemini":
        return  # gemini readiness is checked once by the worker's config gate
    result = backend.availability(deep=False)
    if not result.ok:
        raise ValueError(f"arm '{arm_name}': backend '{backend_name}' unavailable — {result.reason}")






def _runner_for_arm(arm: dict, cancel_cb=None):
    """Build the runner matching an arm's backend and generation overrides.

    Args:
        arm: A parsed arm dict (``backend`` + ``gen_overrides`` keys).
        cancel_cb: Cancellation callback threaded into the runner.

    Returns:
        An object with the ``SyncThreadedRunner.run`` signature.
    """
    from fyp.annotation.backends import get_backend

    backend_name = arm.get("backend") or "gemini"
    gen_overrides = arm.get("gen_overrides") or None
    backend = get_backend(backend_name)
    if backend.name == "gemini":
        # A gemini variant's config overrides apply beneath the arm's own
        # overrides (arm wins) via the threaded per-call override path.
        merged = {**backend.overrides, **(gen_overrides or {})}
        return SyncThreadedRunner(cancel_cb=cancel_cb, gen_overrides=merged or None)

    # Non-Gemini backends constrain decoding with the PORTABLE JSON schema
    # (run_arm passes the google-genai form, which only Gemini understands).
    schema_json = sch.get_annotation_json_schema(arm.get("contract")) if arm.get("contract") else None
    return BackendSequentialRunner(backend, cancel_cb=cancel_cb,
                                   gen_overrides=gen_overrides, schema_json=schema_json)






class BackendSequentialRunner:
    """Arm runner for non-Gemini backends: a sequential loop over the backend.

    Local backends hold the whole model in memory, so ``max_workers`` is
    effectively 1 — a plain loop with a cancellation check between items.
    Implements the same ``run()`` signature as :class:`SyncThreadedRunner` and
    returns the same raw-row keys, translating the backend's production
    raw-row dict (``response``/``structured``) into the A/B row shape
    (``parsed`` extracted here).
    """

    def __init__(self, backend, cancel_cb=None, gen_overrides: dict | None = None,
                 schema_json: dict | None = None):
        self.backend = backend
        self.cancel_cb = cancel_cb
        self.gen_overrides = gen_overrides
        # Portable JSON schema for the arm's contract; preferred over the
        # genai-typed schema run() receives (backends can't consume that).
        self.schema_json = schema_json




    def run(self, prompt_text: str, response_schema, item_ids: list[str],
            platform_map: dict[str, str], progress_cb=None) -> list[dict]:
        """Annotate every item sequentially; returns rows in input order.

        Raises:
            RunCancelled: when ``cancel_cb`` returns True between items.
        """
        rows: list[dict] = []
        for done, item_id in enumerate(item_ids, start=1):
            if self.cancel_cb and self.cancel_cb():
                raise RunCancelled()
            try:
                raw = self.backend.annotate_one(
                    str(item_id), platform=platform_map.get(str(item_id)),
                    gen_overrides=self.gen_overrides,
                    prompt_text=prompt_text,
                    response_schema=self.schema_json if self.schema_json is not None else response_schema,
                )
                row = {
                    "item_id": str(item_id),
                    "model": raw.get("model"),
                    "parsed": None,
                    "response": raw.get("response", ""),
                    "finish_reason": raw.get("finish_reason", "unknown"),
                    "usage": raw.get("usage", {}),
                    "inference_duration": raw.get("inference_duration", -1.0),
                    "error": raw.get("error", ""),
                }
                if not row["error"] and row["response"]:
                    try:
                        row["parsed"] = json.loads(row["response"])
                    except Exception as exc:
                        row["error"] = f"parse: {exc}"
            except Exception as exc:
                row = {"item_id": str(item_id), "model": getattr(self.backend, "name", "?"),
                       "parsed": None, "response": "", "finish_reason": "DNF - runner error",
                       "usage": {}, "inference_duration": -1.0, "error": str(exc)}
            rows.append(row)
            if progress_cb:
                progress_cb(done, len(item_ids))
        return rows






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
    vs = _cf()["var_schema"]
    return {
        str(n): str(s)
        for n, s in zip(vs["variable_name"], vs["scale"], strict=False)
    }




def contract_scale_map(contract: dict) -> dict[str, str]:
    """Return ``{output_column: scale}`` declared by one contract.

    An arm's brand-new field has no var_schema row (that is the whole point of
    evaluating it), so without this its metric kind would be guessed from cell
    lengths. Keyed on the final, post-recode column name.
    """
    out: dict[str, str] = {}
    for field in contract.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        if field.get("type") == "object":
            parent_array = bool(field.get("array"))
            for key, spec in (field.get("keys") or {}).items():
                scale = ac.effective_subkey_scale(spec, parent_array=parent_array)
                if scale:
                    out[ac.contract_output_column(name, key)] = str(scale)
        else:
            scale = ac.effective_scale(field)
            if scale:
                out[ac.contract_output_column(name)] = str(scale)
    return out




def _is_sentinel(value: str) -> bool:
    """True when a normalized cell means "no real value" for this column.

    Case-insensitive, whitespace-trimmed, with unicode dashes folded onto "-".
    """
    return str(value).strip().translate(_DASH_FOLD).lower() in _NORM_SENTINELS




def _canon(value: str) -> str:
    """Canonical comparison key for an enum/list value ("" when it is a sentinel)."""
    return "" if _is_sentinel(value) else str(value).strip().translate(_DASH_FOLD).lower()




def _classify(col: str, sa: pd.Series, sb: pd.Series, scales: dict[str, str]) -> str:
    """Classify a column as numeric / enum / list / freetext for comparison.

    The declared scale wins over the observed dtype: a ``text`` field whose
    answers happen to be short is still free text, and must never be scored
    with exact-string agreement. Only a column with no declared scale (an
    unrecognised recode-derived column) falls back to the dtype/length guess.
    """
    scale = scales.get(col, "")
    if scale in _NUMERIC_SCALES:
        return "numeric"
    if scale in _LIST_SCALES:
        return "list"
    if scale in _ENUM_SCALES:
        return "enum"
    if scale in _TEXT_SCALES:
        return "freetext"

    if pd.api.types.is_numeric_dtype(sa) and pd.api.types.is_numeric_dtype(sb):
        return "numeric"
    has_list = sa.map(lambda x: isinstance(x, (list, np.ndarray))).any() or sb.map(
        lambda x: isinstance(x, (list, np.ndarray))
    ).any()
    if has_list:
        return "list"
    lengths = pd.concat([sa.dropna(), sb.dropna()]).astype(str).str.len()
    avg_len = lengths.mean() if len(lengths) else None
    return "enum" if (avg_len is not None and pd.notna(avg_len) and avg_len < 25) else "freetext"




def _coverage(norm_series: pd.Series) -> float:
    """Share of normalized cells carrying a real (non-sentinel) value."""
    if not len(norm_series):
        return 0.0
    return float((~norm_series.map(_is_sentinel)).mean())




def _to_set(value) -> set[str]:
    """Normalize a list-ish cell to a set of real values for Jaccard.

    Handles the three shapes a ``list``-scale column arrives in: a real
    list/array, a ``SPLITTER``-joined string (what the recode leaves behind for
    object sub-keys like ``faces_gender``), or a bare scalar.
    """
    if isinstance(value, (list, tuple, np.ndarray)):
        parts = [_normalize_cell(v) for v in value]
    else:
        norm = _normalize_cell(value)
        splitter = _cf()["labels"]["SPLITTER"]
        parts = norm.split(splitter) if (splitter and splitter in norm) else [norm]
    return {c for c in (_canon(p) for p in parts) if c}




def _mean_element_chars(sets: pd.Series) -> float:
    """Mean character length of the individual elements across a column of sets."""
    lengths = [len(element) for value_set in sets for element in value_set]
    return float(np.mean(lengths)) if lengths else 0.0




def compare_arms(df_a: pd.DataFrame, df_b: pd.DataFrame,
                 scales: dict[str, str] | None = None) -> dict:
    """Field-type-aware comparison of two recoded arms aligned on ``item_id``.

    Per column, keyed on the field's DECLARED scale (never on how long its
    answers happen to be):

    * ``numeric``  → exact agreement + mean-abs-diff over the items both arms
      scored; Pearson r as a secondary metric (suppressed from the summary and
      flagged when the paired scores barely vary — near-constant scores make r
      track rounding noise, not disagreement).
    * ``categorical`` → exact agreement after case/sentinel canonicalization.
    * ``list``     → mean Jaccard over the value sets + exact-set agreement.
    * ``text``     → coverage only (two prose answers are never string-equal).

    Every column additionally reports ``coverage_a``/``coverage_b`` (the share
    of items where that arm returned a substantive value — ``no`` / ``-`` /
    ``unable to detect`` / blank all count as *no value*) and the item counts
    behind its headline metric, because agreement over items where *both* arms
    said nothing is vacuous and would otherwise silently inflate the means.
    ``agreement_filled`` / ``mean_jaccard_filled`` restate each metric over only
    the items where there was something to agree about.

    Args:
        df_a: arm A's refined frame (must carry ``item_id``).
        df_b: arm B's refined frame.
        scales: ``{column: scale}`` override; defaults to the live var_schema.
            ``execute_run`` passes var_schema unioned with each arm's contract
            so candidate-only fields are classified from their declaration.

    Returns:
        ``{n_items, columns: {col: {...}}, summary: {...}}``.
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

    # Only items BOTH arms successfully annotated are comparable — an item one
    # arm failed would otherwise drag every metric (its empty cells read as
    # disagreement). Excluded count is reported so the drop stays visible.
    n_common_raw = len(common)
    if "annotated_ok" in a.columns and "annotated_ok" in b.columns:
        ok = (a["annotated_ok"].fillna(False).astype(bool)
              & b["annotated_ok"].fillna(False).astype(bool))
        a = a.loc[ok]
        b = b.loc[ok]

    scales = scales if scales is not None else _scale_map()
    skip = {"annotated_ok", "annotated_fail"}
    cols = sorted((set(a.columns) & set(b.columns)) - skip)
    n_items = len(a)
    report: dict = {"n_items": n_items, "columns": {},
                    "n_items_excluded": n_common_raw - n_items}

    enum_agree, enum_agree_filled = [], []
    list_jaccard, list_jaccard_filled = [], []
    num_corr, num_exact, ft_cov_delta = [], [], []
    kind_counts = {"numeric": 0, "enum": 0, "list": 0, "freetext": 0}

    for c in cols:
        sa, sb = a[c], b[c]
        kind = _classify(c, sa, sb, scales)
        kind_counts[kind] += 1

        if kind == "numeric":
            xa = pd.to_numeric(sa, errors="coerce")
            xb = pd.to_numeric(sb, errors="coerce")
            both = xa.notna() & xb.notna()
            n_compared = int(both.sum())
            diffs = (xa[both] - xb[both]).abs()
            exact = float(diffs.le(1e-9).mean()) if n_compared else None
            mad = float(diffs.mean()) if n_compared else None
            # std() over an arrow-backed series is pd.NA for a single value —
            # any comparison against it would propagate NA into the flag.
            std_a, std_b = xa[both].std(), xb[both].std()
            constant = n_compared > 1 and any(
                (not pd.isna(s)) and float(s) == 0 for s in (std_a, std_b))
            corr = (
                float(xa[both].corr(xb[both]))
                if n_compared >= _MIN_CORR_N and not constant
                else None
            )
            # Pearson r covaries with the residual spread, not with closeness:
            # near-constant scores (e.g. sensitivity mostly 0.3 with 0.05
            # wobble) can give a strongly negative r while the arms in fact
            # agree closely. Flag such columns and keep them out of the
            # summary's mean r — exact agreement + mean-abs-diff are the
            # headline metrics there.
            low_variance = False
            if corr is not None:
                level = max(float(pd.concat([xa[both], xb[both]]).abs().mean()), 1e-12)
                low_variance = (float(std_a) / level < 0.15
                                or float(std_b) / level < 0.15)
            caveat = None
            if corr is None:
                # Distinguish "both arms answered identically every time" (r is
                # 0/0, not a disagreement) from "not enough paired answers".
                caveat = "constant" if constant else "too_few"
            elif low_variance:
                caveat = "low_variance"
            report["columns"][c] = {
                "kind": "numeric", "correlation": corr, "mean_abs_diff": mad,
                "exact_agreement": exact,
                "n_compared": n_compared,
                "coverage_a": float(xa.notna().mean()) if n_items else 0.0,
                "coverage_b": float(xb.notna().mean()) if n_items else 0.0,
                "caveat": caveat,
            }
            if exact is not None:
                num_exact.append(exact)
            if corr is not None and not low_variance:
                num_corr.append(corr)

        elif kind == "list":
            sets_a = sa.map(_to_set)
            sets_b = sb.map(_to_set)
            jac, exact, jac_filled = [], [], []
            for ia, ib in zip(sets_a, sets_b, strict=False):
                union = ia | ib
                score = 1.0 if not union else len(ia & ib) / len(union)
                jac.append(score)
                exact.append(1.0 if ia == ib else 0.0)
                if union:
                    jac_filled.append(score)
            mean_jac = float(np.mean(jac)) if jac else None
            mean_jac_filled = float(np.mean(jac_filled)) if jac_filled else None
            free_text = max(_mean_element_chars(sets_a),
                            _mean_element_chars(sets_b)) > _FREE_TEXT_ELEMENT_CHARS
            report["columns"][c] = {
                "kind": "list", "mean_jaccard": mean_jac,
                "mean_jaccard_filled": mean_jac_filled,
                "exact_set_agreement": float(np.mean(exact)) if exact else None,
                "n_both_empty": int(len(jac) - len(jac_filled)),
                "coverage_a": float(sets_a.map(bool).mean()) if n_items else 0.0,
                "coverage_b": float(sets_b.map(bool).mean()) if n_items else 0.0,
                "caveat": "free_text_elements" if free_text else None,
            }
            # Free-text-element lists are exact-phrase Jaccard — like plain
            # free text they are shown but kept OUT of the summary means, which
            # would otherwise read wording variation as disagreement.
            if not free_text:
                if mean_jac is not None:
                    list_jaccard.append(mean_jac)
                if mean_jac_filled is not None:
                    list_jaccard_filled.append(mean_jac_filled)

        elif kind == "enum":
            ca = sa.map(_normalize_cell).map(_canon)
            cb = sb.map(_normalize_cell).map(_canon)
            match = ca.values == cb.values
            filled_both = (ca != "").values & (cb != "").values
            n_filled_both = int(filled_both.sum())
            agreement = float(match.mean()) if n_items else None
            agreement_filled = (
                float(match[filled_both].mean()) if n_filled_both else None
            )
            both_empty = int(((ca == "").values & (cb == "").values).sum())
            report["columns"][c] = {
                "kind": "enum", "agreement": agreement,
                "agreement_filled": agreement_filled,
                "n_filled_both": n_filled_both, "n_both_empty": both_empty,
                "coverage_a": float((ca != "").mean()) if n_items else 0.0,
                "coverage_b": float((cb != "").mean()) if n_items else 0.0,
                "caveat": "both_arms_empty" if both_empty == n_items and n_items else None,
            }
            if agreement is not None:
                enum_agree.append(agreement)
            if agreement_filled is not None:
                enum_agree_filled.append(agreement_filled)

        else:  # freetext
            na = sa.map(_normalize_cell)
            nb = sb.map(_normalize_cell)
            cov_a, cov_b = _coverage(na), _coverage(nb)
            report["columns"][c] = {
                "kind": "freetext", "coverage_a": cov_a, "coverage_b": cov_b,
                "caveat": None,
            }
            ft_cov_delta.append(cov_b - cov_a)

    report["summary"] = {
        "n_columns": len(cols),
        "n_enum_columns": kind_counts["enum"],
        "n_list_columns": kind_counts["list"],
        "n_numeric_columns": kind_counts["numeric"],
        "n_freetext_columns": kind_counts["freetext"],
        "mean_enum_agreement": float(np.mean(enum_agree)) if enum_agree else None,
        "mean_enum_agreement_filled": (
            float(np.mean(enum_agree_filled)) if enum_agree_filled else None
        ),
        "n_enum_columns_filled": len(enum_agree_filled),
        "mean_list_jaccard": float(np.mean(list_jaccard)) if list_jaccard else None,
        "mean_list_jaccard_filled": (
            float(np.mean(list_jaccard_filled)) if list_jaccard_filled else None
        ),
        "n_list_columns_filled": len(list_jaccard_filled),
        "mean_numeric_correlation": float(np.mean(num_corr)) if num_corr else None,
        "n_numeric_columns_correlated": len(num_corr),
        "mean_numeric_exact_agreement": float(np.mean(num_exact)) if num_exact else None,
        "n_numeric_columns_exact": len(num_exact),
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
    """Per-(item, column) rows where the arms' values differ.

    N-arm generalization of the spike's disagreement table: one row per
    ``(item_id, column)`` whose *canonicalized* values are not all equal (so a
    row does not appear merely because one arm wrote a hyphen where another wrote
    an en dash, or left the cell blank), carrying each arm's value as displayed.
    Capped at ``ADJUDICATION_CAP`` rows.
    """
    indexed = {}
    for arm, df in frames.items():
        d = df.drop_duplicates("item_id").copy()
        d["item_id"] = d["item_id"].astype(str)
        # An item this arm failed outright has no values to adjudicate — every
        # column would show as a spurious disagreement against the arms that
        # succeeded. (Human coder frames carry no annotated_ok — kept as-is.)
        if "annotated_ok" in d.columns:
            d = d[d["annotated_ok"].fillna(False).astype(bool)]
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
            if len({_canon(v) for v in values.values()}) > 1:
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




def _arm_price(arm: dict) -> dict | None:
    """The token prices applying to one arm's calls, or ``None`` if unknown.

    No annotation API exposes prices programmatically, so prices are
    config-maintained ``pricing = {input, output}`` (USD per 1M tokens)
    entries on backend blocks and variants; the arm's backend selection
    resolves through :func:`variants.selection_pricing`. An arm whose
    ``gen_overrides`` swap the model away from the selection's effective
    model is unpriced — the declared price no longer describes what ran.

    Args:
        arm: A parsed arm dict (``backend`` + ``gen_overrides`` keys).

    Returns:
        The ``{input, output}`` price entry, or ``None``.
    """
    from fyp.annotation.backends import get_backend, variants

    selection = arm.get("backend") or "gemini"
    overrides = arm.get("gen_overrides") or {}
    try:
        spec = variants.resolve(selection)
        model_key = "model" if spec.backend_id == "gemini" else "model_id"
        if overrides.get(model_key):
            if overrides[model_key] != get_backend(selection).effective_model_id():
                return None
        return variants.selection_pricing(selection)
    except Exception:
        return None




def _arm_cost(raw_rows: list[dict], price: dict | None = None) -> dict:
    """Aggregate token/error/latency/cost stats for one arm's raw rows."""
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
    # Approximate spend: thinking tokens are billed as output on every priced
    # backend, so they fold into the output side.
    cost_usd = None
    if price is not None:
        out_tokens = totals["candidates_tokens"] + totals["thoughts_tokens"]
        cost_usd = (totals["prompt_tokens"] * float(price.get("input", 0))
                    + out_tokens * float(price.get("output", 0))) / 1e6
    return {
        **totals,
        "n_errors": errors,
        "mean_inference_duration": float(np.mean(durations)) if durations else None,
        "cost_usd": cost_usd,
        "unpriced_rows": 0 if price is not None else len(raw_rows),
    }




def execute_run(run_id: str, arms: list[dict], item_ids: list[str],
                started_by: str = "", runner=None, progress_cb=None, cancel_cb=None,
                eval_set: str = "", name: str = "") -> dict:
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
        eval_set: name of the evaluation set the ids came from (recorded on the
            manifest so a run stays interpretable after the set is edited).
        name: optional human-readable run label (shown in run pickers and the
            report header).

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

    # Parse every arm up front (fail fast before any model call is made).
    # backend/model/temperature are optional per-arm overrides — absent keys
    # mean "production defaults", so pre-existing callers and stored runs are
    # unaffected.
    parsed_arms = []
    for arm in arms:
        contract, errors = ac.parse_and_validate(arm["text"])
        if contract is None or errors:
            raise ValueError(f"arm '{arm.get('name')}' contract invalid: {'; '.join(errors)}")
        backend_name = arm.get("backend") or "gemini"
        gen_overrides = {k: arm[k] for k in ("model", "temperature") if arm.get(k) not in (None, "")}
        gen_overrides.update(arm.get("gen_params") or {})
        _validate_arm_backend(arm.get("name"), backend_name)
        parsed_arms.append({
            "name": arm["name"],
            "source": arm.get("source", "candidate"),
            # The underlying candidate name — distinct from the arm name/label
            # when one candidate runs as several arms (e.g. once per backend).
            "candidate": arm.get("candidate") or "",
            "etag": ac._etag(arm["text"], arm.get("source", "candidate")),
            "contract": contract,
            "text": arm["text"],
            "backend": backend_name,
            "gen_overrides": gen_overrides,
        })

    item_ids = [str(i) for i in item_ids]
    platform_map = platform_map_for(item_ids)

    manifest = {
        "run_id": run_id,
        "name": str(name or "").strip()[:60],
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "started_by": started_by,
        "status": "running",
        "eval_set": eval_set,
        "item_ids": item_ids,
        "n_items": len(item_ids),
        "arms": [
            {"name": a["name"], "source": a["source"], "candidate": a["candidate"],
             "etag": a["etag"], "backend": a["backend"],
             "gen_overrides": a["gen_overrides"]}
            for a in parsed_arms
        ],
    }
    data_io.save_json(data=manifest, storage_location=LOCATION,
                      filename=_run_file(run_id, "manifest.json"))
    _update_runs_index({**{k: manifest[k] for k in
                           ("run_id", "name", "started_at", "started_by", "status",
                            "n_items", "eval_set")},
                        "arms": [a["name"] for a in parsed_arms]})

    frames: dict[str, pd.DataFrame] = {}
    costs: dict[str, dict] = {}
    try:
        for arm in parsed_arms:
            def _cb(done, total, _name=arm["name"]):
                if progress_cb:
                    progress_cb(_name, done, total)

            arm_runner = runner or _runner_for_arm(arm, cancel_cb)
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
            costs[arm["name"]] = _arm_cost(raw_rows, _arm_price(arm))

        # Pairwise comparisons + N-arm distributions/adjudication. The live
        # var_schema knows nothing about an arm's brand-new fields, so union in
        # every arm's contract-declared scales before classifying.
        scales = _scale_map()
        for arm in parsed_arms:
            scales.update(contract_scale_map(arm["contract"]))

        arm_names = [a["name"] for a in parsed_arms]
        comparisons: dict[str, dict] = {}
        compared_cols: set[str] = set()
        for i in range(len(arm_names)):
            for j in range(i + 1, len(arm_names)):
                key = f"{arm_names[i]}|{arm_names[j]}"
                comparisons[key] = compare_arms(
                    frames[arm_names[i]], frames[arm_names[j]], scales=scales,
                )
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
                               ("run_id", "name", "started_at", "started_by", "status",
                                "n_items", "eval_set")},
                            "arms": [a["name"] for a in parsed_arms],
                            "error": manifest.get("error")})

    return {
        "run_id": run_id,
        "status": manifest["status"],
        "n_items": len(item_ids),
        "eval_set": eval_set,
        "arms": [a["name"] for a in parsed_arms],
        "costs": costs,
    }
