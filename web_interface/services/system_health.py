"""System-health checks: per-platform test scrape + Gemini ping.

Each run performs a metadata-only test scrape of one recently-scraped item per
registered platform (validating the result against the scrape contract and the
platform's historical field-fill profile), probes the first bytes of the item's
media URL, and pings Gemini with a ~1-token generation call.

Runs in a daemon thread on the web service — kicked off at boot (skipped while
a persisted result is fresh) and manually from the System Information page.
Results are persisted to a server-local temp file after every check so the
polling UI shows partial progress and the document survives a same-machine
restart. Health is deliberately per-server: a local dev server and Cloud Run
each keep their own document (a fresh Cloud Run container starts empty, so the
boot check re-runs there).
"""

import json
import logging
import os
import tempfile
import threading
import time
from datetime import UTC, datetime

import google.genai
import pandas as pd
import requests

from fyp.annotation import machine_annotation
from fyp.core import data_io
from fyp.fyp_config import get_config
from fyp.scrape import scrape_contract as sc
from fyp.scrape.platform_scraper import THROTTLE_CATEGORIES, cleanup_temp_files, get_scraper

from .worker_status import _cached_cookie_health

logger = logging.getLogger(__name__)

# Server-local (never GCS): each server's health reflects its own environment.
_HEALTH_PATH = os.path.join(tempfile.gettempdir(), "fyp_system_health.json")
_DEFAULT_MAX_AGE_HOURS = 6.0

# A base field must be non-null in at least this fraction of a platform's
# historical scrape rows to be *expected* in the test row.
_FILL_THRESHOLD = 0.9

# Base fields stamped by the scrape orchestrator at save time, never present on
# a fresh canonicalized row — excluded from the expected-fill comparison.
_ORCHESTRATOR_FIELDS = {"storage_link", "scrape_ts"}

_MEDIA_PROBE_BYTES = 64 * 1024
_MEDIA_PROBE_TIMEOUT_S = 15

_run_lock = threading.Lock()      # held for the duration of a run
_state_lock = threading.Lock()    # guards the in-memory document
_current: dict | None = None      # in-memory result (authoritative between saves)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()






def _never_run_stub() -> dict:
    """Return the document served before any health check has ever run."""
    return {"schema_version": 1, "overall": "never_run", "trigger": None,
            "started_at": None, "finished_at": None, "checks": {}}






def _overall(checks: dict) -> str:
    """Aggregate per-check statuses into an overall status (fail > warn > ok)."""
    statuses = [c.get("status") for c in checks.values()]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    if "ok" in statuses:
        return "ok"
    return "fail"






# Cookie freshness status → chip status (green/yellow/red/grey).
_COOKIE_TO_CHIP = {
    "healthy": "ok",
    "expiring_soon": "warn",
    "stale": "warn",
    "expired": "fail",
    "missing": "fail",
    "unknown": "unknown",
}

# Chip status severity for the "worst wins" combination.
_CHIP_SEVERITY = {"unknown": 0, "ok": 1, "warn": 2, "fail": 3}


def _worst_chip(*statuses: str) -> str:
    """Return the most severe chip status (fail > warn > ok > unknown)."""
    worst = "unknown"
    for status in statuses:
        if _CHIP_SEVERITY.get(status, 0) > _CHIP_SEVERITY.get(worst, 0):
            worst = status
    return worst






def _chip_from_check_status(status: str | None) -> str:
    """Map a health-check status to a chip status (running/never_run → unknown)."""
    return status if status in ("ok", "warn", "fail") else "unknown"






def _platform_summary(check: dict, cookie: dict) -> str:
    """Build a one-line tooltip summary from a platform check + cookie health."""
    parts = []
    if check.get("message"):
        parts.append(f"Scrape: {check['message']}")
    else:
        parts.append("Scrape not yet checked")
    media = check.get("media") or {}
    if media.get("status") in ("warn", "fail"):
        parts.append(f"Media: {media.get('message') or media['status']}")
    if cookie.get("status"):
        parts.append(f"Cookie: {cookie.get('message') or cookie['status']}")
    return " · ".join(parts)






