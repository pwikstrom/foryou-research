import http.client
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pandas as pd
import toml
from google.api_core.exceptions import Forbidden as google_Forbidden
from google.cloud import storage as gcs_storage

# Root discovery (FYP_CONFIG_PATH override, __proj__.py walk, the sys.path
# append) and the PROJECT_ROOT / worker-script constants live in
# fyp.core.paths; importing it preserves the historical import-time side
# effects. The redundant-alias form re-exports every name so external code
# keeps importing them from fyp.fyp_config.
from fyp.core.paths import (
    AB_EVAL_SCRIPT as AB_EVAL_SCRIPT,
    AIO_FETCH_SCRIPT as AIO_FETCH_SCRIPT,
    COLLECTION_DELETE_SCRIPT as COLLECTION_DELETE_SCRIPT,
    COLLECTION_METADATA_REFRESH_SCRIPT as COLLECTION_METADATA_REFRESH_SCRIPT,
    CONSOLIDATE_ENRICHMENT_SCRIPT as CONSOLIDATE_ENRICHMENT_SCRIPT,
    EMBEDDINGS_REFRESH_SCRIPT as EMBEDDINGS_REFRESH_SCRIPT,
    INGEST_REFRESH_SCRIPT as INGEST_REFRESH_SCRIPT,
    META_REFRESH_GROUPS_SCRIPT as META_REFRESH_GROUPS_SCRIPT,
    PCA_REFRESH_SCRIPT as PCA_REFRESH_SCRIPT,
    PROJECT_ROOT as PROJECT_ROOT,
    PYTHON_EXEC as PYTHON_EXEC,
    QUEUE_ANNOTATOR_BATCH_SCRIPT as QUEUE_ANNOTATOR_BATCH_SCRIPT,
    QUEUE_ANNOTATOR_SCRIPT as QUEUE_ANNOTATOR_SCRIPT,
    QUEUE_SCRAPER_SCRIPT as QUEUE_SCRAPER_SCRIPT,
    RECODE_REFRESH_STUDIES_SCRIPT as RECODE_REFRESH_STUDIES_SCRIPT,
    RETOKENISE_HASHTAGS_SCRIPT as RETOKENISE_HASHTAGS_SCRIPT,
    SEQUENCE_REFRESH_SCRIPT as SEQUENCE_REFRESH_SCRIPT,
    SESSIONS_REFRESH_SCRIPT as SESSIONS_REFRESH_SCRIPT,
    TIMELINES_REFRESH_SCRIPT as TIMELINES_REFRESH_SCRIPT,
    VIDEO_MAP_REFRESH_SCRIPT as VIDEO_MAP_REFRESH_SCRIPT,
    abs_project_root_path as abs_project_root_path,
)


#import fyp


# Fallback for [site].repo_url when neither config.toml nor FYP_REPO_URL
# supplies one (e.g. a stripped-down config): the canonical public repository
# the public pages link to for issues, installation and licence.
DEFAULT_REPO_URL = "https://github.com/pwikstrom/foryou-research"

# Fallback for [site].participant_placeholder_domain — the domain of the fake
# p-N@<domain> accounts minted for participants who left no email address.
DEFAULT_PARTICIPANT_PLACEHOLDER_DOMAIN = "foryouresearch.net"


