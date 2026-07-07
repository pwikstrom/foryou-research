#!/usr/bin/env python3
"""
TikTok downloader using yt-dlp as backend.

Drop-in alternative to mypyktok — returns the same single-row DataFrame
that generate_data_row() produces so downstream code is unchanged.
"""


import logging
import os
from datetime import datetime
from glob import glob
from os import remove
from os.path import exists, join
from time import sleep

import pandas as pd
import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ExtractorError, GeoRestrictedError

from fyp import scraper_cookies
from fyp.fyp_config import fyp_cf
from fyp.platform_scraper import (  # noqa: F401
    _THROTTLE_CATEGORIES,
    SLIDESHOW_SECONDS_PER_IMAGE,
    BaseScraper,
    ThrottleController,
)

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Error classification — maps yt-dlp exceptions to actionable categories
# -------------------------------------------------------------------------

# "carousel" is produced directly by save_tiktok (photo post detected but its
# images could not be extracted or downloaded), not by _classify_error. It is
# retryable — the page-JSON fetch behind image extraction fails transiently —
# and deliberately not a throttle signal.
_RETRYABLE = {"rate_limited", "network", "server_error", "unknown", "carousel"}
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
    # Normalize typographic apostrophes so ASCII keyword matching works.
    msg_lower = msg.lower().replace('\u2019', "'")

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


# ThrottleController now lives in fyp.platform_scraper (platform-agnostic);
# re-exported via the top-of-file import for back-compat with existing imports.



# -------------------------------------------------------------------------
# Cookie handling — generic per-platform plumbing lives in fyp.scraper_cookies;
# these thin wrappers keep the module-internal call sites unchanged.
# -------------------------------------------------------------------------

def _cookie_opts() -> dict:
    """Return yt-dlp cookie options for TikTok (see :mod:`fyp.scraper_cookies`)."""
    return scraper_cookies.cookie_opts("tiktok")


def _requests_cookies():
    """Return a ``MozillaCookieJar`` with the TikTok session cookies, or ``None``.

    Used to attach the same TikTok session cookies to plain ``requests``
    calls in :func:`_fetch_item_struct` and :func:`_download_images`.
    """
    return scraper_cookies.requests_cookiejar("tiktok")


def cookie_health() -> dict:
    """Return health info for the TikTok cookies file (sessionid expiry)."""
    return scraper_cookies.cookie_health("tiktok", session_cookie="sessionid")





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
            stream=True,
        )
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > _PAGE_BYTE_CAP:
                logger.warning(
                    "Page for %s exceeds %d bytes; skipping page-JSON supplement "
                    "to bound memory.", video_id, _PAGE_BYTE_CAP)
                resp.close()
                return None
        resp.close()
        soup = BeautifulSoup(b"".join(chunks), 'html.parser')
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
    written: list[str] = []
    try:
        for k, one_image in enumerate(image_urls):
            image_fn = f"{video_id}_{k + 1:02}.jpeg"
            if stream_to_bucket is None:
                resp = requests_get(one_image, allow_redirects=True, headers=headers,
                                    cookies=cookies, timeout=60)
                with open(join(save_path, image_fn), 'wb') as f:
                    f.write(resp.content)
                written.append(join(save_path, image_fn))
            else:
                resp = requests_get(one_image, headers=headers, cookies=cookies,
                                    stream=True, timeout=60)
                blob = stream_to_bucket.blob(f"{save_path}/{image_fn}")
                with blob.open('wb') as gcs_file:
                    for chunk in resp.iter_content(chunk_size=_CHUNK):
                        if chunk:
                            gcs_file.write(chunk)
                written.append(blob.name)
        return True
    except Exception as e:
        if verbose:
            print(f"WARNING (yt-dlp): Failed to download images for '{video_id}': {e}")
        # Remove the partial image set: the orchestrator's slideshow assembly
        # walks consecutive NN suffixes, so a stale partial set from this
        # attempt could be concatenated into a later retry's slideshow.
        for name in written:
            try:
                if stream_to_bucket is None:
                    remove(name)
                else:
                    stream_to_bucket.blob(name).delete()
            except Exception:
                pass
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

# Hard ceiling on a single media download. On Cloud Run /tmp is a memory-backed
# tmpfs, so a runaway download (livestream, a non-video format with no duration
# to trip the duration cap) writing multi-GB there pushes the container into an
# OOM kill. No legitimate short-form TikTok video approaches this; yt-dlp aborts
# the download when the format size exceeds it. Overridable via
# ``[misc] max_media_download_bytes``.
try:
    _MAX_MEDIA_BYTES = int(fyp_cf["misc"].get("max_media_download_bytes", 1 << 30))
except (KeyError, TypeError, ValueError):
    _MAX_MEDIA_BYTES = 1 << 30

