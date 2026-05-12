#!/usr/bin/env python3
"""
TikTok downloader using yt-dlp as backend.

Drop-in alternative to mypyktok — returns the same single-row DataFrame
that generate_data_row() produces so downstream code is unchanged.
"""


import logging
import os
import threading
import time
from datetime import datetime
from glob import glob
from os import remove
from os.path import exists, join
from time import sleep

import pandas as pd
import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ExtractorError, GeoRestrictedError

from fyp.fyp_config import fyp_cf

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Error classification — maps yt-dlp exceptions to actionable categories
# -------------------------------------------------------------------------

_RETRYABLE = {"rate_limited", "network", "server_error", "unknown"}
_PERMANENT = {"removed", "private", "geo_blocked", "ip_blocked", "extraction"}


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify a yt-dlp error into (category, detail) for logging and retry decisions.

    Returns:
        (category, detail) where category is one of:
        - "ip_blocked"   — TikTok status 10204, HTTP 403 from TikTok
        - "rate_limited"  — HTTP 429
        - "private"       — TikTok status 10216/10222, login required
        - "removed"       — video deleted/unavailable, nonexistent ID
        - "geo_blocked"   — GeoRestrictedError
        - "network"       — timeout, connection refused, DNS failure, SSL
        - "server_error"  — HTTP 5xx from TikTok
        - "extraction"    — JS challenge, parsing failure, unexpected response
        - "unknown"       — unrecognised error
    """
    msg = str(exc)
    cause = getattr(exc, 'cause', None)

    # GeoRestrictedError is a subclass of ExtractorError
    if isinstance(exc, GeoRestrictedError):
        return "geo_blocked", msg

    # Check the underlying HTTP error for status codes
    if isinstance(cause, HTTPError):
        status = cause.status
        if status == 429:
            return "rate_limited", f"HTTP 429: {msg}"
        if status == 403:
            return "ip_blocked", f"HTTP 403: {msg}"
        if 500 <= status < 600:
            return "server_error", f"HTTP {status}: {msg}"

    # Check for network-level transport errors
    if isinstance(cause, TransportError):
        return "network", f"Transport error: {msg}"

    # String-based classification from TikTok extractor messages
    msg_lower = msg.lower()

    # yt-dlp often wraps the HTTPError inside DownloadError and flattens the
    # cause into the message ("...HTTP Error 403: Forbidden (caused by
    # <HTTPError 403: Forbidden>)"), so the isinstance(cause, HTTPError)
    # branch above doesn't fire. Catch via string match and classify as
    # rate_limited (retryable + triggers ThrottleController backoff).
    if 'http error 403' in msg_lower or 'httperror 403' in msg_lower:
        return "rate_limited", msg

    if 'http error 429' in msg_lower or 'httperror 429' in msg_lower:
        return "rate_limited", msg

    if 'ip address is blocked' in msg_lower or 'status code 10204' in msg_lower:
        return "ip_blocked", msg

    if 'not have permission' in msg_lower or 'log into' in msg_lower:
        return "private", msg

    if any(kw in msg_lower for kw in ('unavailable', 'removed', 'deleted', 'not found',
                                       'does not exist', 'status code 10')):
        return "removed", msg

    if any(kw in msg_lower for kw in ('challenge', 'js challenge', 'unable to extract',
                                       'unable to solve')):
        return "extraction", msg

    if any(kw in msg_lower for kw in ('timed out', 'timeout', 'connection', 'network',
                                       'ssl', 'certificate', 'dns', 'reset by peer')):
        return "network", msg

    if any(kw in msg_lower for kw in ('429', 'rate limit', 'too many requests')):
        return "rate_limited", msg

    return "unknown", msg


_THROTTLE_CATEGORIES = {"rate_limited"}


class ThrottleController:
    """Dynamic concurrency controller that reacts to TikTok rate signals.

    Workers call ``acquire()`` before each video and ``report_result()``
    after.  The controller adjusts the semaphore so that fewer workers
    run concurrently when rate-limit signals arrive, and gradually
    recovers when things are healthy.

    Args:
        initial:  Starting concurrency (default 8).
        minimum:  Floor — never go below this (default 2).
        maximum:  Ceiling — never exceed this (default 12).
        cooldown_successes: How many consecutive clean results before
            growing concurrency by 1 (default 10).
    """

    def __init__(
        self,
        initial: int = 8,
        minimum: int = 2,
        maximum: int = 12,
        cooldown_successes: int = 10,
        on_change: "callable | None" = None,
    ) -> None:
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(initial)
        self._current = initial
        self._minimum = minimum
        self._maximum = maximum
        self._cooldown_successes = cooldown_successes
        self._consecutive_ok = 0
        self._total_throttle_events = 0
        self._on_change = on_change

    # -- public API used by workers --

    def acquire(self) -> None:
        """Block until a concurrency slot is available."""
        self._sem.acquire()

    def release(self) -> None:
        """Return a concurrency slot (call in finally block)."""
        self._sem.release()

    def report_result(self, error_category: str | None) -> None:
        """Report the outcome of one video scrape.

        Args:
            error_category: The error category string from
                ``_classify_error()``, or ``None`` for success.
        """
        with self._lock:
            if error_category in _THROTTLE_CATEGORIES:
                self._consecutive_ok = 0
                self._total_throttle_events += 1
                self._shrink()
            else:
                self._consecutive_ok += 1
                if self._consecutive_ok >= self._cooldown_successes:
                    self._consecutive_ok = 0
                    self._grow()

    # -- read-only properties --

    @property
    def current(self) -> int:
        with self._lock:
            return self._current

    @property
    def total_throttle_events(self) -> int:
        with self._lock:
            return self._total_throttle_events

    # -- internal helpers (caller holds _lock) --

    def _shrink(self) -> None:
        target = max(self._minimum, self._current // 2)
        drop = self._current - target
        if drop <= 0:
            return
        logger.warning("Throttle: reducing concurrency %d → %d", self._current, target)
        for _ in range(drop):
            self._sem.acquire(blocking=False)  # drain permits
        self._current = target
        if self._on_change:
            self._on_change(self._current)

    def _grow(self) -> None:
        if self._current >= self._maximum:
            return
        self._current += 1
        self._sem.release()  # add one permit
        logger.info("Throttle: growing concurrency to %d", self._current)
        if self._on_change:
            self._on_change(self._current)



# -------------------------------------------------------------------------
# Cookie handling — adapts to local dev vs Cloud Run
# -------------------------------------------------------------------------

# Local cache path for the cookie file when running on Cloud Run.
# /tmp is writable in Cloud Run containers and survives within a single
# container instance (but not across container restarts — which is fine,
# we just re-pull on next call).
_COOKIE_LOCAL_PATH = "/tmp/tiktok_cookies.txt"

# How long to trust the locally-cached copy before re-pulling from GCS.
# Set to 6h so a freshly-uploaded cookies.txt is picked up by all running
# containers within ~6h without needing a redeploy.
_COOKIE_CACHE_TTL_SEC = 6 * 3600

# GCS object path for the cookie file (within the configured bucket).
_COOKIE_GCS_BLOB = "secrets/tiktok_cookies.txt"

# Guard concurrent download attempts so 8+ workers starting at once don't
# all race to download the same file.
_COOKIE_DOWNLOAD_LOCK = threading.Lock()


def _ensure_cookie_file() -> str | None:
    """Lazily fetch the TikTok cookies file from GCS to ``/tmp``.

    Only runs on Cloud Run (``K_SERVICE`` env var set). Returns the path
    to the local cookies file if available, ``None`` if no cookies could
    be obtained (in which case scraping continues without authentication).

    Caching: the local file is re-used for ``_COOKIE_CACHE_TTL_SEC`` before
    re-pulling. Concurrent calls are serialised by a module-level lock so
    only one download happens per refresh cycle.
    """
    if not os.environ.get('K_SERVICE'):
        return None

    # Fast path — local copy is fresh enough.
    if os.path.exists(_COOKIE_LOCAL_PATH):
        age = time.time() - os.path.getmtime(_COOKIE_LOCAL_PATH)
        if age < _COOKIE_CACHE_TTL_SEC:
            return _COOKIE_LOCAL_PATH

    with _COOKIE_DOWNLOAD_LOCK:
        # Re-check inside the lock — another thread may have just downloaded.
        if os.path.exists(_COOKIE_LOCAL_PATH):
            age = time.time() - os.path.getmtime(_COOKIE_LOCAL_PATH)
            if age < _COOKIE_CACHE_TTL_SEC:
                return _COOKIE_LOCAL_PATH

        try:
            bucket = fyp_cf['data_io'].get('bucket')
            if bucket is None:
                logger.warning("No GCS bucket configured; cannot fetch cookies")
                return None

            blob = bucket.blob(_COOKIE_GCS_BLOB)
            if not blob.exists():
                logger.warning(
                    "Cookie file not found at gs://%s/%s — running without "
                    "authentication. Upload a Netscape-format cookies.txt "
                    "to enable session-authenticated scraping.",
                    bucket.name, _COOKIE_GCS_BLOB,
                )
                return None

            # Download to a temp file then atomically rename so concurrent
            # readers never see a partial file.
            tmp_path = f"{_COOKIE_LOCAL_PATH}.{os.getpid()}.tmp"
            blob.download_to_filename(tmp_path)
            os.replace(tmp_path, _COOKIE_LOCAL_PATH)
            logger.info("Downloaded TikTok cookies from gs://%s/%s to %s",
                        bucket.name, _COOKIE_GCS_BLOB, _COOKIE_LOCAL_PATH)
            return _COOKIE_LOCAL_PATH
        except Exception as e:
            logger.warning("Cookie fetch failed: %s — running without cookies", e)
            return None


def _cookie_opts() -> dict:
    """Return yt-dlp cookie options appropriate for the current environment.

    Local dev (macOS): extract cookies from Chrome browser directly.
    Cloud Run / Docker: pull cookies.txt from GCS (cached in /tmp). Falls
    back to ``YTDLP_COOKIE_FILE`` env var if set, then to no cookies.
    """
    # Cloud Run sets K_SERVICE; Docker containers won't have a browser
    if os.environ.get('K_SERVICE') or not os.path.exists('/Applications'):
        path = _ensure_cookie_file()
        if path:
            return {'cookiefile': path}
        cookie_file = os.environ.get('YTDLP_COOKIE_FILE', '')
        if cookie_file and os.path.exists(cookie_file):
            return {'cookiefile': cookie_file}
        return {}

    return {'cookiesfrombrowser': ('chrome',)}


def _requests_cookies():
    """Return a ``MozillaCookieJar`` for requests, or ``None`` if unavailable.

    Used to attach the same TikTok session cookies to plain ``requests``
    calls in :func:`_fetch_item_struct` and :func:`_download_images`.
    """
    path = _ensure_cookie_file() or os.environ.get('YTDLP_COOKIE_FILE', '')
    if not path or not os.path.exists(path):
        return None

    from http.cookiejar import MozillaCookieJar
    try:
        jar = MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        return jar
    except Exception as e:
        logger.debug("Failed to load cookies for requests: %s", e)
        return None


# -------------------------------------------------------------------------
# Cookie health — parse sessionid expiry so callers can warn before a
# stale cookie file causes a queue-wide 403 spike.
# -------------------------------------------------------------------------

def cookie_health() -> dict:
    """Return health info for the TikTok cookies file.

    Returns a dict with keys:
        present (bool):           File found in GCS (or local override path).
        local_path (str | None):  Path to the locally-cached copy, if any.
        file_age_days (float | None): Age of the GCS blob (or local file).
        sessionid_expires_at (int | None): Unix timestamp from the cookie row.
        sessionid_days_left (float | None): Days until ``sessionid`` expires.
        status (str): One of 'missing', 'expired', 'expiring_soon', 'stale',
                      'healthy', 'unknown'.
        message (str): Human-readable summary.

    Notes:
        * 'expiring_soon' fires when sessionid has < 14 days left.
        * 'stale' fires when the file is older than 25 days, even if
          sessionid hasn't expired yet — msToken/ttwid drift and
          behavioural flags accumulate, so periodic refresh is healthy.
    """
    health = {
        'present': False,
        'local_path': None,
        'file_age_days': None,
        'sessionid_expires_at': None,
        'sessionid_days_left': None,
        'status': 'unknown',
        'message': '',
    }

    path = _ensure_cookie_file() or os.environ.get('YTDLP_COOKIE_FILE', '')

    # Local-dev path: cookies come from Chrome, not a file — declare healthy.
    if not os.environ.get('K_SERVICE') and os.path.exists('/Applications'):
        health['present'] = True
        health['status'] = 'healthy'
        health['message'] = 'Local dev: using cookies from Chrome browser'
        return health

    if not path or not os.path.exists(path):
        health['status'] = 'missing'
        health['message'] = (
            f"Cookie file missing — upload a Netscape cookies.txt to "
            f"gs://<bucket>/{_COOKIE_GCS_BLOB} to enable authenticated scraping"
        )
        return health

    health['present'] = True
    health['local_path'] = path

    # File age — use GCS blob mtime if available (more accurate than local
    # cache mtime, which is just when we last downloaded), else local mtime.
    try:
        bucket = fyp_cf['data_io'].get('bucket')
        if bucket is not None:
            blob = bucket.blob(_COOKIE_GCS_BLOB)
            blob.reload()
            if blob.updated:
                age_sec = time.time() - blob.updated.timestamp()
                health['file_age_days'] = age_sec / 86400
    except Exception:
        pass
    if health['file_age_days'] is None:
        try:
            health['file_age_days'] = (time.time() - os.path.getmtime(path)) / 86400
        except OSError:
            pass

    # Parse sessionid expiry from the Netscape file. Format:
    #   domain\tflag\tpath\tsecure\texpiry\tname\tvalue
    sessionid_expiry = None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.rstrip('\n').split('\t')
                if len(parts) >= 7 and parts[5] == 'sessionid':
                    try:
                        sessionid_expiry = int(parts[4])
                    except ValueError:
                        continue
                    break
    except OSError as e:
        logger.warning("Could not read cookie file %s: %s", path, e)

    if sessionid_expiry:
        health['sessionid_expires_at'] = sessionid_expiry
        health['sessionid_days_left'] = (sessionid_expiry - time.time()) / 86400

    # Decide status.
    if sessionid_expiry is None:
        health['status'] = 'unknown'
        health['message'] = 'Cookie file present but sessionid row not found'
    elif health['sessionid_days_left'] <= 0:
        health['status'] = 'expired'
        health['message'] = (
            f"sessionid expired {-health['sessionid_days_left']:.1f} days ago — "
            f"re-export cookies and re-upload"
        )
    elif health['sessionid_days_left'] < 14:
        health['status'] = 'expiring_soon'
        health['message'] = (
            f"sessionid expires in {health['sessionid_days_left']:.1f} days — "
            f"plan to re-export cookies before then"
        )
    elif health['file_age_days'] is not None and health['file_age_days'] > 25:
        health['status'] = 'stale'
        health['message'] = (
            f"cookie file is {health['file_age_days']:.0f} days old — "
            f"consider re-exporting to refresh msToken/ttwid"
        )
    else:
        days = health['sessionid_days_left']
        age = health['file_age_days']
        age_str = f"{age:.0f}d old" if age is not None else "age unknown"
        health['status'] = 'healthy'
        health['message'] = f"sessionid valid for {days:.0f} more days ({age_str})"

    return health





# -------------------------------------------------------------------------
# Field mapping helpers
# -------------------------------------------------------------------------

_DEFAULTS = {
    'desc': "",
    'createTime': "no default",
    'item_id': "",
    'video_duration': -1,
    'image_list': "",
    'author_id': "",
    'author_uniqueId': "",
    'author_nickname': "",
    'author_signature': "",
    'author_verified': False,
    'music_id': "",
    'music_title': "",
    'music_authorName': "",
    'music_album': "",
    'music_original': False,
    'music_duration': 0,
    'playlistId': "",
    'stats_diggCount': -1,
    'stats_commentCount': -1,
    'stats_playCount': -1,
    'stats_collectCount': -1,
    'stats_shareCount': -1,
    'anchors': "",
    'challenges': "",
    'poi_name': "",
    'poi_address': "",
    'poi_city': "",
    'poi_province': "",
    'poi_country': "",
    'IsAigc': False,
    'AIGCDescription': "",
    'aigcLabelType': "",
    'isAd': False,
    'video_downloaded': False,
    'last_modified': "no default",
}


def _info_to_row(info: dict) -> pd.DataFrame:
    """Convert yt-dlp info_dict to a single-row DataFrame matching the mypyktok schema."""

    try:
        create_time = datetime.fromtimestamp(int(info.get('timestamp', 0)))
    except (ValueError, TypeError, OSError):
        create_time = datetime(2000, 1, 1)

    artists_raw = info.get('artists') or []
    artist_str = info.get('artist', '') or (', '.join(artists_raw) if artists_raw else '')

    row = {
        'item_id': str(info.get('id', '')),
        'createTime': create_time,
        'desc': info.get('description', '') or '',
        'video_duration': info.get('duration') or -1,
        'image_list': "",
        'author_id': str(info.get('uploader_id', '') or ''),
        'author_uniqueId': str(info.get('uploader', '') or ''),
        'author_nickname': str(info.get('channel', '') or info.get('creator', '') or info.get('uploader', '') or ''),
        'author_signature': "",
        'author_verified': False,
        'music_id': str(info.get('track_id', '') or ''),
        'music_title': str(info.get('track', '') or ''),
        'music_authorName': artist_str,
        'music_album': str(info.get('album', '') or ''),
        'music_original': False,
        'music_duration': 0,
        'playlistId': "",
        'stats_diggCount': info.get('like_count') if info.get('like_count') is not None else -1,
        'stats_commentCount': info.get('comment_count') if info.get('comment_count') is not None else -1,
        'stats_playCount': info.get('view_count') if info.get('view_count') is not None else -1,
        'stats_collectCount': info.get('save_count') if info.get('save_count') is not None else -1,
        'stats_shareCount': info.get('repost_count') if info.get('repost_count') is not None else -1,
        'challenges': "",
        'anchors': "",
        'poi_name': "",
        'poi_address': "",
        'poi_city': "",
        'poi_province': "",
        'poi_country': "",
        'IsAigc': False,
        'AIGCDescription': "",
        'aigcLabelType': "",
        'isAd': False,
        'video_downloaded': False,
        'last_modified': datetime.now(),
    }

    # Build types dict (same logic as mypyktok.generate_data_row)
    pyk_data_types = {}
    for key, default in _DEFAULTS.items():
        if key not in ('createTime', 'last_modified'):
            pyk_data_types[key] = type(default)

    df = pd.DataFrame([row])
    df = df[list(_DEFAULTS.keys())]
    df = df.astype(pyk_data_types)

    return df





# -------------------------------------------------------------------------
# Page JSON extraction — fetches TikTok's embedded itemStruct to
# supplement yt-dlp metadata and detect image carousels.
# -------------------------------------------------------------------------

# Each entry: (script_tag_id, function that extracts itemStruct from parsed JSON)
_JSON_PATHS: list[tuple[str, str]] = [
    ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "rehydration"),
    ("__NEXT_DATA__", "next_data"),
    ("SIGI_STATE", "sigi_state"),
]


def _struct_rehydration(data: dict, video_id: str) -> dict:
    return data['__DEFAULT_SCOPE__']['webapp.video-detail']['itemInfo']['itemStruct']


def _struct_next_data(data: dict, video_id: str) -> dict:
    return data['props']['pageProps']['itemInfo']['itemStruct']


def _struct_sigi_state(data: dict, video_id: str) -> dict:
    return data['ItemModule'][video_id]


_STRUCT_EXTRACTORS = {
    "rehydration": _struct_rehydration,
    "next_data": _struct_next_data,
    "sigi_state": _struct_sigi_state,
}


def _fetch_item_struct(video_url: str) -> dict | None:
    """Fetch TikTok page and extract the full itemStruct dict.

    Tries multiple known JSON embedding paths that TikTok has used
    historically. Returns the itemStruct dict on success, None on failure.

    Uses a plain HTTP request — no browser_cookie3 dependency so it works
    on Cloud Run.
    """
    from json import loads

    from bs4 import BeautifulSoup
    from requests import get as requests_get

    video_id = video_url.rstrip('/').split('/')[-1]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }

    try:
        resp = requests_get(
            video_url,
            headers=headers,
            cookies=_requests_cookies(),
            timeout=20,
        )
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        logger.warning("Page fetch failed for %s: %s", video_url, e)
        return None

    for script_id, extractor_name in _JSON_PATHS:
        script = soup.find('script', attrs={'id': script_id})
        if script is None or not script.string:
            continue

        try:
            data = loads(script.string)
            item_struct = _STRUCT_EXTRACTORS[extractor_name](data, video_id)
            if item_struct:
                logger.info("Page JSON extracted via '%s' for %s", script_id, video_id)
                return item_struct
        except KeyError as e:
            logger.debug("Page JSON path '%s' missing key %s for %s",
                         script_id, e, video_id)
        except Exception as e:
            logger.debug("Page JSON path '%s' failed for %s: %s",
                         script_id, video_id, e)

    logger.warning("All page JSON extraction paths failed for %s", video_url)
    return None


def _get_image_urls_from_struct(item_struct: dict) -> list[str]:
    """Extract carousel image URLs from an itemStruct dict."""
    image_post = item_struct.get('imagePost', {})
    images = image_post.get('images', [])
    return [img['imageURL']['urlList'][0]
            for img in images
            if img.get('imageURL', {}).get('urlList')]


def _supplement_from_struct(data_row: pd.DataFrame, item_struct: dict) -> None:
    """Fill in metadata fields that yt-dlp doesn't extract, using the page JSON."""
    music = item_struct.get('music', {})
    author = item_struct.get('author', {})
    challenges = item_struct.get('challenges', [])

    # Music fields
    if music.get('id'):
        data_row.loc[0, 'music_id'] = str(music['id'])
    if music.get('duration'):
        data_row.loc[0, 'music_duration'] = int(music['duration'])
    if 'original' in music:
        data_row.loc[0, 'music_original'] = bool(music['original'])

    # Author fields
    if author.get('signature'):
        data_row.loc[0, 'author_signature'] = str(author['signature'])
    if 'verified' in author:
        data_row.loc[0, 'author_verified'] = bool(author['verified'])

    # Challenges (hashtag names, pipe-separated)
    if challenges:
        challenge_titles = [c.get('title', '') for c in challenges if c.get('title')]
        if challenge_titles:
            data_row.loc[0, 'challenges'] = " | ".join(challenge_titles)

    # AIGC fields
    if 'IsAigc' in item_struct:
        data_row.loc[0, 'IsAigc'] = bool(item_struct['IsAigc'])
    if item_struct.get('AIGCDescription'):
        data_row.loc[0, 'AIGCDescription'] = str(item_struct['AIGCDescription'])
    if item_struct.get('aigcLabelType') is not None:
        data_row.loc[0, 'aigcLabelType'] = str(item_struct['aigcLabelType'])





