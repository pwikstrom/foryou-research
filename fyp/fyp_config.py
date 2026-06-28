import http.client
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd
import toml
from google.api_core.exceptions import Forbidden as google_Forbidden
from google.cloud import storage as gcs_storage

# look for the folder that contains the __proj__.py file, which is the root folder for the project structure
_cwd = Path(os.getcwd())
_candidates = [_cwd] + list(_cwd.parents)
for _p in _candidates:
    if (_p / "__proj__.py").exists():
        abs_project_root_path = str(_p)
        break
else:
    raise FileNotFoundError("Could not find __proj__.py in any parent directory")
sys.path.append(abs_project_root_path)


#import fyp



def _create_local_dirs(cf: dict, verbose: bool = False):
    # create missing local folders if not using GCS for data
    if not cf['data_io']['use_gcs_for_data'] or cf['misc']['local_mode']:
        if verbose:
            print("Data is stored in locally")
            print("Cache is stored in locally")
        for k in cf["paths"].keys():
            os.makedirs(cf["paths"][k], exist_ok=True)
    # create missing local folders if not using GCS for data
    elif not cf['data_io']['use_gcs_for_cache']:
        if verbose:
            print("Cache is stored in locally")
        if not os.path.exists(cf["paths"]["cache"]):
            if verbose:
                print("Creating missing local folder for cache")
            os.makedirs(cf["paths"]["cache"], exist_ok=True)

    # Media is orthogonal to data/cache - ensure its folder exists whenever GCS media is off
    if not cf['data_io']['use_gcs_for_media'] or cf['misc']['local_mode']:
        if verbose:
            print("Media is stored locally")
        os.makedirs(cf["paths"]["media"], exist_ok=True)