def derive_card_health(live_cookie: dict | None = None) -> dict:
    """Collapse the health doc + live cookie health into enrichment-card chips.

    Produces one green/yellow/red/grey status per scraper platform (combining
    that platform's test-scrape + media result with the freshest cookie status)
    and one for annotation (from the Gemini ping) — the shape the enrichment
    tab's chips consume. Never raises.

    Args:
        live_cookie: optional ``{platform: cookie_health_dict}`` from the
            enrichment endpoint's 5-minute cache, preferred over the (possibly
            hours-old) cookie captured inside the last health check.

    Returns:
        ``{"ran": bool, "platforms": {p: {status, summary, checked_at}},
        "annotation": {status, summary, checked_at}}``. ``status`` is one of
        ``ok``/``warn``/``fail``/``unknown`` (→ ``cookie-pill--{ok|warn|bad|unknown}``).
    """
    doc = get_health()
    checks = doc.get("checks") or {}
    ran = doc.get("overall") not in (None, "never_run")
    live_cookie = live_cookie or {}

    platforms: dict[str, dict] = {}
    try:
        platform_list = sc.platforms(sc.load_contract())
    except Exception:
        platform_list = list(live_cookie.keys())

    for platform in platform_list:
        check = checks.get(f"scrape_{platform}") or {}
        cookie = live_cookie.get(platform) or check.get("cookie") or {}
        status = _worst_chip(
            _chip_from_check_status(check.get("status")),
            _COOKIE_TO_CHIP.get(cookie.get("status", "unknown"), "unknown"),
        )
        platforms[platform] = {
            "status": status,
            "summary": _platform_summary(check, cookie),
            "checked_at": check.get("checked_at"),
        }

    gemini = checks.get("gemini") or {}
    annotation = {
        "status": _chip_from_check_status(gemini.get("status")),
        "summary": gemini.get("message") or "Gemini not yet checked",
        "checked_at": gemini.get("checked_at"),
    }

    return {"ran": ran, "platforms": platforms, "annotation": annotation}






def is_running() -> bool:
    """True while a health-check run is in flight in this process."""
    if _run_lock.acquire(blocking=False):
        _run_lock.release()
        return False
    return True






def get_health() -> dict:
    """Return the current health document. Never raises.

    Order of precedence: the in-memory document of a live/finished run in this
    process, else the server-local temp file, else a ``never_run`` stub. A
    persisted document stuck in ``running`` (a previous instance died mid-run)
    is downgraded to the aggregate of its completed checks so the UI never
    polls forever.
    """
    with _state_lock:
        if _current is not None:
            return dict(_current)

    try:
        if os.path.exists(_HEALTH_PATH):
            with open(_HEALTH_PATH, encoding="utf-8") as f:
                doc = json.load(f)
            if isinstance(doc, dict) and doc.get("checks") is not None:
                if doc.get("overall") == "running" and not is_running():
                    doc["overall"] = _overall(doc["checks"])
                    doc["interrupted"] = True
                return doc
    except Exception as e:
        logger.warning(f"Could not load persisted system health: {e}")
    return _never_run_stub()






def _persist(doc: dict) -> None:
    """Store the document in memory and best-effort write the local temp file."""
    global _current
    with _state_lock:
        _current = dict(doc)
    try:
        with open(_HEALTH_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f)
    except Exception as e:
        logger.warning(f"Could not persist system health: {e}")






def start_health_check(trigger: str) -> bool:
    """Spawn the health-check worker thread. Returns False when one is running.

    Args:
        trigger: ``"boot"`` or ``"manual"`` — recorded on the result document.
    """
    if not _run_lock.acquire(blocking=False):
        return False

    def _worker() -> None:
        try:
            _run_all_checks(trigger)
        except Exception as e:
            logger.error(f"System health run failed: {e}")
        finally:
            _run_lock.release()

    threading.Thread(target=_worker, name="system-health", daemon=True).start()
    return True






def maybe_start_boot_check() -> None:
    """Boot hook: run a health check unless a persisted result is still fresh.

    Freshness window comes from config ``[web] health_check_max_age_hours``
    (default 6; 0 forces a run every boot). Never raises — a health-check
    problem must not break app startup.
    """
    try:
        max_age_h = float(get_config().get("web", {}).get(
            "health_check_max_age_hours", _DEFAULT_MAX_AGE_HOURS))
        doc = get_health()
        finished = doc.get("finished_at")
        if finished and max_age_h > 0:
            age_s = (datetime.now(UTC) - datetime.fromisoformat(finished)).total_seconds()
            if age_s < max_age_h * 3600:
                logger.info(f"System health check skipped — last result is {age_s / 3600:.1f}h old")
                return
        start_health_check("boot")
    except Exception as e:
        logger.warning(f"Boot health check could not start: {e}")