# Hard cap on the raw page read in _fetch_item_struct. A normal TikTok page is
# 1-3 MB; the optional page-JSON supplement loads the whole body into memory and
# parses it with BeautifulSoup (a multi-x memory multiplier). A pathological
# page (huge embedded JSON, or a body with no Content-Length) read unbounded
# across concurrent workers is a prime OOM source, so cap the read and skip the
# supplement past the cap — yt-dlp's extract_info still supplies core metadata.
_PAGE_BYTE_CAP = 8 * 1024 * 1024


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

    Photo posts: with ``save_video`` a detected photo post whose images cannot
    be extracted or downloaded fails with the retryable ``"carousel"`` category
    instead of storing media. Metadata-only calls (``save_video=False``) still
    return the metadata row for such posts.
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
            save_path=save_path,
            stream_to_bucket=stream_to_bucket,
            verbose=verbose,
        )
        if not ok:
            return _empty_fail(
                "carousel",
                f"failed downloading {len(image_urls)} carousel images",
            )
        data_row.loc[0, 'video_downloaded'] = True

    elif not has_video_format:
        # Photo post detected but page-JSON extraction produced no image URLs.
        # Never fall through to the video branch: yt-dlp would download the
        # audio-only slideshow format and store it as {id}.mp4.
        return _empty_fail(
            "carousel",
            "photo post detected but page-JSON image extraction returned no images",
        )

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
            'max_filesize': _MAX_MEDIA_BYTES,
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
                    # Metadata row is still saved; the orchestrator uses these
                    # attrs to keep transient media failures queued for retry
                    # (see BaseScraper.fetch contract).
                    data_row.attrs['media_error_type'] = 'unknown'
                    data_row.attrs['media_error_detail'] = 'download finished but no output file found'

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
                data_row.attrs['media_error_type'] = category
                data_row.attrs['media_error_detail'] = detail
                break

            except Exception as e:
                logger.error("Scrape %s download unexpected error: %s", video_id, e)
                _cleanup_temp_files(temp_dir, video_id)
                data_row.attrs['media_error_type'] = 'unknown'
                data_row.attrs['media_error_detail'] = str(e)
                break

    return data_row





def _download_slideshow_audio(
    video_url: str,
    video_id: str,
    temp_dir: str,
    verbose: bool = False,
) -> str | None:
    """Download a photo post's audio track (music/voiceover) to a temp file.

    Photo posts expose exactly one audio-only format in yt-dlp (the slideshow
    audio), so ``bestaudio/best`` selects it. The ``_audio`` suffix keeps the
    file out of the ``{video_id}.*`` cleanup glob and away from the
    orchestrator's temp ``{video_id}.mp4`` / ``{video_id}_NN.jpeg`` names.

    Returns:
        Path to the downloaded audio file, or ``None`` on any failure — the
        caller then builds a silent slideshow.
    """
    out_template = join(temp_dir, f"{video_id}_audio.%(ext)s")
    dl_opts: dict = {
        'quiet': True,
        'no_warnings': not verbose,
        **_cookie_opts(),
        'outtmpl': out_template,
        'format': 'bestaudio/best',
        'no_color': True,
        'overwrites': True,
        'retries': 2,
        'socket_timeout': 30,
    }
    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([video_url])
        candidates = glob(join(temp_dir, f"{video_id}_audio.*"))
        if candidates:
            return candidates[0]
        logger.warning("Slideshow audio download for '%s' produced no file", video_id)
        return None
    except Exception as e:
        logger.warning("Slideshow audio download failed for '%s': %s", video_id, e)
        for f in glob(join(temp_dir, f"{video_id}_audio.*")):
            try:
                remove(f)
            except OSError:
                pass
        return None




# -------------------------------------------------------------------------
# Count overflow repair (TikTok-specific) + the platform scraper subclass
# -------------------------------------------------------------------------

# TikTok reports view/engagement counts as signed 32-bit integers; counts above
# 2**31 - 1 (~2.15 billion) arrive wrapped around to a negative value. The true
# count is recovered by adding 2**32. The yt-dlp "missing" sentinel -1 is left
# untouched (a genuine 4,294,967,295-count item would also wrap to -1, but that is
# vanishingly rare and indistinguishable from the sentinel).
_UINT32_RANGE: int = 1 << 32

# Repaired on the CANONICAL frame: play_count is the renamed view count; the four
# stats_* counts stay platform-specific and feed the per-K engagement rates.
OVERFLOW_REPAIR_COLUMNS: tuple[str, ...] = (
    "play_count",
    "stats_diggCount",
    "stats_shareCount",
    "stats_commentCount",
    "stats_collectCount",
)


# Raw yt-dlp / page-JSON column names → canonical base names. Platform-specific
# fields (music_*, stats_diggCount, challenges, ...) keep their raw names.
_RAW_TO_CANONICAL: dict[str, str] = {
    "createTime": "create_time",
    "video_duration": "duration",
    "stats_playCount": "play_count",
    "author_nickname": "author_name",
    "last_modified": "scrape_ts",
}