def _download_images(
    image_urls: list[str],
    video_id: str,
    save_path: str,
    stream_to_bucket=None,
    verbose: bool = False,
) -> bool:
    """Download carousel images. Returns True on success."""
    from requests import get as requests_get

    _CHUNK = 8 * 1024 * 1024

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://www.tiktok.com/',
    }

    cookies = _requests_cookies()
    try:
        for k, one_image in enumerate(image_urls):
            image_fn = f"{video_id}_{k + 1:02}.jpeg"
            if stream_to_bucket is None:
                resp = requests_get(one_image, allow_redirects=True, headers=headers,
                                    cookies=cookies, timeout=60)
                with open(join(save_path, image_fn), 'wb') as f:
                    f.write(resp.content)
            else:
                resp = requests_get(one_image, headers=headers, cookies=cookies,
                                    stream=True, timeout=60)
                blob = stream_to_bucket.blob(f"{save_path}/{image_fn}")
                with blob.open('wb') as gcs_file:
                    for chunk in resp.iter_content(chunk_size=_CHUNK):
                        if chunk:
                            gcs_file.write(chunk)
        return True
    except Exception as e:
        if verbose:
            print(f"WARNING (yt-dlp): Failed to download images for '{video_id}': {e}")
        sleep(3)
        return False