def _run_all_checks(trigger: str) -> None:
    """Worker body: run every check sequentially, persisting after each."""
    doc = {"schema_version": 1, "overall": "running", "trigger": trigger,
           "started_at": _now_iso(), "finished_at": None, "checks": {}}
    _persist(doc)

    status_df = _load_status_frame()
    fill_profiles = _load_fill_profiles()

    for platform in sc.platforms(sc.load_contract()):
        key = f"scrape_{platform}"
        doc["checks"][key] = {"status": "running", "message": "Checking...",
                              "detail": None, "duration_s": None, "checked_at": None}
        _persist(doc)
        try:
            doc["checks"][key] = _check_platform(platform, status_df, fill_profiles.get(platform, []))
        except Exception as e:
            doc["checks"][key] = {"status": "fail", "message": "Health check crashed",
                                  "detail": repr(e), "duration_s": None, "checked_at": _now_iso()}
        try:
            doc["checks"][key]["cookie"] = _cached_cookie_health(platform)
        except Exception:
            doc["checks"][key]["cookie"] = {"status": "unknown", "message": "Cookie health unavailable"}
        _persist(doc)

    doc["checks"]["gemini"] = {"status": "running", "message": "Checking...",
                               "detail": None, "duration_s": None, "checked_at": None}
    _persist(doc)
    try:
        doc["checks"]["gemini"] = _check_gemini()
    except Exception as e:
        doc["checks"]["gemini"] = {"status": "fail", "message": "Gemini check crashed",
                                   "detail": repr(e), "duration_s": None, "checked_at": _now_iso()}
    doc["overall"] = _overall(doc["checks"])
    doc["finished_at"] = _now_iso()
    _persist(doc)






def _load_status_frame() -> pd.DataFrame | None:
    """Load the minimal enrichment-status frame used to pick test items."""
    try:
        if not data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            return None
        return data_io.load_parquet_selective(
            storage_location="recoded", filename="enrichment_status.parquet",
            columns=["source_platform", "scraped_ok"], set_index="item_id")
    except Exception as e:
        logger.warning(f"Could not load enrichment status for health check: {e}")
        return None






def _pick_test_item(status_df: pd.DataFrame | None, platform: str) -> str | None:
    """Return a recently-scraped item id for a platform, or None.

    Takes the last matching row: the frame is rebuilt from collections in
    append order and carries no scrape timestamp, so the tail approximates the
    most recently ingested item. Rows without a ``source_platform`` column
    (legacy single-platform files) count as the default platform.
    """
    if status_df is None or status_df.empty or "scraped_ok" not in status_df.columns:
        return None
    mask = status_df["scraped_ok"].fillna(False).astype(bool)
    if "source_platform" in status_df.columns:
        mask &= status_df["source_platform"] == platform
    elif platform != sc.default_platform(sc.load_contract()):
        return None
    matching = status_df.index[mask]
    return str(matching[-1]) if len(matching) else None






def _load_fill_profiles() -> dict[str, list[str]]:
    """Compute each platform's historically-expected base fields.

    Loads the consolidated scrapes parquet (column-projected to the contract's
    base fields) and returns, per platform, the base columns non-null in at
    least ``_FILL_THRESHOLD`` of that platform's rows — minus the
    orchestrator-stamped fields a fresh canonicalized row can never carry.
    Missing/unreadable file (fresh install) → empty profiles, so the fill
    comparison is skipped and only the structural check applies.
    """
    try:
        contract = sc.load_contract()
        label = get_config()["labels"]["SCRAPES_LABEL"]
        filename = f"{label}_recoded.parquet"
        if not data_io.exists(storage_location="recoded", filename=filename):
            return {}
        base_cols = [c for c in sc.base_field_names(contract) if c not in _ORCHESTRATOR_FIELDS]
        df = data_io.load_parquet_selective(
            storage_location="recoded", filename=filename,
            columns=list(dict.fromkeys(["source_platform", *base_cols])))
        if df is None or "source_platform" not in df.columns:
            return {}
        profiles: dict[str, list[str]] = {}
        for platform, group in df.groupby("source_platform"):
            if not len(group):
                continue
            profiles[str(platform)] = [
                c for c in base_cols
                if c in group.columns and group[c].notna().mean() >= _FILL_THRESHOLD
            ]
        return profiles
    except Exception as e:
        logger.warning(f"Could not compute scrape fill profiles: {e}")
        return {}






