"""Per-platform cookie plumbing for the platform scrapers.

Generalizes the cookie handling that previously lived in :mod:`fyp.tiktok_dl`:
each platform keeps a Netscape-format cookie file at
``gs://<bucket>/secrets/{platform}_cookies.txt`` (Cloud Run, cached in ``/tmp``
for six hours) or reads cookies straight from the local Chrome profile (dev).

All functions take the platform key (``"tiktok"``, ``"instagram"``,
``"youtube"``) and derive paths from it — adding a platform needs no edit here.
"""

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)





# How long to trust the locally-cached copy before re-pulling from GCS.
# 6h so a freshly-uploaded cookies.txt is picked up by all running containers
# without a redeploy.
_COOKIE_CACHE_TTL_SEC = 6 * 3600

# One download lock per platform so 8+ workers starting at once don't race to
# download the same file (and platforms never block each other).
_DOWNLOAD_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)
_LOCKS_GUARD = threading.Lock()

# Netscape cookie-file magic header. Mirrors the check in Python's
# ``http.cookiejar.MozillaCookieJar._really_load`` (which yt-dlp inherits), so a
# file that passes this passes yt-dlp's own loader and one that fails it would
# have been rejected by yt-dlp anyway. Used to reject a truncated/empty cache.
_NETSCAPE_MAGIC_RE = re.compile(r"#( Netscape)? HTTP Cookie File")

# Disposable per-yt-dlp-call copies of the cookie file live here (see
# _private_cookie_copy). Reaped by age so they never accumulate; the system temp
# dir is /tmp on Cloud Run (an in-memory tmpfs), so the copies are kept tiny and
# short-lived. `gettempdir()` keeps this correct on Windows (%TEMP%) too.
_COOKIE_WORK_DIR = os.path.join(tempfile.gettempdir(), "fyp_cookie_work")
_COPY_TTL_SEC = 300





def _cf():
    """Lazy config accessor — keeps this module import-cycle safe."""
    from fyp.fyp_config import fyp_cf
    return fyp_cf





def _local_path(platform: str) -> str:
    """Local cache path for a platform's cookie file (writable on Cloud Run)."""
    return os.path.join(tempfile.gettempdir(), f"{platform}_cookies.txt")





def _gcs_blob_name(platform: str) -> str:
    """GCS object path for a platform's cookie file (within the bucket)."""
    return f"secrets/{platform}_cookies.txt"





def _download_lock(platform: str) -> threading.Lock:
    """Return the per-platform download lock (defaultdict access is guarded)."""
    with _LOCKS_GUARD:
        return _DOWNLOAD_LOCKS[platform]