def repair_overflowed_counts(
    df: pd.DataFrame,
    columns: tuple[str, ...] = OVERFLOW_REPAIR_COLUMNS,
    verbose: bool = False,
) -> pd.DataFrame:
    """Recover signed-32-bit-overflowed TikTok counts in place.

    Any value strictly below -1 in a count column is treated as a 32-bit wrap of
    a count exceeding 2**31 and is corrected by adding 2**32. The -1 missing
    sentinel and all non-negative values are preserved.

    Args:
        df: DataFrame of scrape stats (mutated and returned).
        columns: Count column names to repair (canonical names).
        verbose: When True, print the number of values repaired per column.

    Returns:
        The same DataFrame with overflowed counts recovered.
    """
    for col in columns:
        if col not in df.columns:
            continue
        series = df[col]
        mask = (series < -1).fillna(False)
        n_repaired = int(mask.sum())
        if n_repaired:
            df[col] = series.mask(mask, series + _UINT32_RANGE)
            if verbose:
                print(f"    Recovered {n_repaired:,} signed-32-bit-overflowed value(s) in {col}")
    return df




class TikTokScraper(BaseScraper):
    """TikTok platform scraper (yt-dlp primary, legacy pyktok fallback).

    Wraps the module's existing download/extraction helpers behind the
    :class:`~fyp.platform_scraper.BaseScraper` contract: :meth:`fetch` selects the
    backend and returns the raw single-row frame; :meth:`map_to_canonical`
    renames it to the canonical schema; :meth:`classify_error` and
    :meth:`repair_counts` cover TikTok's error categories and 32-bit count wrap.
    """

    platform = "tiktok"
    url_template = "https://www.tiktok.com/@/video/{item_id}/"
    # Raw " | "-joined image URLs for photo posts; emitted by both the ytdlp
    # and legacy pyktok backends. Drives the base image_count() default.
    slideshow_image_column = "image_list"


    def item_url(self, item_id: str) -> str:
        return self.url_template.format(item_id=item_id)


    def fetch(
        self,
        item_id: str,
        *,
        save_media: bool,
        save_path: str,
        stream_to_bucket=None,
        verbose: bool = False,
    ) -> pd.DataFrame:
        backend = fyp_cf['misc'].get('scraper_backend', 'pyktok')
        max_duration = self.media_duration_cap()
        url = self.item_url(item_id)
        if backend == 'ytdlp':
            return save_tiktok(
                url,
                save_video=save_media,
                max_duration_to_save=max_duration,
                save_path=save_path,
                stream_to_bucket=stream_to_bucket,
                verbose=verbose,
            )
        import fyp.mypyktok as pyk
        pyk.specify_browser('chrome')
        return pyk.save_tiktok(
            url,
            save_video=save_media,
            max_duration_to_save=max_duration,
            browser_name='chrome',
            save_path=save_path,
            stream_to_bucket=stream_to_bucket,
            verbose=verbose,
        )


    def map_to_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        return raw.rename(columns=_RAW_TO_CANONICAL)


    def prepare_raw_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        """Raw fix-ups: image_list URL string → count, slideshow duration override.

        Image posts get ``video_duration = image_count * SLIDESHOW_SECONDS_PER_IMAGE``
        (the orchestrator assembles slideshows at that rate); zero/negative
        durations become NA.
        """
        if 'image_list' in df.columns:
            df['image_list'] = df['image_list'].map(
                lambda x: len(x.split("|")) if isinstance(x, str) and x else 0
            ).astype("int64[pyarrow]")
            mask = (df['image_list'] > 0).fillna(False)
            if 'video_duration' in df.columns:
                df.loc[mask, 'video_duration'] = (
                    df.loc[mask, 'image_list'] * SLIDESHOW_SECONDS_PER_IMAGE
                )
        if 'video_duration' in df.columns:
            df.loc[(df['video_duration'] < 1).fillna(False), 'video_duration'] = pd.NA
        return df


    def fetch_slideshow_audio(self, item_id: str, temp_dir: str) -> str | None:
        return _download_slideshow_audio(
            self.item_url(item_id), item_id, temp_dir, verbose=self.verbose
        )


    def classify_error(self, error_type: str | None) -> str:
        if error_type is None:
            return "ok"
        bucket = "permanent" if error_type in _PERMANENT else "transient"
        return f"{bucket}:{error_type}"


    def repair_counts(self, df: pd.DataFrame) -> pd.DataFrame:
        return repair_overflowed_counts(df, verbose=self.verbose)


    def throttle_limits(self, max_workers: int) -> tuple[int, int, int]:
        # All threads share a single TikTok session behind the same cookies, so
        # the ceiling stays modest even when the caller asks for more workers.
        # Capped at 4 concurrent: a small subset of items transiently allocate
        # multiple GiB during the metadata/page phase, and fewer in-flight items
        # keeps the aggregate working set well under the container memory limit.
        return (min(max_workers, 4), 2, 4)


    def health_check(self) -> dict | None:
        return cookie_health()