def _check_row_format(scraper, raw_df: pd.DataFrame, expected_fields: list[str]) -> tuple[str, str | None, str | None]:
    """Validate a fetched raw row against the contract and historical fill.

    Runs the row through the production canonicalization path
    (``prepare_raw_batch`` → ``canonicalize_batch``), then checks that every
    historically-expected base field came back non-null.

    Returns:
        ``(status, message, detail)`` — status ``"ok"`` with a filled-field
        count message when the row is consistent; ``"warn"`` with fill counts
        plus the empty expected fields on fill drift; ``"fail"`` when
        canonicalization itself breaks.
    """
    try:
        canonical = scraper.canonicalize_batch(scraper.prepare_raw_batch(raw_df.copy()))
    except Exception as e:
        return "fail", "Metadata format drift: canonicalization failed", repr(e)

    row = canonical.iloc[0]
    empty = [f for f in expected_fields
             if f not in canonical.columns or pd.isna(row[f])]
    total = len(expected_fields)
    if empty:
        # Rates and plays_per_day are all derived from play_count; when only
        # those are missing the cause is an environment-dependent play_count
        # fetch (e.g. Instagram's supplemental authenticated count lookup),
        # not a schema/format problem.
        play_count_derived = set(sc.per_k_sources(sc.load_contract())) | {"plays_per_day"}
        if set(empty) <= play_count_derived:
            message = ("play_count unavailable in this environment — "
                       "play_count-derived field(s) came back empty")
        else:
            message = "Metadata format drift: historically-filled field(s) came back empty"
        return ("warn", message,
                f"{total - len(empty)} of {total} expected fields filled OK · "
                f"empty: {', '.join(empty)}")
    return "ok", f"all {total} expected fields filled" if total else None, None






def _probe_media(scraper, item_id: str) -> dict:
    """Request the first bytes of the item's media URL, then abandon it.

    Resolves the direct CDN URL via the scraper's ``media_probe_url`` hook and
    issues a single ranged GET, reading one chunk before closing the
    connection — proving the CDN serves bytes without downloading the file.
    Failures are reported but never fail the platform check (media from
    datacenter IPs is environmentally flaky by design).
    """
    t0 = time.monotonic()
    try:
        target = scraper.media_probe_url(item_id)
    except Exception as e:
        return {"status": "warn", "message": "Media URL resolution failed",
                "detail": repr(e), "bytes_read": 0,
                "duration_s": round(time.monotonic() - t0, 2)}
    if not target or not target.get("url"):
        return {"status": "skipped", "message": "No media URL available for this item",
                "detail": None, "bytes_read": 0, "duration_s": None}

    try:
        headers = {**(target.get("headers") or {}), "Range": f"bytes=0-{_MEDIA_PROBE_BYTES - 1}"}
        resp = requests.get(target["url"], headers=headers, stream=True,
                            timeout=_MEDIA_PROBE_TIMEOUT_S)
        try:
            chunk = next(resp.iter_content(chunk_size=_MEDIA_PROBE_BYTES), b"")
        finally:
            resp.close()
        duration = round(time.monotonic() - t0, 2)
        if resp.status_code in (200, 206) and chunk:
            return {"status": "ok",
                    "message": f"CDN served {len(chunk) // 1024}KB in {duration}s",
                    "detail": None, "bytes_read": len(chunk), "duration_s": duration}
        return {"status": "warn",
                "message": f"CDN responded HTTP {resp.status_code} with {len(chunk)} bytes",
                "detail": None, "bytes_read": len(chunk), "duration_s": duration}
    except Exception as e:
        return {"status": "warn", "message": "Media probe request failed",
                "detail": repr(e), "bytes_read": 0,
                "duration_s": round(time.monotonic() - t0, 2)}