def _looks_like_netscape(path: str) -> bool:
    """True if ``path``'s first line carries the Netscape cookie-file header.

    Guards against a truncated/empty cache: a valid cookies.txt starts with a
    ``# Netscape HTTP Cookie File`` (or ``# HTTP Cookie File``) line, and yt-dlp
    rejects anything else with "does not look like a Netscape format cookies
    file". A zero-length or half-written file fails this check.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return False
    return bool(first_line) and bool(_NETSCAPE_MAGIC_RE.search(first_line))





def _fresh_and_valid(local_path: str) -> bool:
    """True if the cached cookie file exists, is within TTL, and is well-formed.

    A file that fails the Netscape-header check (e.g. left truncated by an older
    code path in a still-warm container) is treated as a miss so the caller
    re-pulls a clean copy from GCS.
    """
    if not os.path.exists(local_path):
        return False
    try:
        age = time.time() - os.path.getmtime(local_path)
    except OSError:
        return False
    if age >= _COOKIE_CACHE_TTL_SEC:
        return False
    if not _looks_like_netscape(local_path):
        logger.warning(
            "Cached cookie file %s failed Netscape-format validation — refetching",
            local_path,
        )
        return False
    return True





def _reap_stale_copies() -> None:
    """Delete disposable cookie copies older than ``_COPY_TTL_SEC`` (best effort).

    Deleting a copy mid-flight is safe: yt-dlp reads the cookiefile only at
    construction and writes it back only at context exit, and that write-back is
    exactly what we intend to discard.
    """
    try:
        now = time.time()
        for name in os.listdir(_COOKIE_WORK_DIR):
            fpath = os.path.join(_COOKIE_WORK_DIR, name)
            try:
                if now - os.path.getmtime(fpath) > _COPY_TTL_SEC:
                    os.remove(fpath)
            except OSError:
                pass
    except OSError:
        pass





def _private_cookie_copy(platform: str, src: str) -> str:
    """Return a private, disposable copy of ``src`` for a single yt-dlp call.

    yt-dlp rewrites its ``cookiefile`` (non-atomic truncate-then-write) on every
    ``YoutubeDL`` context exit. Concurrent scraper threads sharing one cookie
    file therefore race — one thread's save truncates the file while another's
    ``YoutubeDL()`` construction is loading it, yielding "does not look like a
    Netscape format cookies file". Handing each call its own copy isolates that
    write-back so the shared canonical cache is never truncated. The refreshed
    session cookies yt-dlp would persist are intentionally discarded — GCS is
    the source of truth and the cache is re-pulled on its own TTL.

    Falls back to the shared path if the copy can't be made (best-effort auth
    beats dropping to unauthenticated scraping).
    """
    try:
        os.makedirs(_COOKIE_WORK_DIR, exist_ok=True)
        _reap_stale_copies()
        fd, dst = tempfile.mkstemp(prefix=f"{platform}_", suffix=".txt",
                                   dir=_COOKIE_WORK_DIR)
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            shutil.copyfileobj(f, out)
        return dst
    except OSError as e:
        logger.warning("Could not make private %s cookie copy (%s) — using shared "
                       "path; concurrent yt-dlp write-back may race", platform, e)
        return src





def _env_cookie_file(platform: str) -> str:
    """Cookie-file path from env: platform-specific var first, then legacy."""
    specific = os.environ.get(f"YTDLP_COOKIE_FILE_{platform.upper()}", "")
    if specific and os.path.exists(specific):
        return specific
    legacy = os.environ.get("YTDLP_COOKIE_FILE", "")
    if legacy and os.path.exists(legacy):
        return legacy
    return ""





def _is_local_dev() -> bool:
    """True when running on a dev machine with a browser (not Cloud Run/Docker)."""
    return not os.environ.get("K_SERVICE") and os.path.exists("/Applications")





def ensure_cookie_file(platform: str) -> str | None:
    """Lazily fetch a platform's cookie file from GCS to ``/tmp``.

    Only runs on Cloud Run (``K_SERVICE`` env var set). Returns the path to
    the local cookie file if available, ``None`` if no cookies could be
    obtained (in which case scraping continues without authentication).

    Caching: the local file is re-used for six hours before re-pulling.
    Concurrent calls are serialised by a per-platform lock so only one
    download happens per refresh cycle.

    Args:
        platform: platform key, e.g. ``"tiktok"``.

    Returns:
        Path to the locally-cached cookie file, or ``None``.
    """
    if not os.environ.get("K_SERVICE"):
        return None

    local_path = _local_path(platform)

    # Fast path — local copy is fresh AND well-formed.
    if _fresh_and_valid(local_path):
        return local_path

    with _download_lock(platform):
        # Re-check inside the lock — another thread may have just downloaded.
        if _fresh_and_valid(local_path):
            return local_path

        blob_name = _gcs_blob_name(platform)
        try:
            bucket = _cf()["data_io"].get("bucket")
            if bucket is None:
                logger.warning("No GCS bucket configured; cannot fetch %s cookies", platform)
                return None

            blob = bucket.blob(blob_name)
            if not blob.exists():
                logger.warning(
                    "Cookie file not found at gs://%s/%s — running without "
                    "authentication. Upload a Netscape-format cookies.txt "
                    "to enable session-authenticated scraping.",
                    bucket.name, blob_name,
                )
                return None

            # Download to a temp file then atomically rename so concurrent
            # readers never see a partial file.
            tmp_path = f"{local_path}.{os.getpid()}.tmp"
            blob.download_to_filename(tmp_path)
            if not _looks_like_netscape(tmp_path):
                logger.warning(
                    "Downloaded %s cookie file from gs://%s/%s is not "
                    "Netscape-format (missing header) — not caching; running "
                    "without authentication until a valid file is uploaded",
                    platform, bucket.name, blob_name,
                )
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return None
            os.replace(tmp_path, local_path)
            logger.info("Downloaded %s cookies from gs://%s/%s to %s",
                        platform, bucket.name, blob_name, local_path)
            return local_path
        except Exception as e:
            logger.warning("%s cookie fetch failed: %s — running without cookies",
                           platform, e)
            return None





def cookie_opts(platform: str) -> dict:
    """Return yt-dlp cookie options appropriate for the current environment.

    Local dev (macOS): extract cookies from the Chrome browser directly.
    Cloud Run / Docker: pull the platform's cookies.txt from GCS (cached in
    ``/tmp``). Falls back to ``YTDLP_COOKIE_FILE_{PLATFORM}`` then the legacy
    ``YTDLP_COOKIE_FILE`` env var, then to no cookies.

    Args:
        platform: platform key, e.g. ``"instagram"``.
    """
    # Cloud Run sets K_SERVICE; Docker containers won't have a browser.
    if os.environ.get("K_SERVICE") or not os.path.exists("/Applications"):
        path = ensure_cookie_file(platform) or _env_cookie_file(platform)
        if path:
            # Hand yt-dlp a private copy: it rewrites the cookiefile on context
            # exit, and concurrent scraper threads sharing one file race on that
            # write, truncating it mid-read (see _private_cookie_copy).
            return {"cookiefile": _private_cookie_copy(platform, path)}
        return {}

    return {"cookiesfrombrowser": ("chrome",)}





def requests_cookiejar(platform: str):
    """Return a ``MozillaCookieJar`` for ``requests`` calls, or ``None``.

    Used to attach the same session cookies to plain ``requests`` calls
    (e.g. TikTok's page-JSON and carousel-image fetches).

    Args:
        platform: platform key, e.g. ``"tiktok"``.
    """
    path = ensure_cookie_file(platform) or _env_cookie_file(platform)
    if not path or not os.path.exists(path):
        return None

    from http.cookiejar import MozillaCookieJar
    try:
        jar = MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        return jar
    except Exception as e:
        logger.debug("Failed to load %s cookies for requests: %s", platform, e)
        return None





def cookie_health(platform: str, session_cookie: str = "sessionid") -> dict:
    """Return health info for a platform's cookie file.

    Args:
        platform: platform key, e.g. ``"tiktok"``.
        session_cookie: name of the cookie row whose expiry marks the session's
            lifetime (TikTok and Instagram: ``"sessionid"``; YouTube:
            ``"__Secure-3PSID"``). When the row is absent or carries no expiry,
            status degrades to file-presence/age-based instead of unknown-only.

    Returns:
        Dict with keys ``present``, ``local_path``, ``file_age_days``,
        ``session_expires_at``, ``session_days_left``, ``status`` (one of
        'missing', 'expired', 'expiring_soon', 'stale', 'healthy', 'unknown')
        and a human-readable ``message``.

    Notes:
        * 'expiring_soon' fires when the session cookie has < 14 days left.
        * 'stale' fires when the file is older than 25 days, even if the
          session hasn't expired yet — auxiliary tokens drift and behavioural
          flags accumulate, so periodic refresh is healthy.
    """
    health = {
        "present": False,
        "local_path": None,
        "file_age_days": None,
        "session_expires_at": None,
        "session_days_left": None,
        "status": "unknown",
        "message": "",
    }

    path = ensure_cookie_file(platform) or _env_cookie_file(platform)

    # Local-dev path: cookies come from Chrome, not a file — declare healthy.
    if _is_local_dev():
        health["present"] = True
        health["status"] = "healthy"
        health["message"] = "Local dev: using cookies from Chrome browser"
        return health

    if not path or not os.path.exists(path):
        health["status"] = "missing"
        health["message"] = (
            f"Cookie file missing — upload a Netscape cookies.txt to "
            f"gs://<bucket>/{_gcs_blob_name(platform)} to enable authenticated scraping"
        )
        return health

    health["present"] = True
    health["local_path"] = path

    # File age — use GCS blob mtime if available (more accurate than local
    # cache mtime, which is just when we last downloaded), else local mtime.
    try:
        bucket = _cf()["data_io"].get("bucket")
        if bucket is not None:
            blob = bucket.blob(_gcs_blob_name(platform))
            blob.reload()
            if blob.updated:
                age_sec = time.time() - blob.updated.timestamp()
                health["file_age_days"] = age_sec / 86400
    except Exception:
        pass
    if health["file_age_days"] is None:
        try:
            health["file_age_days"] = (time.time() - os.path.getmtime(path)) / 86400
        except OSError:
            pass

    # Parse the session cookie's expiry from the Netscape file. Format:
    #   domain\tflag\tpath\tsecure\texpiry\tname\tvalue
    session_expiry = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == session_cookie:
                    try:
                        session_expiry = int(parts[4])
                    except ValueError:
                        continue
                    break
    except OSError as e:
        logger.warning("Could not read cookie file %s: %s", path, e)

    if session_expiry:
        health["session_expires_at"] = session_expiry
        health["session_days_left"] = (session_expiry - time.time()) / 86400

    age = health["file_age_days"]

    # Decide status.
    if session_expiry is None:
        # No usable expiry row — degrade to file-age-based status so platforms
        # whose session cookie carries no expiry still get a useful signal.
        if age is not None and age > 25:
            health["status"] = "stale"
            health["message"] = (
                f"cookie file is {age:.0f} days old and has no readable "
                f"'{session_cookie}' expiry — consider re-exporting"
            )
        elif age is not None:
            health["status"] = "healthy"
            health["message"] = (
                f"cookie file is {age:.0f} days old; no '{session_cookie}' "
                f"expiry row found (age-based status)"
            )
        else:
            health["status"] = "unknown"
            health["message"] = (
                f"Cookie file present but '{session_cookie}' row not found"
            )
    elif health["session_days_left"] <= 0:
        health["status"] = "expired"
        health["message"] = (
            f"{session_cookie} expired {-health['session_days_left']:.1f} days ago — "
            f"re-export cookies and re-upload"
        )
    elif health["session_days_left"] < 14:
        health["status"] = "expiring_soon"
        health["message"] = (
            f"{session_cookie} expires in {health['session_days_left']:.1f} days — "
            f"plan to re-export cookies before then"
        )
    elif age is not None and age > 25:
        health["status"] = "stale"
        health["message"] = (
            f"cookie file is {age:.0f} days old — "
            f"consider re-exporting to refresh auxiliary tokens"
        )
    else:
        days = health["session_days_left"]
        age_str = f"{age:.0f}d old" if age is not None else "age unknown"
        health["status"] = "healthy"
        health["message"] = f"{session_cookie} valid for {days:.0f} more days ({age_str})"

    return health