# -------------------------------------------------------------------------
# Main entry point — matches mypyktok.save_tiktok() interface
# -------------------------------------------------------------------------

def _empty_fail(error_type: str = "unknown", error_detail: str = "") -> pd.DataFrame:
    """Return an empty DataFrame tagged with error classification metadata."""
    df = pd.DataFrame()
    df.attrs['error_type'] = error_type
    df.attrs['error_detail'] = error_detail
    return df


def _cleanup_temp_files(temp_dir: str, video_id: str) -> None:
    """Remove any partial download files for a video from the temp directory."""
    for f in glob(join(temp_dir, f"{video_id}.*")):
        try:
            remove(f)
        except OSError:
            pass


_META_MAX_RETRIES = 3
_DL_MAX_RETRIES = 2


def save_tiktok(
    video_url: str,
    save_video: bool = True,
    max_duration_to_save: int = 9000,
    save_path: str = "",
    stream_to_bucket=None,
    verbose: bool = False,
) -> pd.DataFrame:
    """Download a TikTok video's metadata and media using yt-dlp.

    Returns a single-row DataFrame matching the mypyktok schema,
    or an empty DataFrame on failure. Failed DataFrames carry
    ``attrs['error_type']`` and ``attrs['error_detail']`` for
    downstream retry/queue decisions.
    """

    video_id = video_url.rstrip('/').split('/')[-1]
    temp_dir = fyp_cf['paths']['temp']

    # -------------------------------------------
    # Step 1: extract metadata (no download yet)
    # -------------------------------------------
    ydl_opts: dict = {
        'quiet': True,
        'no_warnings': not verbose,
        **_cookie_opts(),
        'skip_download': True,
        'no_color': True,
        'extractor_retries': 3,
        'socket_timeout': 30,
    }

    info = None
    last_category, last_detail = "unknown", ""

    for attempt in range(_META_MAX_RETRIES):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
            break
        except (yt_dlp.utils.DownloadError, ExtractorError) as e:
            last_category, last_detail = _classify_error(e)
            logger.warning("Scrape %s metadata attempt %d/%d failed: [%s] %s",
                           video_id, attempt + 1, _META_MAX_RETRIES,
                           last_category, last_detail)
            if last_category in _RETRYABLE and attempt < _META_MAX_RETRIES - 1:
                backoff = 3 * (2 ** attempt)
                logger.info("Retrying %s in %ds...", video_id, backoff)
                sleep(backoff)
                continue
            return _empty_fail(last_category, last_detail)
        except Exception as e:
            last_category, last_detail = "unknown", str(e)
            logger.error("Scrape %s metadata unexpected error: %s", video_id, e)
            return _empty_fail(last_category, last_detail)

    if info is None:
        logger.warning("Scrape %s: no info returned", video_id)
        return _empty_fail("extraction", "No info returned by yt-dlp")

    data_row = _info_to_row(info)

    # -------------------------------------------
    # Step 2: supplement metadata from page JSON
    # -------------------------------------------
    item_struct = _fetch_item_struct(video_url)
    if item_struct:
        _supplement_from_struct(data_row, item_struct)

    # Detect image carousel
    is_slideshow = False
    image_urls: list[str] = []

    formats = info.get('formats') or []
    has_video_format = any(f.get('vcodec', 'none') != 'none' for f in formats)

    if not has_video_format or (info.get('duration') or 0) == 0:
        if item_struct:
            image_urls = _get_image_urls_from_struct(item_struct)
        if image_urls:
            is_slideshow = True
            data_row.loc[0, 'image_list'] = " | ".join(image_urls)
        elif not has_video_format:
            logger.warning("Suspected image post (no video formats) but carousel "
                           "extraction returned no images: %s", video_url)

    # -------------------------------------------
    # Step 3: download media
    # -------------------------------------------
    if not save_video:
        return data_row

    duration = data_row.loc[0, 'video_duration']
    if isinstance(duration, (int, float)) and duration > max_duration_to_save:
        logger.info("Video '%s' duration (%ss) exceeds %ss. Skipping download.",
                     video_id, duration, max_duration_to_save)
        return data_row

    if is_slideshow and image_urls:
        ok = _download_images(
            image_urls=image_urls,
            video_id=video_id,
            save_path=save_path if stream_to_bucket is None else fyp_cf['data_io']['gcs_media_prefix'],
            stream_to_bucket=stream_to_bucket,
            verbose=verbose,
        )
        data_row.loc[0, 'video_downloaded'] = ok

    else:
        # Download video via yt-dlp to temp, then upload to GCS
        out_template = join(temp_dir, f"{video_id}.%(ext)s")
        dl_opts: dict = {
            'quiet': True,
            'no_warnings': not verbose,
            **_cookie_opts(),
            'outtmpl': out_template,
            'no_color': True,
            'overwrites': True,
            'format': 'best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'retries': 3,
            'socket_timeout': 30,
        }

        for attempt in range(_DL_MAX_RETRIES):
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    ydl.download([video_url])

                # Find the downloaded file
                downloaded = join(temp_dir, f"{video_id}.mp4")
                if not exists(downloaded):
                    candidates = glob(join(temp_dir, f"{video_id}.*"))
                    mp4_candidates = [c for c in candidates if c.endswith('.mp4')]
                    downloaded = mp4_candidates[0] if mp4_candidates else (candidates[0] if candidates else None)

                if downloaded and exists(downloaded):
                    video_fn = f"{video_id}.mp4"

                    if stream_to_bucket is not None:
                        blob = stream_to_bucket.blob(f"{save_path}/{video_fn}")
                        blob.upload_from_filename(downloaded)
                        data_row.loc[0, 'video_downloaded'] = True
                    else:
                        target = join(save_path, video_fn)
                        if downloaded != target:
                            # Atomic rename when src and dst are on the same filesystem.
                            # Avoids partial-file reads if another thread/process touches dst.
                            os.replace(downloaded, target)
                        data_row.loc[0, 'video_downloaded'] = True

                    # Clean up temp file
                    if exists(downloaded):
                        try:
                            remove(downloaded)
                        except OSError:
                            pass
                else:
                    logger.warning("Download succeeded but file not found for '%s'", video_id)

                break

            except (yt_dlp.utils.DownloadError, ExtractorError) as e:
                category, detail = _classify_error(e)
                logger.warning("Scrape %s download attempt %d/%d failed: [%s] %s",
                               video_id, attempt + 1, _DL_MAX_RETRIES,
                               category, detail)
                _cleanup_temp_files(temp_dir, video_id)
                if category in _RETRYABLE and attempt < _DL_MAX_RETRIES - 1:
                    backoff = 3 * (3 ** attempt)
                    logger.info("Retrying download %s in %ds...", video_id, backoff)
                    sleep(backoff)
                    continue
                break

            except Exception as e:
                logger.error("Scrape %s download unexpected error: %s", video_id, e)
                _cleanup_temp_files(temp_dir, video_id)
                break

    return data_row