def _load_dotenv(project_root: str, verbose: bool = False) -> list[str]:
    """Load ``KEY=VALUE`` lines from ``<project_root>/.env`` into the environment.

    Variables already present in ``os.environ`` always win — the file only
    fills gaps, so an exported value can never be shadowed by it. Parsing is
    deliberately minimal: blank lines and ``#`` comments are skipped, an
    optional ``export `` prefix is dropped, and matching single/double quotes
    around the value are stripped. A missing or unreadable file is a no-op.

    (FYP_CONFIG_PATH itself cannot come from ``.env`` — the root the file is
    found under is derived from that variable, so it must be exported.)

    Returns:
        The names of the variables actually applied.
    """
    applied: list[str] = []
    try:
        with open(os.path.join(project_root, ".env"), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return applied
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        stripped = stripped.removeprefix("export ").lstrip()
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    if applied and verbose:
        print(f"Loaded {len(applied)} value(s) from .env: {', '.join(applied)}")
    return applied


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




def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base`` in place.

    Nested dicts are merged key-by-key; any other value type in ``override``
    replaces the corresponding value in ``base``.

    Args:
        base: The dict to update.
        override: The dict whose values win.

    Returns:
        ``base``, for convenience.
    """
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base




# Former flat [machine] keys that belong to the Gemini backend — the canonical
# home is now [machine.gemini] (config schema 2026-07). Includes retired knobs
# (use_structured_output, prompt, ...) so any old config hoists completely.
_LEGACY_GEMINI_KEYS = (
    "key", "model", "vertexai", "project", "location",
    "http_options_api_version", "http_options_timeout",
    "temperature", "max_output_tokens", "thinking_budget",
    "max_retries", "retry_base_delay", "media_resolution", "version_label",
    "use_structured_output", "use_generated_prompt", "prompt",
    "presence_penalty", "frequency_penalty",
)




def _normalize_machine_config(cf: dict) -> None:
    """Hoist the legacy flat ``[machine]`` layout into the per-backend schema.

    The canonical schema nests every annotation backend under its own block
    (``[machine.gemini]``, ``[machine.qwen_api]``, ...), with variants at
    ``[machine.<backend>.variants.<name>]``. Old configs and overlays
    (``config.local.toml`` written before the restructure, or by an old
    ``scripts/setup.py``) still use flat ``[machine]`` Gemini keys and the
    flat ``[machine.variants]`` table — this hoists them in place so every
    reader sees exactly one location. A flat key wins over the nested one:
    flat keys can only come from an explicit old overlay/config, which is
    deliberate user intent overriding the committed nested default.

    Args:
        cf: The loaded (and overlay-merged) config dict, mutated in place.
    """
    machine = cf.get("machine")
    if not isinstance(machine, dict):
        return
    gemini = machine.setdefault("gemini", {})
    for key in _LEGACY_GEMINI_KEYS:
        if key in machine:
            gemini[key] = machine.pop(key)

    legacy_variants = machine.pop("variants", None)
    if isinstance(legacy_variants, dict):
        for name, block in legacy_variants.items():
            if not isinstance(block, dict):
                continue
            backend_id = block.pop("backend", None) or "gemini"
            nested = machine.setdefault(backend_id, {}).setdefault("variants", {})
            nested.setdefault(name, block)

    # The short-lived model-keyed [machine.pricing] table (2026-07) moved to a
    # per-block `pricing` inline table; a leftover copy is dropped, not read.
    if machine.pop("pricing", None) is not None:
        print("[CONFIG] Ignoring legacy [machine.pricing] - prices now live as "
              "`pricing = {input=..., output=...}` on each backend/variant block.")




def _localize_default_path(configured: str, home_subdir: str) -> str:
    """Make a committed POSIX default data path usable on the current OS.

    The tracked ``config.toml`` ships macOS-style absolute defaults (e.g.
    ``~/fyp_local``). On Windows those are drive-relative and
    point at another user's profile, so a POSIX-absolute default is redirected
    to a per-user location under the home directory. Anything a user set
    deliberately — a relative path or a path carrying a Windows drive/UNC
    prefix (typically via ``config.local.toml``) — is returned unchanged, and
    on macOS/Linux the value is always returned unchanged.

    Args:
        configured: The path value read from config (already overlay-merged).
        home_subdir: Fallback location under ``~`` used on Windows when
            ``configured`` is a bare POSIX-absolute path.

    Returns:
        A path string appropriate for the current platform.
    """
    if os.name == "nt":
        drive, _ = os.path.splitdrive(configured)
        posix_absolute = not drive and configured[:1] in ("/", "\\")
        if posix_absolute:
            return os.path.join(os.path.expanduser("~"), home_subdir)
    return configured




def initialize(
    verbose: bool = False,
    abs_project_root_path: str = None
    ) -> dict:
    
    # ------------------------------------------------------------------
    # Locate the project root - I don't know what other people do - this works for me
    # ------------------------------------------------------------------
    env_config_path = os.environ.get("FYP_CONFIG_PATH")
    if abs_project_root_path is None and env_config_path:
        # FYP_CONFIG_PATH names the config TOML directly; the project root is
        # derived from it (normally <root>/config/config.toml), so no
        # __proj__.py discovery is needed — the reuse path for importing fyp
        # from another project. Absent env var, behavior is unchanged.
        env_config_path = str(Path(env_config_path).resolve())
        abs_project_root_path = str(Path(env_config_path).parent.parent)
        if verbose:
            print("Project root (from FYP_CONFIG_PATH):", abs_project_root_path)
        sys.path.append(abs_project_root_path)
    else:
        env_config_path = None

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


    # A gitignored .env at the project root is loaded here, before any of the
    # os.environ reads below, so users never need `set -a; source .env`.
    # Already-exported variables always take precedence.
    _load_dotenv(abs_project_root_path, verbose=verbose)

    # ------------------------------------------------------------------
    # Load essential config - let it blow up if the files aren't found
    # ------------------------------------------------------------------
    config_path = env_config_path or os.path.join(abs_project_root_path,"config","config.toml")
    cf = toml.load(config_path)

    # Optional machine-local overlay: config/config.local.toml (gitignored) is
    # deep-merged over the committed config, so collaborators can point
    # paths.local_data etc. at their own machine without editing the tracked
    # file. Absent file = no change. See config/config.local.toml.example.
    # (With FYP_CONFIG_PATH the overlay is looked up next to the named file.)
    local_config_path = os.path.join(os.path.dirname(config_path), "config.local.toml")
    if os.path.exists(local_config_path):
        _deep_merge(cf, toml.load(local_config_path))
        if verbose:
            print(f"Applied local config overlay: {local_config_path}")

    cf["paths"]["project_root"] = abs_project_root_path

    # Hoist any legacy flat [machine] layout (old configs/overlays) into the
    # per-backend schema BEFORE the env-secret writes below target it.
    _normalize_machine_config(cf)


    # ------------------------------------------------------------------
    # Use env var for secrets; fall back to config if present (avoid committing real keys)
    # ------------------------------------------------------------------
    gcp_bucket_name = os.environ.get("FYP_GCS_BUCKET_NAME")
    if gcp_bucket_name:
        cf["data_io"]["GCS_bucket_name"] = gcp_bucket_name

    gemini_env_key = os.environ.get("GEMINI_API_KEY")
    if gemini_env_key:
        cf["machine"]["gemini"]["key"] = gemini_env_key

    # Vertex project: the committed default is empty (no third party should
    # bill the author's project). Deployed services fall back to the
    # GCP_PROJECT_ID env var they already carry; FYP_VERTEX_PROJECT overrides.
    if not cf["machine"]["gemini"].get("project"):
        cf["machine"]["gemini"]["project"] = (
            os.environ.get("FYP_VERTEX_PROJECT")
            or os.environ.get("GCP_PROJECT_ID")
            or ""
        )

    # Site/branding values ([site]): committed defaults are empty; env vars
    # override so deployed instances configure them without a config overlay.
    site = cf.setdefault("site", {})
    for env_name, site_key in (
        ("FYP_CONTACT_EMAIL", "contact_email"),
        ("FYP_MAIL_SENDER", "mail_sender"),
        ("FYP_APP_URL", "app_url"),
    ):
        env_value = os.environ.get(env_name)
        if env_value:
            site[site_key] = env_value.strip()
        else:
            site.setdefault(site_key, "")

    # repo_url is about the software rather than the operator, so its default
    # is the canonical repository instead of "" - a fresh install still links
    # to the source, the issue tracker and the installation guide. A fork
    # overrides it in config.local.toml / FYP_REPO_URL; an explicit empty
    # value hides the source-code links entirely.
    repo_env = os.environ.get("FYP_REPO_URL")
    if repo_env is not None:
        site["repo_url"] = repo_env.strip()
    else:
        site.setdefault("repo_url", DEFAULT_REPO_URL)

    # Domain for the fake p-N@<domain> addresses minted for participants who
    # donated demographics but no email. Never a real mailbox; kept
    # per-install so a third-party instance doesn't mint our domain.
    placeholder_env = os.environ.get("FYP_PARTICIPANT_PLACEHOLDER_DOMAIN")
    if placeholder_env:
        site["participant_placeholder_domain"] = placeholder_env.strip()
    else:
        site.setdefault("participant_placeholder_domain", DEFAULT_PARTICIPANT_PLACEHOLDER_DOMAIN)


    # ------------------------------------------------------------------
    # initialize paths
    # ------------------------------------------------------------------
    # Fail loud if the config.local.toml template placeholder was copied but
    # never edited — silently creating a "~CHANGE-ME~" directory helps nobody.
    for placeholder_key in ("local_data", "local_media"):
        if "~CHANGE-ME~" in str(cf["paths"].get(placeholder_key, "")):
            raise ValueError(
                "config/config.local.toml still contains the ~CHANGE-ME~ "
                f"placeholder in paths.{placeholder_key} - edit the file or "
                "run: python scripts/setup.py"
            )

    # The committed defaults are home-relative ("~/fyp_local") so a fresh
    # clone works on any machine; expanduser resolves them per-user on every
    # OS while absolute paths (e.g. from config.local.toml) pass through.
    cf["paths"]["local_data"] = os.path.expanduser(cf["paths"]["local_data"])
    cf["paths"]["local_media"] = os.path.expanduser(cf["paths"]["local_media"])

    # Redirect legacy macOS-style POSIX-absolute values to a per-user home
    # location when running on Windows (no-op on macOS/Linux/Cloud Run). Runs
    # before the project-root join below, so a Windows drive path set in
    # config.local.toml still wins.
    cf["paths"]["local_data"] = _localize_default_path(cf["paths"]["local_data"], "fyp_local")
    cf["paths"]["local_media"] = _localize_default_path(
        cf["paths"]["local_media"], os.path.join("fyp_local", "media")
    )

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
    cf["machine"]["gemini"]["client"] = None


    # ------------------------------------------------------------------
    # prepare data storage for initialisation - either gcs or local
    # ------------------------------------------------------------------
    # This is not set by the config so I'm setting it to None
    cf["data_io"]["bucket"] = None

    # If running on Cloud Run, force all storage to GCS. FYP_FORCE_GCS allows a
    # local process (e.g. a residential-IP scrape-queue drain) to opt into the
    # same prod-GCS storage resolution without setting K_SERVICE, which would
    # also flip cookie sourcing, task-status and Cloud Tasks dispatch.
    if os.environ.get("K_SERVICE") or os.environ.get("FYP_FORCE_GCS"):
        source = "Cloud Run detected." if os.environ.get("K_SERVICE") else "FYP_FORCE_GCS set."
        print(f"{source} Forcing all storage to GCS.")
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






# Well-known HEAD-tolerant host used for the default connectivity probe.
_DEFAULT_PROBE_HOST = "connectivitycheck.gstatic.com"


# check internet connectivity
def _online_ok(url=_DEFAULT_PROBE_HOST,
                        timeout=1):
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

    # On Cloud Run (K_SERVICE set) connectivity and GCS creds are guaranteed, so
    # skip the external HTTP HEAD probe — it only adds latency (and up to a full
    # timeout on a slow response) to every cold start.
    probe_host = cf["misc"].get("connectivity_probe_host") or _DEFAULT_PROBE_HOST
    if os.environ.get("K_SERVICE") or _online_ok(url=probe_host):

        # Initialize a GCS storage client
        try:
            bucket_client = gcs_storage.Client()

            # Lightweight bucket handle — bucket() makes no network call (unlike
            # get_bucket()). Bucket metadata (and any access error) resolves
            # lazily on first real object I/O, so we avoid a GCS round-trip at
            # import on every cold start. Missing credentials still raise from
            # Client() above and fall through to the local-mode fallback below.
            bucket = bucket_client.bucket(cf["data_io"]["GCS_bucket_name"])
            cf["data_io"]["bucket"] = bucket
            print(f"GCS bucket '{bucket.name}' handle ready (metadata resolved lazily).")
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

    # FYP_FORCE_GCS means the process must operate on GCS data (e.g. a local
    # scrape-queue drain against prod) — silently degrading to local storage
    # would make it read/write the wrong data, so fail hard instead.
    if os.environ.get("FYP_FORCE_GCS"):
        raise RuntimeError("FYP_FORCE_GCS is set but the GCS connection failed - refusing local fallback.")

    cf['data_io']['use_gcs_for_data'] = False
    cf['data_io']['use_gcs_for_cache'] = False
    cf['data_io']['use_gcs_for_media'] = False
    _create_local_dirs(cf, verbose=verbose)
    return cf







def _var_schema_source_fingerprint(cf) -> str | None:
    """Cheap fingerprint of the synthesized schema's RUNTIME inputs.

    The schema is synthesized from the contract TOMLs, the presentation store,
    and the version registries. Most contract TOMLs are baked into the deploy,
    but the **annotation** contract can also be uploaded to data storage at
    runtime (``users/annotation_contract.toml`` — see
    ``fyp.annotation_contract.refresh_runtime_contract``), so its mtime is folded
    in here too. The presentation store and version registries also change at
    runtime (registrations, the versions-in-data snapshot, admin edits).
    ``reload_var_schema_if_changed`` compares this at every Cloud Task entry so
    long-lived containers pick those changes up. Returns None on failure.
    """
    try:
        from fyp import var_presentation as vp
        from fyp import annotation_contract as ac
        import fyp.data_io as data_io

        parts = [f"presentation:{vp.compute_presentation_etag()}"]
        for fname in (
            "annotation_versions.json",
            "scrape_versions.json",
            "activity_versions.json",
            "annotation_versions_in_data.json",
        ):
            try:
                if data_io.exists(storage_location="recoded", filename=fname):
                    mtime = data_io.getmtime(storage_location="recoded", filename=fname)
                    parts.append(f"{fname}:{mtime}")
                else:
                    parts.append(f"{fname}:absent")
            except Exception:
                parts.append(f"{fname}:unknown")
        # Runtime annotation contract (one getmtime; absent → baked default).
        try:
            mtime = data_io.getmtime(
                storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME
            )
            parts.append(f"{ac.RUNTIME_FILENAME}:{mtime}")
        except FileNotFoundError:
            parts.append(f"{ac.RUNTIME_FILENAME}:absent")
        except Exception:
            parts.append(f"{ac.RUNTIME_FILENAME}:unknown")
        return "|".join(parts)
    except Exception:
        return None



def _apply_contract_accepted_labels(cf) -> None:
    """Materialize ``accepted_labels`` from the annotation contract, in memory.

    ``accepted_labels`` is NOT stored in ``var_schema.csv``; the Gemini annotation
    contract (``config/annotation_contract.toml``) is the single source for the enum
    vocabularies, so the column is rebuilt here at load and overlaid onto the
    in-memory schema that every consumer reads (recode, the version hash, the admin
    API, the UI metadata).

    A field is closed-tag — and therefore gets contract-sourced labels — when the
    contract defines an enum for it and declares a closed scale (``categorical`` or
    ``list``). Labels are the contract enum lower-cased (the recoded form). Every
    other field gets ``NA``. Membership is derived from the contract alone, so the
    overlay does not depend on any other overlay having restored ``scale`` on the
    frame first (the raw CSV blanks contract-owned cells); free-text fields have
    no enum and get no labels.

    The column is always created (NA-filled) even when the contract cannot be loaded,
    so direct consumers and the schema hash never see a missing column.

    Args:
        cf: the config dict whose ``var_schema`` DataFrame is overlaid in place.
    """
    vs = cf.get("var_schema")
    if vs is None or "variable_name" not in getattr(vs, "columns", []):
        # An EMPTY frame with the right columns is the synthesis skeleton —
        # the overlay proceeds and injects every contract-owned row.
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
        scale = str(ac.effective_scale(field) or "").strip().lower()
        if ref and scale in ("categorical", "list"):
            values = ac.enum_values(contract, ref)
            enum_labels[field["name"]] = "[" + ", ".join(str(v).lower() for v in values) + "]"
    if not enum_labels:
        return

    for idx in vs.index:
        name = vs.at[idx, "variable_name"]
        if name in enum_labels:
            vs.at[idx, "accepted_labels"] = enum_labels[name]




def _apply_contract_variable_metadata(cf) -> None:
    """Materialize Gemini variable metadata from the annotation contract, in memory.

    For variables the contract owns (its flattened output columns), ``role`` /
    ``scale`` / ``display_name`` / ``description`` are NOT stored in
    ``var_schema.csv`` — the annotation contract (``config/annotation_contract.toml``)
    is the single source, and the columns are overlaid here at load. Separately,
    EVERY annotation-owned row (current contract or a past version's registry
    snapshot) is grouped under a single ``"AI Annotations"`` UI section, replacing
    whatever ``section`` the CSV carried.

    The overlay is PER-ROW: non-Gemini rows keep their CSV values untouched. It
    runs after :func:`_apply_contract_accepted_labels` and before any consumer
    reads the overlaid ``role`` / ``scale`` (recode, the schema hash, the admin
    API, the UI metadata), mirroring the accepted-labels overlay. Degrades to a
    no-op when the contract cannot be loaded, so the columns are never missing.

    Args:
        cf: the config dict whose ``var_schema`` DataFrame is overlaid in place.
    """
    vs = cf.get("var_schema")
    if vs is None or "variable_name" not in getattr(vs, "columns", []):
        # An EMPTY frame with the right columns is the synthesis skeleton —
        # the overlay proceeds and injects every contract-owned row.
        return
    for col in ("role", "scale", "display_name", "description", "section"):
        if col not in vs.columns:
            vs[col] = pd.NA
    try:
        from fyp import annotation_contract as ac

        meta = ac.contract_column_metadata(ac.load_contract())
    except Exception:
        return
    # Legacy fields owned by PAST annotation versions (the union of the version
    # registry's per-version metadata snapshots), minus anything the CURRENT
    # contract still owns — so superseded Gemini fields (e.g. ``trend`` /
    # ``australian_relevance``) stay contract-owned/read-only instead of degrading
    # into editable orphans. Current-contract metadata always wins.
    try:
        from fyp import annotation_versioning as av

        legacy_meta = {k: v for k, v in av.union_field_metadata().items() if k not in meta}
    except Exception as e:
        # Loud on purpose: a silent failure here once cost hours — boot frames
        # lost all legacy metadata and the schema hash drifted per-instance.
        print(f"WARNING: legacy annotation metadata union unavailable ({e}); overlay incomplete.")
        legacy_meta = {}
    for idx in vs.index:
        name = vs.at[idx, "variable_name"]
        owned = meta.get(name) or legacy_meta.get(name)
        if owned:
            for col in ("role", "scale", "display_name", "description"):
                if owned.get(col) is not None:
                    vs.at[idx, col] = owned[col]
            # Every annotation-owned row lands under one UI section.
            vs.at[idx, "section"] = "AI Annotations"
    # Inject contract/legacy fields absent from the frame so they surface as owned
    # rows, grouped under "AI Annotations". With the synthesized (CSV-free) schema
    # every annotation row enters here; on a legacy CSV load only genuinely
    # missing rows are added.
    all_owned = {**legacy_meta, **meta}
    if all_owned:
        present = set(vs["variable_name"].astype("string"))
        missing = [name for name in all_owned if name not in present]
        if missing:
            rows = []
            for name in missing:
                owned = all_owned[name]
                rows.append({
                    "variable_name": name,
                    "role": owned.get("role"),
                    "scale": owned.get("scale"),
                    "display_name": owned.get("display_name"),
                    "description": owned.get("description"),
                    "section": "AI Annotations",
                    "skip_recode": False,
                })
            cf["var_schema"] = pd.concat([vs, pd.DataFrame(rows)], ignore_index=True)



def _apply_contract_scrape_metadata(cf) -> None:
    """Materialize scrape-variable metadata from the scrape contract, in memory.

    The scrape contract (``config/scrape_contract.toml``) owns ``role`` /
    ``scale`` / ``display_name`` / ``description`` / ``section`` for the canonical
    scrape columns, the same way the annotation contract owns them for the Gemini
    columns. This (a) overlays that metadata onto the matching ``var_schema`` rows
    and (b) injects any contract-owned scrape column missing from
    ``var_schema.csv``, so the TOML — not the CSV — is the source of truth for the
    scrape field set (a new platform's fields appear with no CSV edit). The
    overlay is PER-ROW and additive: non-scrape rows are untouched. It runs after
    :func:`_apply_contract_variable_metadata` in :func:`load_var_schema` and
    degrades to a no-op when the contract cannot be loaded.

    Args:
        cf: the config dict whose ``var_schema`` DataFrame is overlaid in place.
    """
    vs = cf.get("var_schema")
    if vs is None or "variable_name" not in getattr(vs, "columns", []):
        # An EMPTY frame with the right columns is the synthesis skeleton —
        # the overlay proceeds and injects every contract-owned row.
        return
    for col in ("role", "scale", "display_name", "description", "section", "skip_recode"):
        if col not in vs.columns:
            vs[col] = pd.NA
    try:
        from fyp import scrape_contract as sc

        contract = sc.load_contract()
        meta = sc.contract_column_metadata(contract)
    except Exception:
        return
    # Union in fields owned by PAST scrape-contract versions (the registry's
    # per-version metadata snapshots) so a field a future contract stops
    # emitting stays contract-owned/read-only. Current contract wins.
    try:
        from fyp import scrape_versioning as sv

        meta = {**{k: v for k, v in sv.union_field_metadata().items() if k not in meta}, **meta}
    except Exception as e:
        print(f"WARNING: legacy scrape metadata union unavailable ({e}); overlay incomplete.")
    # Migrate legacy TikTok-named rows to canonical in-memory, so an un-migrated
    # var_schema.csv (existing local or prod deployment) self-heals at load.
    # RETIRED_TO_GENERIC names must NOT join this replace: several map to one
    # target (duplicate rows) and they must stay as read-only legacy-union rows.
    vs["variable_name"] = vs["variable_name"].replace(sc.LEGACY_COLUMN_ALIASES)
    for idx in vs.index:
        owned = meta.get(vs.at[idx, "variable_name"])
        if owned:
            for col in ("role", "scale", "display_name", "description", "section"):
                if owned.get(col) is not None:
                    vs.at[idx, col] = owned[col]
            vs.at[idx, "skip_recode"] = _owned_skip_recode(owned)
    present = set(vs["variable_name"].astype("string"))
    missing = [name for name in meta if name not in present]
    if missing:
        rows = []
        for name in missing:
            owned = meta[name]
            rows.append({
                "variable_name": name,
                "role": owned.get("role"),
                "scale": owned.get("scale"),
                "display_name": owned.get("display_name"),
                "description": owned.get("description"),
                "section": owned.get("section"),
                "skip_recode": _owned_skip_recode(owned),
            })
        cf["var_schema"] = pd.concat([vs, pd.DataFrame(rows)], ignore_index=True)




def _owned_skip_recode(owned: dict) -> bool:
    """Resolve a contract/legacy metadata dict's recode-skip flag.

    Current contracts emit an explicit ``skip_recode`` boolean. Legacy registry
    snapshots (persisted before the flag existed) instead carry the retired
    ``source`` string, whose ``"derived:"`` prefix meant the same thing — kept
    as a read-only fallback so retired derived fields stay skipped.
    """
    if "skip_recode" in owned:
        return bool(owned["skip_recode"])
    return str(owned.get("source") or "").startswith("derived:")




def _overlay_contract_metadata(cf, meta: dict) -> None:
    """Overlay a contract's column metadata onto var_schema and inject missing rows.

    Shared by the activity and derived contract overlays (the scrape overlay is
    separate — it also self-heals legacy column names). Per-row and additive: rows
    the contract does not own are untouched. ``meta`` maps column name →
    ``{role, scale, display_name, description, section, skip_recode}``.

    Args:
        cf: the config dict whose ``var_schema`` DataFrame is overlaid in place.
        meta: the contract's ``contract_column_metadata`` payload.
    """
    vs = cf.get("var_schema")
    if vs is None or "variable_name" not in getattr(vs, "columns", []):
        # An EMPTY frame with the right columns is the synthesis skeleton —
        # the overlay proceeds and injects every contract-owned row.
        return
    for col in ("role", "scale", "display_name", "description", "section", "skip_recode"):
        if col not in vs.columns:
            vs[col] = pd.NA
    if not meta:
        return
    for idx in vs.index:
        owned = meta.get(vs.at[idx, "variable_name"])
        if owned:
            for col in ("role", "scale", "display_name", "description", "section"):
                if owned.get(col) is not None:
                    vs.at[idx, col] = owned[col]
            vs.at[idx, "skip_recode"] = _owned_skip_recode(owned)
    present = set(vs["variable_name"].astype("string"))
    missing = [name for name in meta if name not in present]
    if missing:
        rows = []
        for name in missing:
            owned = meta[name]
            rows.append({
                "variable_name": name,
                "role": owned.get("role"),
                "scale": owned.get("scale"),
                "display_name": owned.get("display_name"),
                "description": owned.get("description"),
                "section": owned.get("section"),
                "skip_recode": _owned_skip_recode(owned),
            })
        cf["var_schema"] = pd.concat([vs, pd.DataFrame(rows)], ignore_index=True)




def _apply_contract_activity_metadata(cf) -> None:
    """Materialize activity-variable metadata from the activity contract, in memory.

    Mirrors :func:`_apply_contract_scrape_metadata` for
    ``config/activity_contract.toml``: the activity contract owns
    role/scale/display_name/description/section for the canonical activity columns
    (incl. ``item_id``, the ``local_*`` features, ``session_id`` and the
    ``activity_contract_version`` provenance stamp), and injects any missing ones
    (``source_platform`` / ``raw_file`` / ``activity_contract_version`` are absent
    from ``var_schema.csv`` today). Degrades to a no-op if the contract cannot load.
    """
    try:
        from fyp import activity_contract as acy

        contract = acy.load_contract()
        meta = acy.contract_column_metadata(contract)
    except Exception:
        return
    # Union in fields owned by PAST activity-contract versions (registry
    # snapshots); current contract wins — mirrors the scrape/annotation overlays.
    try:
        from fyp import activity_versioning as av_act

        meta = {**{k: v for k, v in av_act.union_field_metadata().items() if k not in meta}, **meta}
    except Exception as e:
        print(f"WARNING: legacy activity metadata union unavailable ({e}); overlay incomplete.")
    _overlay_contract_metadata(cf, meta)




def _apply_contract_derived_metadata(cf) -> None:
    """Materialize enrichment-variable metadata from the derived contract, in memory.

    Mirrors the activity overlay for ``config/derived_contract.toml`` (the
    merge-time calculated + niche columns). Injects ``scraped_fail`` (absent from
    ``var_schema.csv`` today, so the produced column gains its metadata).
    """
    try:
        from fyp import derived_contract as dc

        contract = dc.load_contract()
        meta = dc.contract_column_metadata(contract)
    except Exception:
        return
    _overlay_contract_metadata(cf, meta)



# The synthesized schema's column set (accepted_labels is added by its overlay;
# the internal boolean ``skip_recode`` — contract-owned, drives the recode plan —
# is added typed by the skeleton in :func:`load_var_schema`).
VAR_SCHEMA_COLUMNS = [
    "section", "variable_name", "display_name", "role", "scale",
    "web_filter_prio", "web_timeline_prio", "web_viz_prio", "web_display_prio",
    "description",
]




def load_var_schema(cf, verbose=False):
    """Synthesize the in-memory var_schema — the CSV is retired.

    Sources, in order:
      1. the four contract TOMLs (+ the version registries' legacy snapshots)
         own every row's semantic + display metadata, injected by the overlays;
      2. ``var_presentation.json`` owns the four ``web_*_prio`` membership
         columns;
      3. ``accepted_labels`` is rebuilt from the annotation contract's enums.

    The result is identical (rows / role / scale / accepted_labels) to what the
    legacy CSV path produced after its overlays, so the study hash is unchanged
    by the retirement.
    """
    from fyp import var_presentation as vp
    from fyp import annotation_contract as ac

    # 0. Refresh the runtime annotation-contract snapshot BEFORE the overlays, so
    #    every load_contract() call below (and the accepted_labels overlay) sees
    #    an uploaded contract. Every rebuild path — boot, ?force_reload=1, Cloud
    #    Task entry — flows through here, so this is the single refresh point that
    #    keeps long-lived containers current. Never raises.
    ac.refresh_runtime_contract()

    # 1. Presentation store (a fresh install starts with empty prio surfaces).
    presentation = vp.load_presentation()
    if presentation is None:
        print("WARNING: no presentation store — all prio surfaces start empty.")
        presentation = vp.empty_presentation()

    # 2. Empty typed skeleton; the contract overlays below inject every owned row.
    cf["var_schema"] = pd.DataFrame(
        {c: pd.Series(dtype="string[pyarrow]") for c in VAR_SCHEMA_COLUMNS}
    )
    cf["var_schema"]["skip_recode"] = pd.Series(dtype="bool[pyarrow]")
    # 3. Contract overlays — on the empty skeleton these inject every owned row
    #    (annotation incl. registry legacy fields, scrape, activity, derived) and
    #    the accepted_labels overlay fills the enum vocabularies.
    _apply_contract_variable_metadata(cf)
    _apply_contract_scrape_metadata(cf)
    _apply_contract_activity_metadata(cf)
    _apply_contract_derived_metadata(cf)
    _apply_contract_accepted_labels(cf)

    # Normalize legacy role strings (factor/group_factor/feature) to the current
    # vocabulary. The contract TOMLs are rewritten, but registry field_metadata
    # snapshots keep the old strings on disk forever — this single choke point
    # means downstream matchers only ever see the new values.
    if "role" in cf["var_schema"].columns:
        try:
            from fyp.recode_variables import normalize_role
            cf["var_schema"]["role"] = cf["var_schema"]["role"].map(
                lambda r: normalize_role(r) if pd.notna(r) else r
            )
        except Exception:
            pass

    # Injection concatenates plain-dict rows, which can degrade column dtypes —
    # re-coerce the metadata columns to pyarrow strings for downstream consumers.
    for _meta_col in ("role", "scale", "display_name", "description", "section", "variable_name"):
        if _meta_col in cf["var_schema"].columns:
            try:
                cf["var_schema"][_meta_col] = cf["var_schema"][_meta_col].astype("string[pyarrow]")
            except Exception:
                pass
    if "skip_recode" in cf["var_schema"].columns:
        try:
            cf["var_schema"]["skip_recode"] = (
                cf["var_schema"]["skip_recode"].fillna(False).astype("bool[pyarrow]")
            )
        except Exception:
            pass

    # 4. Fill the four prio membership columns from the presentation store
    #    ("1" = ON — any non-blank numeric counts as membership downstream).
    vs = cf["var_schema"]
    for surface, col in vp.SURFACE_TO_PRIO_COLUMN.items():
        members = set(presentation.get("surfaces", {}).get(surface, []) or [])
        vs[col] = pd.Series(
            ["1" if n in members else pd.NA for n in vs["variable_name"]],
            dtype="string[pyarrow]", index=vs.index,
        )

    cf["_var_schema_fingerprint"] = _var_schema_source_fingerprint(cf)
    if verbose:
        print(f"Synthesized variable schema from contracts + presentation store. Shape: {cf['var_schema'].shape}")
    return cf



def reload_var_schema_if_changed(cf=None, verbose: bool = False) -> bool:
    """Re-synthesize the var_schema only if one of its sources has changed.

    Sources are the contract TOMLs (incl. the runtime annotation contract),
    the presentation store, and the version registries — see
    :func:`_var_schema_source_fingerprint`.

    Designed to be called at the entry point of every Cloud Task worker so
    long-lived task-runner containers don't keep using a stale in-memory
    schema after an admin edit on the web service.  Cheap (one stat / one
    GCS metadata call) so it's safe to call on every task.

    Returns True if the schema was reloaded, False otherwise.
    """
    if cf is None:
        cf = get_config()
    current = _var_schema_source_fingerprint(cf)
    cached = cf.get("_var_schema_fingerprint")
    if current is not None and current == cached:
        return False
    if verbose:
        print(f"var_schema fingerprint changed ({cached!r} → {current!r}); reloading.")
    load_var_schema(cf, verbose=verbose)
    return True








# The heavy init (config load, GCS connect, var_schema synthesis) is lazy: it
# runs on first access of ``fyp_cf`` — served by the module ``__getattr__``
# below (PEP 562) — instead of at module import. ``from fyp.fyp_config import
# fyp_cf`` therefore still triggers init at the consumer's import time and
# always binds the same singleton dict.
_fyp_cf: dict | None = None
_fyp_cf_lock = threading.Lock()




def get_config() -> dict:
    """Return the memoized config dict, running the heavy init on first call.

    Performs exactly the three boot steps that used to run at module import
    (``initialize()`` → ``_connect_to_google()`` → ``load_var_schema()``) and
    emits the ``[BOOT]`` timing line once, so the real cold-start bottleneck is
    provable from a single grep of the boot logs (Cloud Run and local).

    Returns:
        The singleton config dict (the same object on every call).
    """
    global _fyp_cf
    if _fyp_cf is not None:
        return _fyp_cf
    with _fyp_cf_lock:
        if _fyp_cf is not None:
            return _fyp_cf
        _boot_t0 = time.perf_counter()
        cf = initialize()
        # Publish before the connect/var_schema steps: those call into modules
        # (data_io, var_presentation, the *_versioning registries) whose lazy
        # ``_cf()`` accessors re-enter this module's ``fyp_cf`` attribute. The
        # fast path above serves them the in-progress dict — the exact
        # mid-init semantics the old module-level ``fyp_cf = initialize()``
        # binding provided (guarded by tests/unit/test_import_cycle_hash.py).
        _fyp_cf = cf
        _boot_t1 = time.perf_counter()
        cf = _connect_to_google(cf, verbose=True)
        _boot_t2 = time.perf_counter()
        cf = load_var_schema(cf, verbose=True)
        _boot_t3 = time.perf_counter()
        print(
            f"[BOOT] fyp_config init: initialize={_boot_t1 - _boot_t0:.3f}s "
            f"connect_google={_boot_t2 - _boot_t1:.3f}s "
            f"load_var_schema={_boot_t3 - _boot_t2:.3f}s "
            f"total={_boot_t3 - _boot_t0:.3f}s",
            flush=True,
        )
    return _fyp_cf




def __getattr__(name: str):
    """Serve ``fyp_cf`` lazily (PEP 562), triggering the heavy init on first use."""
    if name == "fyp_cf":
        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