def _media_probe_bot_walled(media: dict) -> bool:
    """Return True when the media probe failed on a bot wall / rate limit.

    The probe reports raw exception text, not a classified category, so this
    matches the bot-check and rate-limit phrasings the platform classifiers
    key on (normalizing YouTube's typographic apostrophe).
    """
    if media.get("status") != "warn":
        return False
    text = f"{media.get('message') or ''} {media.get('detail') or ''}".lower().replace("’", "'")
    return any(kw in text for kw in ("not a bot", "not a robot", "rate-limit",
                                     "rate limit", "too many requests", "429"))






def _check_platform(platform: str, status_df: pd.DataFrame | None,
                    expected_fields: list[str]) -> dict:
    """Test-scrape one item for a platform and classify the outcome.

    Metadata-only (``save_media=False``); the fetched row is validated against
    the contract + historical fill profile and then discarded — nothing is
    written to the scrape parquets or queues.
    """
    item_id = _pick_test_item(status_df, platform)
    if item_id is None:
        return {"status": "warn",
                "message": "No test item available (no successfully scraped items yet)",
                "detail": None, "duration_s": None, "checked_at": _now_iso(),
                "item_id": None}

    scraper = get_scraper(platform)
    save_path = tempfile.mkdtemp(prefix="fyp_health_")
    t0 = time.monotonic()
    try:
        raw = scraper.fetch(item_id, save_media=False, save_path=save_path)
    finally:
        cleanup_temp_files(save_path, item_id)
    duration = round(time.monotonic() - t0, 2)

    result = {"status": "ok", "message": "", "detail": None, "duration_s": duration,
              "checked_at": _now_iso(), "item_id": item_id}

    if raw.empty:
        error_type = raw.attrs.get("error_type")
        classified = scraper.classify_error(error_type)
        result["detail"] = f"{classified}: {raw.attrs.get('error_detail', '')}".strip(": ")
        if error_type in THROTTLE_CATEGORIES:
            result["status"] = "warn"
            result["message"] = (f"Throttled/bot-checked fetching {item_id} — "
                                 "likely environmental (datacenter IP), not a code failure")
        elif classified.startswith("transient:"):
            result["status"] = "warn"
            result["message"] = f"Transient failure fetching {item_id}"
        else:
            result["status"] = "fail"
            result["message"] = f"Scrape failed for {item_id}"
        return result

    fmt_status, fmt_message, fmt_detail = _check_row_format(scraper, raw, expected_fields)
    if fmt_status != "ok":
        result["status"] = fmt_status
        result["message"] = fmt_message
        result["detail"] = fmt_detail
    else:
        result["message"] = f"Fetched metadata for {item_id} in {duration}s"
        if fmt_message:
            result["message"] += f" · {fmt_message}"

    result["media"] = _probe_media(scraper, item_id)
    if result["status"] == "ok" and result["media"]["status"] == "warn":
        result["status"] = "warn"
        result["message"] += f" — media probe: {result['media']['message']}"
    elif fmt_status == "warn" and _media_probe_bot_walled(result["media"]):
        # The bot wall that broke the media probe also degrades the metadata
        # response (e.g. YouTube's player response carries `duration`), so the
        # missing fields are environmental, not format drift.
        result["message"] += (" — likely environmental: media probe hit a bot "
                              "wall / rate limit on this IP, which also degrades metadata")
    return result






def _check_gemini() -> dict:
    """Ping Gemini with a ~1-token generation call to prove auth/quota/model."""
    machine_annotation.initialize_machine()
    client = get_config()["machine"].get("client")
    if client is None:
        return {"status": "fail",
                "message": "Gemini client failed to initialize (offline or bad credentials)",
                "detail": None, "duration_s": None, "checked_at": _now_iso()}

    model = get_config()["machine"]["model"]
    t0 = time.monotonic()
    try:
        client.models.generate_content(
            model=model, contents="ping",
            config=google.genai.types.GenerateContentConfig(
                max_output_tokens=1,
                thinking_config=google.genai.types.ThinkingConfig(thinking_budget=0)))
        duration = round(time.monotonic() - t0, 2)
        return {"status": "ok", "message": f"{model} responded in {duration}s",
                "detail": None, "duration_s": duration, "checked_at": _now_iso()}
    except Exception as e:
        return {"status": "fail", "message": f"{model} generation call failed",
                "detail": repr(e), "duration_s": round(time.monotonic() - t0, 2),
                "checked_at": _now_iso()}