def initialize(
    verbose: bool = False,
    abs_project_root_path: str = None
    ) -> dict:
    
    # ------------------------------------------------------------------
    # Locate the project root - I don't know what other people do - this works for me
    # ------------------------------------------------------------------
    if abs_project_root_path is None:

        # I put an empty __proj__.py file in the root folder of the project structure
        cwd = Path(os.getcwd())
        candidates = [cwd] + list(cwd.parents)
        for p in candidates:
            if (p / "__proj__.py").exists():
                abs_project_root_path = str(p)
                break
        else:
            raise FileNotFoundError("Could not find __proj__.py in any parent directory")
        if verbose:
            print("Project root:",abs_project_root_path)

        # add project root path to PATH since the modules are located in the project structure
        sys.path.append(abs_project_root_path)

    
    # ------------------------------------------------------------------
    # Load essential config - let it blow up if the files aren't found
    # ------------------------------------------------------------------
    config_path = os.path.join(abs_project_root_path,"config","config.toml")
    cf = toml.load(config_path)
    cf["paths"]["project_root"] = abs_project_root_path


    # ------------------------------------------------------------------
    # Use env var for secrets; fall back to config if present (avoid committing real keys)
    # ------------------------------------------------------------------
    gcp_bucket_name = os.environ.get("FYP_GCS_BUCKET_NAME")
    if gcp_bucket_name:
        cf["data_io"]["GCS_bucket_name"] = gcp_bucket_name

    gemini_env_key = os.environ.get("GEMINI_API_KEY")
    if gemini_env_key:
        cf["machine"]["key"] = gemini_env_key


    # ------------------------------------------------------------------
    # initialize paths
    # ------------------------------------------------------------------
    # Resolve relative paths against the project root for consistent file access.
    # I'm creating the paths as if they are local - if everything is GCS, these will just be
    # used as a template for the gcs paths 
    cf["paths"]["local_data"] = os.path.abspath(os.path.join(cf["paths"]["project_root"], cf["paths"]["local_data"]))

    # Resolve the local media path the same way. Accepts absolute or project-relative values.
    cf["paths"]["media"] = os.path.abspath(
        os.path.join(cf["paths"]["project_root"], cf["paths"]["local_media"])
    )
    del cf["paths"]["local_media"]


    cf["paths"]["activity_data"] = os.path.join(cf["paths"]["local_data"],"activity_data")

    # paths to zeeschuimer data
    cf["paths"]["zeeschuimer"] = os.path.join(cf["paths"]["activity_data"], "zeeschuimer")
    cf["paths"]["zeeschuimer_raw"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_raw")

    # paths to ddp data
    cf["paths"]["ddp"] = os.path.join(cf["paths"]["activity_data"], "ddp")
    cf["paths"]["ddp_raw"] = os.path.join(cf["paths"]["ddp"], "ddp_raw")

    # paths to aio data (from Australian Internet Observatory AWS)
    cf["paths"]["aio"] = os.path.join(cf["paths"]["activity_data"], "aio")
    cf["paths"]["aio_raw"] = os.path.join(cf["paths"]["aio"], "aio_raw")
    cf["paths"]["aio_participants"] = os.path.join(cf["paths"]["aio"], "aio_participants")

    # paths to scrape data
    cf["paths"]["scrape"] = os.path.join(cf["paths"]["local_data"], "scrape")

    # paths to machine annotations
    cf["paths"]["machine_annotations"] = os.path.join(cf["paths"]["local_data"], "machine_annotations")
    cf["paths"]["machine_annotations_raw"] = os.path.join(cf["paths"]["machine_annotations"], "machine_annotations_raw")
    cf["paths"]["machine_annotations_refined"] = os.path.join(cf["paths"]["machine_annotations"], "machine_annotations_refined")

    # other paths
    cf["paths"]["recoded"] = os.path.join(cf["paths"]["local_data"], "recoded")
    cf["paths"]["archive"] = os.path.join(cf["paths"]["local_data"], "archive")
    cf["paths"]["users"] = os.path.join(cf["paths"]["local_data"], "users") 
    cf["paths"]["cache"] = os.path.join(cf["paths"]["local_data"], "cache") 
    
    cf["paths"]["temp"] = os.path.join(tempfile.gettempdir(), "fyp", "")
    os.makedirs(cf["paths"]["temp"], exist_ok=True)
    

    # ------------------------------------------------------------------
    # prepare gen ai parameters for initialisation
    # ------------------------------------------------------------------
    cf["machine"]["client"] = None
    cf["machine"]["global_generation_config"] = None

    # I've used different prompts in the config. This allows for some flexibility.
    # It is expected that the parameter in the config file is a filename to a text file
    # that is located in a folder named 'prompts' in the project root. 
    for p in cf["machine"].keys():
        if "prompt" in p and isinstance(cf["machine"][p], str):
            cf["machine"][p] = os.path.join(cf["paths"]["project_root"],"prompts",cf["machine"][p])


    # ------------------------------------------------------------------
    # prepare data storage for initialisation - either gcs or local
    # ------------------------------------------------------------------
    # This is not set by the config so I'm setting it to None
    cf["data_io"]["bucket"] = None

    # If running on Cloud Run, force all storage to GCS
    if os.environ.get("K_SERVICE"):
        print("Cloud Run detected. Forcing all storage to GCS.")
        cf['data_io']['use_gcs_for_data'] = True
        cf['data_io']['use_gcs_for_cache'] = True
        cf['data_io']['use_gcs_for_media'] = True
        cf['misc']['local_mode'] = False

    # If local mode is enabled, set the GCS flags to False
    elif cf['misc']['local_mode']:
        print("Local mode is enabled. GCS data will not be used.")
        cf['data_io']['use_gcs_for_data'] = False
        cf['data_io']['use_gcs_for_cache'] = False
        cf['data_io']['use_gcs_for_media'] = False

    if cf['data_io']['use_gcs_for_data']:
        cf["gcs_paths"] = {}
        gcs_prefix = cf["data_io"].get("gcs_data_prefix", "")
        for k, v in cf["paths"].items():
            if k == "media":
                continue  # media uses data_io.gcs_media_prefix, not gcs_paths
            if isinstance(v, str) and v.startswith(cf["paths"]["local_data"]) and k != "local_data":
                rel = os.path.relpath(v, cf["paths"]["local_data"])
                if rel == ".": 
                    gcs_path = gcs_prefix
                else:
                    gcs_path = f"{gcs_prefix}/{rel}" if gcs_prefix else rel            
                cf["gcs_paths"][k] = gcs_path
        
    # create missing local folders - note that this function first checks relevant flags and
    # only creates folders if needed 
    _create_local_dirs(cf, verbose=verbose)


    return cf






# check internet connectivity
def _online_ok(url="www.qut.edu.au",
                        timeout=3):
    connection = http.client.HTTPConnection(url,
                                        timeout=timeout)
    try:
        # only header requested for fast operation
        connection.request("HEAD", "/")
        connection.close()  # connection closed
        return True
    except Exception as exep:
        print(exep)
        return False






def _connect_to_google(cf, verbose=False):

    if cf["data_io"]["bucket"] is not None:
        return cf

    cf["data_io"]["bucket"] = None

    if cf['misc']['local_mode'] or not (cf['data_io']['use_gcs_for_data'] or cf['data_io']['use_gcs_for_cache'] or cf['data_io']['use_gcs_for_media']):
        return cf

    if _online_ok():

        # Initialize a GCS storage client
        try:
            bucket_client = gcs_storage.Client()

            # Get the GCS bucket
            bucket = bucket_client.get_bucket(cf["data_io"]["GCS_bucket_name"])

            # Try to access the GCS bucket's metadata
            bucket.reload()
            cf["data_io"]["bucket"] = bucket
            print(f"Access to GCS bucket '{bucket.name}' ({bucket.location}) is authorized.")
            if verbose:
                if cf['data_io']['use_gcs_for_data']:
                    print("Data is stored in GCS")
                else:
                    print("Data is stored locally")
                if cf['data_io']['use_gcs_for_cache']:
                    print("Cache is stored in GCS")
                else:
                    print("Cache is stored locally")
                if cf['data_io']['use_gcs_for_media']:
                    print("Media is stored in GCS")
                else:
                    print("Media is stored locally")

            return cf
        
        except google_Forbidden:
            print("I don't have access to the GCS.")
        except Exception as e:
            print(f"A GCS error occurred: {e}")

    else:
        print("No internet connection. Running local mode.")
        cf['misc']['local_mode'] = True
    

    cf['data_io']['use_gcs_for_data'] = False
    cf['data_io']['use_gcs_for_cache'] = False
    cf['data_io']['use_gcs_for_media'] = False
    _create_local_dirs(cf, verbose=verbose)
    return cf







def _var_schema_path(cf) -> str:
    """Return the canonical path (local or ``gs://``) for ``var_schema.csv``.

    Single source of truth used by every read/write/freshness call so the
    web server, task runner, and migration scripts can never disagree.
    """
    if cf['data_io']['use_gcs_for_data']:
        return f"gs://{cf['data_io']['GCS_bucket_name']}/data/var_schema.csv"
    return os.path.join(cf['paths']['local_data'], "var_schema.csv")



def _var_schema_source_fingerprint(cf) -> str | None:
    """Cheap O(1) fingerprint of the on-disk schema source.

    Local: ``"{mtime_ns}:{size}"``.
    GCS: the blob's ``generation`` number (changes on every overwrite).
    Returns None if the source can't be reached (logged elsewhere).
    """
    path = _var_schema_path(cf)
    try:
        if path.startswith("gs://"):
            bucket = cf['data_io'].get('bucket')
            if bucket is None:
                return None
            blob = bucket.get_blob(path[len(f"gs://{bucket.name}/"):])
            return None if blob is None else str(blob.generation)
        st = os.stat(path)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except Exception:
        return None



def _apply_contract_accepted_labels(cf) -> None:
    """Materialize ``accepted_labels`` from the annotation contract, in memory.

    ``accepted_labels`` is NOT stored in ``var_schema.csv``; the Gemini annotation
    contract (``config/annotation_contract.toml``) is the single source for the enum
    vocabularies, so the column is rebuilt here at load and overlaid onto the
    in-memory schema that every consumer reads (recode, the version hash, the admin
    API, the UI metadata).

    A field is closed-tag — and therefore gets contract-sourced labels — when it is
    recoded as a closed categorical (``recode_func == "recode_stringified_list"``)
    and the contract defines an enum for it. Labels are the contract enum
    lower-cased (the recoded form). Every other field gets ``NA``. Membership is
    thus derived from var_schema's recode config plus the contract, so a new closed
    categorical picks up its labels automatically; free-text fields (recoded by
    e.g. ``recode_long_strings``) get no labels.

    The column is always created (NA-filled) even when the contract cannot be loaded,
    so direct consumers and the schema hash never see a missing column.

    Args:
        cf: the config dict whose ``var_schema`` DataFrame is overlaid in place.
    """
    vs = cf.get("var_schema")
    if vs is None or getattr(vs, "empty", True) or "variable_name" not in vs.columns:
        return
    # Always ensure the column exists, so nothing downstream KeyErrors and the
    # semantic hash is computed over a present column.
    if "accepted_labels" not in vs.columns:
        vs["accepted_labels"] = pd.NA
    try:
        from fyp import annotation_contract as ac

        contract = ac.load_contract()
    except Exception:
        return
    enum_labels: dict[str, str] = {}
    for field in contract.get("fields", []):
        ref = field.get("enum")
        if ref:
            values = ac.enum_values(contract, ref)
            enum_labels[field["name"]] = "[" + ", ".join(str(v).lower() for v in values) + "]"
    if not enum_labels:
        return

    # A field is closed-tag when its (derived) recode op is the list/enum cleaner.
    # recode_func is no longer a column; resolve the op via build_recode_plan.
    try:
        from fyp.recode_variables import build_recode_plan

        plan = build_recode_plan(vs.set_index("variable_name"))
    except Exception:
        plan = {}
    for idx in vs.index:
        name = vs.at[idx, "variable_name"]
        if name not in enum_labels:
            continue
        if getattr(plan.get(name), "__name__", "") == "recode_stringified_list":
            vs.at[idx, "accepted_labels"] = enum_labels[name]



def load_var_schema(cf, verbose=False):
    # Load variable schema
    var_schema_path = _var_schema_path(cf)
    if cf['data_io']['use_gcs_for_data']:
        if verbose:
            print("Loading variable schema from GCS", end="", flush=True)
    else:
        if verbose:
            print("Loading variable schema from local disk", end="", flush=True)
    try:
        cf["var_schema"] = pd.read_csv(var_schema_path, dtype_backend="pyarrow", encoding="utf-8")
        cf["_var_schema_fingerprint"] = _var_schema_source_fingerprint(cf)
        if verbose:
            print(f" - OK. Shape: {cf['var_schema'].shape}")
    except Exception:
        # var_schema not found — try to bootstrap from template
        template_path = os.path.join(cf["paths"]["project_root"], "config", "var_schema_template.csv")
        if os.path.exists(template_path):
            print(f"\nVariable schema not found at '{var_schema_path}'. Bootstrapping from template.")
            template_df = pd.read_csv(template_path, dtype_backend="pyarrow", encoding="utf-8")
            if cf['data_io']['use_gcs_for_data']:
                # Upload template to GCS
                bucket = cf['data_io'].get('bucket')
                if bucket:
                    blob = bucket.blob(f"{cf['data_io'].get('gcs_data_prefix', 'data')}/var_schema.csv")
                    blob.upload_from_filename(template_path)
                    print("Uploaded var_schema template to GCS.")
            else:
                # Copy template to local data directory
                os.makedirs(os.path.dirname(var_schema_path), exist_ok=True)
                shutil.copy2(template_path, var_schema_path)
                print(f"Copied var_schema template to '{var_schema_path}'.")
            cf["var_schema"] = template_df
        else:
            print(f"\nCRITICAL: No var_schema.csv and no template found at '{template_path}'.")
            cf["var_schema"] = pd.DataFrame(columns=[
                "source", "section", "variable_name", "display_name", "role", "scale",
                "sortable", "searchable", "web_filter_prio", "web_timeline_prio",
                "web_viz_prio", "web_display_prio",
                "description", "accepted_labels"
            ])
        cf["_var_schema_fingerprint"] = _var_schema_source_fingerprint(cf)
    # Retired columns, all now derived: ``mapper`` / ``ignore_strings`` from the
    # annotation contract (build_field_normalization); ``recode_func`` from scale +
    # source (build_recode_plan); ``unable_to_detect_policy`` from scale
    # (default_uncertain_policy — recode normalises, never imputes); the three
    # ``web_viz_*`` presentation flags now derived from the data distribution and
    # scale (``derive_log_scale`` / ``derive_bin_count`` in explorer_backend, and
    # ``scale == 'collection'`` for the timeline multi-label denominator). Drop
    # them so a stale on-disk CSV never surfaces them to the admin editor or the hash.
    cf["var_schema"] = cf["var_schema"].drop(
        columns=["mapper", "ignore_strings", "recode_func", "unable_to_detect_policy",
                 "web_viz_log", "web_viz_multi_label", "web_viz_bins"],
        errors="ignore",
    )
    _apply_contract_accepted_labels(cf)
    return cf



class VarSchemaConflict(Exception):
    """Raised when a save is attempted with a stale etag."""



def _read_var_schema_bytes(cf) -> bytes:
    """Return the raw bytes of ``var_schema.csv`` from its canonical source."""
    path = _var_schema_path(cf)
    if path.startswith("gs://"):
        bucket = cf['data_io'].get('bucket')
        if bucket is None:
            raise FileNotFoundError(f"GCS bucket not configured; cannot read {path}")
        blob_path = path[len(f"gs://{bucket.name}/"):]
        blob = bucket.blob(blob_path)
        return blob.download_as_bytes()
    with open(path, "rb") as f:
        return f.read()



def compute_var_schema_etag(cf=None) -> str:
    """SHA-256 of the on-disk schema bytes — opaque concurrency token."""
    import hashlib
    if cf is None:
        cf = fyp_cf
    try:
        data = _read_var_schema_bytes(cf)
    except Exception:
        return "missing"
    return hashlib.sha256(data).hexdigest()



def save_var_schema(df: pd.DataFrame, expected_etag: str | None = None,
                    cf=None, verbose: bool = False) -> dict:
    """Persist ``df`` to the canonical ``var_schema.csv`` location.

    1. If ``expected_etag`` is given, verify it matches the current on-disk
       etag.  Mismatch raises :class:`VarSchemaConflict` and the file is
       left untouched — caller is expected to surface a 409 to the admin.
    2. Write a timestamped backup ``var_schema_YYYYMMDDTHHMMSSZ.csv``
       alongside the live file (best-effort; failure does not abort save).
    3. Write the new CSV in place.
    4. Reload into ``cf['var_schema']`` so subsequent reads see the update.

    Returns ``{"etag": new_etag, "fingerprint": new_fingerprint}``.
    """
    from datetime import datetime, timezone
    if cf is None:
        cf = fyp_cf

    if expected_etag is not None:
        current = compute_var_schema_etag(cf)
        if current != expected_etag:
            raise VarSchemaConflict(
                f"var_schema etag mismatch: expected {expected_etag!r}, "
                f"on-disk is {current!r}. Reload the editor and re-apply."
            )

    path = _var_schema_path(cf)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Best-effort timestamped backup of the *current* live file (not the
    # incoming df) so we always have an "undo" target.
    try:
        existing = _read_var_schema_bytes(cf)
        if path.startswith("gs://"):
            bucket = cf['data_io'].get('bucket')
            blob_path = path[len(f"gs://{bucket.name}/"):]
            backup_path = blob_path.replace("var_schema.csv",
                                            f"var_schema_{timestamp}.csv")
            bucket.blob(backup_path).upload_from_string(existing,
                                                       content_type="text/csv")
        else:
            backup_path = path.replace("var_schema.csv",
                                       f"var_schema_{timestamp}.csv")
            with open(backup_path, "wb") as f:
                f.write(existing)
        if verbose:
            print(f"Wrote backup to {backup_path}")
    except Exception as e:
        if verbose:
            print(f"Backup write failed (continuing): {e}")

    # ``accepted_labels`` is contract-owned (rebuilt in memory at load from
    # annotation_contract.toml), so it is never persisted to the CSV. ``mapper`` /
    # ``ignore_strings`` / ``recode_func`` / ``unable_to_detect_policy`` are retired
    # columns whose behavior is derived (contract + scale/source) — never persist.
    df = df.drop(
        columns=["accepted_labels", "mapper", "ignore_strings", "recode_func",
                 "unable_to_detect_policy"],
        errors="ignore",
    )

    # Atomic-ish write: local goes through a temp file + os.replace;
    # GCS overwrites are atomic at the blob level.
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    if path.startswith("gs://"):
        bucket = cf['data_io'].get('bucket')
        blob_path = path[len(f"gs://{bucket.name}/"):]
        bucket.blob(blob_path).upload_from_string(csv_bytes, content_type="text/csv")
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(csv_bytes)
        os.replace(tmp_path, path)

    load_var_schema(cf, verbose=False)
    return {
        "etag": compute_var_schema_etag(cf),
        "fingerprint": cf.get("_var_schema_fingerprint"),
    }



def reload_var_schema_if_changed(cf=None, verbose: bool = False) -> bool:
    """Re-read ``var_schema.csv`` only if its source has changed on disk.

    Designed to be called at the entry point of every Cloud Task worker so
    long-lived task-runner containers don't keep using a stale in-memory
    schema after an admin edit on the web service.  Cheap (one stat / one
    GCS metadata call) so it's safe to call on every task.

    Returns True if the schema was reloaded, False otherwise.
    """
    if cf is None:
        cf = fyp_cf
    current = _var_schema_source_fingerprint(cf)
    cached = cf.get("_var_schema_fingerprint")
    if current is not None and current == cached:
        return False
    if verbose:
        print(f"var_schema fingerprint changed ({cached!r} → {current!r}); reloading.")
    load_var_schema(cf, verbose=verbose)
    return True








PROJECT_ROOT = Path(abs_project_root_path)


# Initialize things
fyp_cf = initialize()
fyp_cf = _connect_to_google(fyp_cf, verbose = True)
fyp_cf = load_var_schema(fyp_cf, verbose=True)


QUEUE_SCRAPER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_scraper.py"
QUEUE_ANNOTATOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_annotator.py"
QUEUE_ANNOTATOR_BATCH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_annotator_batch.py"
META_REFRESH_GROUPS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_meta_refresh_groups.py"
TIMELINES_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_timelines_refresh.py"
RECODE_REFRESH_STUDIES_SCRIPT = PROJECT_ROOT / "web_interface" / "run_recode_refresh_studies.py"
PCA_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_pca_refresh.py"
SEQUENCE_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_sequence_refresh.py"
EMBEDDINGS_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_embeddings_refresh.py"
VIDEO_MAP_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_video_map_refresh.py"
CONSOLIDATE_ENRICHMENT_SCRIPT = PROJECT_ROOT / "web_interface" / "run_consolidate_enrichment.py"
INGEST_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_ingest_refresh.py"
AIO_FETCH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_aio_fetch.py"
COLLECTION_METADATA_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_collection_metadata_refresh.py"
COLLECTION_DELETE_SCRIPT = PROJECT_ROOT / "web_interface" / "run_collection_delete.py"
PYTHON_EXEC = sys.executable
