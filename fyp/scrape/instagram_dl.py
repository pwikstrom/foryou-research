#!/usr/bin/env python3
"""
Instagram scraper using yt-dlp as backend.

Fetches metadata + media for Instagram posts/reels identified by their URL
shortcode (the ``item_id`` produced by :class:`fyp.ingest.InstagramDDPCollection`).
Requires a logged-in research account's cookies — anonymous Instagram access
fails for most content (see :mod:`fyp.scraper_cookies`).

Image-only posts (single photos and carousels): yt-dlp cannot extract them
("No video formats found!"), so the fetch falls back to the authenticated
media-info endpoint for metadata + image URLs, downloads the source images as
``{item_id}_{NN:02}.jpeg``, and the orchestrator assembles a silent
``{item_id}.mp4`` slideshow — the same division of labor as TikTok's photo
posts (see :class:`fyp.scrape.platform_scraper.BaseScraper`). Phase-2
candidates: muxing the post's music (slideshows are silent for now), a
page-JSON image-URL fallback when the media-info endpoint is blocked, and
video segments inside mixed carousels (currently skipped).
"""


import logging
import os
import random
import re
import threading
from datetime import datetime
from glob import glob
from json import loads as json_loads
from os import remove
from os.path import exists, join
from time import sleep

import pandas as pd
import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ExtractorError, GeoRestrictedError

from fyp.scrape import scraper_cookies
from fyp.scrape.platform_scraper import (
    SLIDESHOW_SECONDS_PER_IMAGE,
    BaseScraper,
    cleanup_temp_files,
    empty_fail,
)

logger = logging.getLogger(__name__)




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf


# -------------------------------------------------------------------------
# Error classification
# -------------------------------------------------------------------------

# Instagram's catch-all "Requested content is not available, rate-limit reached
# or login required" conflates removed, throttled, and logged-out — it is kept
# retryable (rate_limited) so genuinely throttled items stay queued; permanently
# removed items behind it will keep retrying (accepted for now).
# "carousel" is produced only by the image-post path (a partial image-download
# failure), never parsed from an exception, so it cannot reach the
# _extract_metadata/_download_media retry loops.
_RETRYABLE = {"rate_limited", "login_required", "network", "server_error", "carousel", "unknown"}
_PERMANENT = {"removed", "private", "no_video", "geo_blocked"}

_META_MAX_RETRIES = 3
_DL_MAX_RETRIES = 2


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify a yt-dlp error into (category, detail) for retry decisions.

    Returns:
        (category, detail) where category is one of:
        - "rate_limited"   — HTTP 429/403, empty media response, IG's ambiguous
                             "rate-limit reached or login required" catch-all
        - "login_required" — bare login wall (usually dead cookies, account-wide)
        - "no_video"       — image-only post (no video to extract)
        - "private"        — private account/post
        - "removed"        — post deleted or id nonexistent
        - "geo_blocked"    — GeoRestrictedError
        - "network"        — timeout, connection refused, DNS failure, SSL
        - "server_error"   — HTTP 5xx
        - "unknown"        — unrecognised (kept retryable)
    """
    msg = str(exc)
    cause = getattr(exc, 'cause', None)

    if isinstance(exc, GeoRestrictedError):
        return "geo_blocked", msg

    if isinstance(cause, HTTPError):
        status = cause.status
        if status in (403, 429):
            return "rate_limited", f"HTTP {status}: {msg}"
        if 500 <= status < 600:
            return "server_error", f"HTTP {status}: {msg}"

    if isinstance(cause, TransportError):
        return "network", f"Transport error: {msg}"

    # Normalize typographic apostrophes so ASCII keyword matching works.
    msg_lower = msg.lower().replace('\u2019', "'")

    # Image-only / carousel-image posts: yt-dlp parses the post but finds no
    # video stream. Both phrasings mean the same thing — no video to fetch. This
    # is permanent (retrying can't conjure a video); the donated seed still
    # carries the caption/author. Without this, image posts churned 3× per run
    # as 'unknown' and never left the queue.
    if ('there is no video in this post' in msg_lower
            or 'no video formats found' in msg_lower):
        return "no_video", msg

    # The ambiguous catch-all must be checked before the "removed" patterns —
    # it contains "not available" but usually means throttled/logged out.
    if 'rate-limit reached' in msg_lower or 'rate limit' in msg_lower:
        return "rate_limited", msg

    if ('http error 403' in msg_lower or 'http error 429' in msg_lower
            or 'too many requests' in msg_lower
            or 'empty media response' in msg_lower):
        return "rate_limited", msg

    if 'private' in msg_lower:
        return "private", msg

    if 'login required' in msg_lower or 'log in' in msg_lower or 'logged-in' in msg_lower:
        return "login_required", msg

    if any(kw in msg_lower for kw in ('unavailable', 'removed', 'deleted', 'not found',
                                       'does not exist', 'page not found')):
        return "removed", msg

    if any(kw in msg_lower for kw in ('timed out', 'timeout', 'connection', 'network',
                                       'ssl', 'certificate', 'dns', 'reset by peer')):
        return "network", msg

    return "unknown", msg




def _empty_fail(error_type: str = "unknown", error_detail: str = "") -> pd.DataFrame:
    """Return an empty DataFrame tagged with error classification metadata."""
    return empty_fail(error_type, error_detail)


def _cleanup_temp_files(temp_dir: str, item_id: str) -> None:
    """Remove any partial download files for an item from the temp directory."""
    cleanup_temp_files(temp_dir, item_id)




def _info_to_row(info: dict, item_id: str) -> pd.DataFrame:
    """Convert a yt-dlp info dict to the raw single-row Instagram frame.

    ``item_id`` is stamped from the *requested* shortcode, never from
    ``info['id']`` — yt-dlp returns Instagram's numeric media pk there, which
    would break the queue/enrichment join.
    """
    try:
        create_time = datetime.fromtimestamp(int(info.get('timestamp', 0)))
    except (ValueError, TypeError, OSError):
        create_time = datetime(2000, 1, 1)

    row = {
        'item_id': str(item_id),
        'desc': info.get('description', '') or '',
        'create_time_raw': create_time,
        'duration_raw': info.get('duration') or -1,
        'author_id': str(info.get('channel_id', '') or info.get('uploader_id', '') or ''),
        'ig_author_handle': str(info.get('uploader_id', '') or info.get('channel', '') or ''),
        'author_name_raw': str(info.get('uploader', '') or info.get('channel', '') or ''),
        'play_count_raw': info.get('view_count') if info.get('view_count') is not None else -1,
        'ig_like_count': info.get('like_count') if info.get('like_count') is not None else -1,
        'ig_comment_count': info.get('comment_count') if info.get('comment_count') is not None else -1,
        'video_downloaded': False,
        'last_modified': datetime.now(),
    }
    return pd.DataFrame([row])




def _extract_metadata(url: str, item_id: str, verbose: bool = False):
    """yt-dlp metadata extraction with retry. Returns (info, None) or (None, fail_df)."""
    ydl_opts: dict = {
        'quiet': True,
        'no_warnings': not verbose,
        **scraper_cookies.cookie_opts("instagram"),
        'skip_download': True,
        'no_color': True,
        'extractor_retries': 3,
        'socket_timeout': 30,
    }

    for attempt in range(_META_MAX_RETRIES):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False), None
        except (yt_dlp.utils.DownloadError, ExtractorError) as e:
            category, detail = _classify_error(e)
            logger.warning("Scrape %s metadata attempt %d/%d failed: [%s] %s",
                           item_id, attempt + 1, _META_MAX_RETRIES, category, detail)
            if category in _RETRYABLE and attempt < _META_MAX_RETRIES - 1:
                backoff = 3 * (2 ** attempt)
                logger.info("Retrying %s in %ds...", item_id, backoff)
                sleep(backoff)
                continue
            return None, _empty_fail(category, detail)
        except Exception as e:
            logger.error("Scrape %s metadata unexpected error: %s", item_id, e)
            return None, _empty_fail("unknown", str(e))

    return None, _empty_fail("extraction", "No info returned by yt-dlp")




def _download_media(
    url: str,
    item_id: str,
    save_path: str,
    stream_to_bucket=None,
    verbose: bool = False,
) -> tuple[bool, str | None, str]:
    """Download the post's video to temp and move/upload it.

    Returns:
        ``(ok, error_category, error_detail)`` — category/detail are ``None``/""
        on success, otherwise the :func:`_classify_error` result of the last
        failure so the caller can distinguish transient from permanent.
    """
    temp_dir = _cf()['paths']['temp']
    out_template = join(temp_dir, f"{item_id}.%(ext)s")
    dl_opts: dict = {
        'quiet': True,
        'no_warnings': not verbose,
        **scraper_cookies.cookie_opts("instagram"),
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
                ydl.download([url])

            downloaded = join(temp_dir, f"{item_id}.mp4")
            if not exists(downloaded):
                candidates = glob(join(temp_dir, f"{item_id}.*"))
                mp4_candidates = [c for c in candidates if c.endswith('.mp4')]
                downloaded = mp4_candidates[0] if mp4_candidates else (candidates[0] if candidates else None)

            if not downloaded or not exists(downloaded):
                logger.warning("Download succeeded but file not found for '%s'", item_id)
                return False, "unknown", "download finished but no output file found"

            video_fn = f"{item_id}.mp4"
            if stream_to_bucket is not None:
                blob = stream_to_bucket.blob(f"{save_path}/{video_fn}")
                blob.upload_from_filename(downloaded)
                try:
                    remove(downloaded)
                except OSError:
                    pass
            else:
                target = join(save_path, video_fn)
                if downloaded != target:
                    # Atomic rename when src and dst share a filesystem —
                    # avoids partial-file reads by concurrent consumers.
                    os.replace(downloaded, target)
            return True, None, ""

        except (yt_dlp.utils.DownloadError, ExtractorError) as e:
            category, detail = _classify_error(e)
            logger.warning("Scrape %s download attempt %d/%d failed: [%s] %s",
                           item_id, attempt + 1, _DL_MAX_RETRIES, category, detail)
            _cleanup_temp_files(temp_dir, item_id)
            if category in _RETRYABLE and attempt < _DL_MAX_RETRIES - 1:
                backoff = 3 * (3 ** attempt)
                logger.info("Retrying download %s in %ds...", item_id, backoff)
                sleep(backoff)
                continue
            return False, category, detail

        except Exception as e:
            logger.error("Scrape %s download unexpected error: %s", item_id, e)
            _cleanup_temp_files(temp_dir, item_id)
            return False, "unknown", str(e)

    return False, "unknown", "download retries exhausted"




# -------------------------------------------------------------------------
# Page-JSON count supplementation (the TikTok _fetch_item_struct analogue)
# -------------------------------------------------------------------------

_PAGE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# Keys Instagram has used for the reel/video view counter, in preference order.
_PLAY_COUNT_KEYS = ('play_count', 'ig_play_count', 'video_view_count', 'view_count')


def _walk_for_media_node(node, item_id: str) -> dict | None:
    """Depth-first search of a parsed JSON blob for the media node of ``item_id``.

    Instagram's relay payloads nest the media info at varying depths; the stable
    anchor is a dict whose ``code`` equals the post shortcode and that carries at
    least one view-count key.
    """
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get('code') == item_id and any(k in cur for k in _PLAY_COUNT_KEYS):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None




def _fetch_page_counts(url: str, item_id: str) -> dict | None:
    """Fetch the post page and extract engagement counts yt-dlp doesn't return.

    yt-dlp's Instagram extractor usually returns no ``view_count`` (and
    occasionally no like/comment counts); the numbers are present in the
    page-embedded relay JSON for the logged-in session. Never raises —
    supplementation must not fail a scrape. Returns
    ``{"play_count": int|None, "like_count": int|None, "comment_count": int|None}``
    or None when nothing could be extracted.
    """
    from requests import get as requests_get

    try:
        resp = requests_get(
            url,
            headers=_PAGE_HEADERS,
            cookies=scraper_cookies.requests_cookiejar("instagram"),
            timeout=20,
        )
        html_text = resp.text
    except Exception as e:
        logger.warning("Page fetch for counts failed for %s: %s", item_id, e)
        return None

    return _parse_page_counts(html_text, item_id)




def _parse_page_counts(html_text: str, item_id: str) -> dict | None:
    """Extract engagement counts from a post page's HTML (pure, no network).

    Primary path: parse every embedded JSON blob mentioning the shortcode and
    walk it for the media node. Fallback: regex over the raw HTML.
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        for script in soup.find_all('script', attrs={'type': 'application/json'}):
            blob = script.string
            if not blob or item_id not in blob:
                continue
            try:
                data = json_loads(blob)
            except Exception:
                continue
            node = _walk_for_media_node(data, item_id)
            if node is None:
                continue
            play = next((node[k] for k in _PLAY_COUNT_KEYS if node.get(k) is not None), None)
            counts = {
                'play_count': int(play) if play is not None else None,
                'like_count': int(node['like_count']) if node.get('like_count') is not None else None,
                'comment_count': int(node['comment_count']) if node.get('comment_count') is not None else None,
            }
            if any(v is not None for v in counts.values()):
                logger.info("Page JSON counts extracted for %s: %s", item_id, counts)
                return counts
    except Exception as e:
        logger.debug("Page JSON count walk failed for %s: %s", item_id, e)

    # Fallback: raw regex over the HTML (first play-count occurrence).
    try:
        m = re.search(r'"(?:ig_play_count|play_count|video_view_count)"\s*:\s*(\d+)', html_text)
        if m:
            logger.info("Page regex play_count extracted for %s: %s", item_id, m.group(1))
            return {'play_count': int(m.group(1)), 'like_count': None, 'comment_count': None}
    except Exception:
        pass

    logger.info("No page counts found for %s", item_id)
    return None




# -------------------------------------------------------------------------
# Authenticated media-info count supplementation (api/v1/media/{pk}/info/)
# -------------------------------------------------------------------------
#
# yt-dlp's Instagram extractor returns no view/play count for reels, and the
# post page HTML embeds none either (_fetch_page_counts is a no-op for reels).
# Instagram's authenticated web API does return them: an item's numeric media pk
# (a base64 decode of the shortcode) fetched from
# https://www.instagram.com/api/v1/media/{pk}/info/ with the session cookies and
# the web X-IG-App-ID carries play_count / ig_play_count / like_count /
# comment_count. This is the same private endpoint instaloader/instagrapi wrap,
# hit directly with the cookie jar we already manage — no extra dependency.
#
# It is the authenticated private API, so it runs under a throttle-hard posture:
# a randomized inter-call delay plus a process-wide circuit breaker that pauses
# supplementation for the rest of a batch after repeated rate-limit/challenge
# responses (so a flagged account is not hammered).

# Instagram's public web app id (the value the web client sends).
_IG_WEB_APP_ID = "936619743392459"

_MEDIA_INFO_URL = "https://www.instagram.com/api/v1/media/{pk}/info/"

# base64 alphabet Instagram uses to encode the numeric media pk as a shortcode.
_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

# Media-info play-count keys in preference order: play_count is the "views"
# Instagram now shows, ig_play_count is the reels-plays variant, view_count is
# the legacy field.
_MEDIA_INFO_PLAY_KEYS = ("play_count", "ig_play_count", "view_count")

# After this many consecutive rate-limit/challenge responses, stop calling the
# endpoint for the rest of the batch (throttle-hard).
_MEDIA_INFO_MAX_CONSECUTIVE_BLOCKS = 3

_MEDIA_INFO_LOCK = threading.Lock()
_MEDIA_INFO_CONSECUTIVE_BLOCKS = 0
_MEDIA_INFO_CIRCUIT_OPEN = False


def _shortcode_to_mediaid(shortcode: str) -> int | None:
    """Decode an Instagram shortcode to its numeric media pk (base64).

    Returns None when the shortcode carries a character outside Instagram's
    alphabet, so the caller skips supplementation rather than raising.
    """
    mediaid = 0
    for ch in shortcode:
        pos = _SHORTCODE_ALPHABET.find(ch)
        if pos < 0:
            return None
        mediaid = mediaid * 64 + pos
    return mediaid




def _csrftoken_from_jar(jar) -> str | None:
    """Return the '.instagram.com' csrftoken value from a cookie jar.

    Picks the domain-specific row explicitly: the jar can carry more than one
    'csrftoken' (different domains), which would raise CookieConflictError on a
    plain name lookup.
    """
    values = {c.domain: c.value for c in jar if c.name == "csrftoken"}
    return values.get(".instagram.com") or next(iter(values.values()), None)




def _media_info_circuit_open() -> bool:
    """Whether the circuit breaker has paused count supplementation."""
    with _MEDIA_INFO_LOCK:
        return _MEDIA_INFO_CIRCUIT_OPEN




def _note_media_info_block(item_id: str, status) -> None:
    """Record a rate-limit/challenge response; open the breaker past the limit."""
    global _MEDIA_INFO_CONSECUTIVE_BLOCKS, _MEDIA_INFO_CIRCUIT_OPEN
    with _MEDIA_INFO_LOCK:
        _MEDIA_INFO_CONSECUTIVE_BLOCKS += 1
        count = _MEDIA_INFO_CONSECUTIVE_BLOCKS
        if count >= _MEDIA_INFO_MAX_CONSECUTIVE_BLOCKS and not _MEDIA_INFO_CIRCUIT_OPEN:
            _MEDIA_INFO_CIRCUIT_OPEN = True
            logger.warning(
                "IG media-info: %d consecutive blocks (last HTTP %s on %s) — pausing "
                "count supplementation for the rest of this batch.",
                count, status, item_id)
        else:
            logger.warning("IG media-info blocked (HTTP %s) for %s [%d/%d]",
                           status, item_id, count, _MEDIA_INFO_MAX_CONSECUTIVE_BLOCKS)




def _reset_media_info_blocks() -> None:
    """Clear the consecutive-block counter after a clean response."""
    global _MEDIA_INFO_CONSECUTIVE_BLOCKS
    if _MEDIA_INFO_CONSECUTIVE_BLOCKS:
        with _MEDIA_INFO_LOCK:
            _MEDIA_INFO_CONSECUTIVE_BLOCKS = 0




def _parse_media_info_counts(payload: dict, item_id: str) -> dict | None:
    """Extract counts from a media-info JSON payload (pure, no network)."""
    items = payload.get("items") or []
    if not items:
        return None
    media = items[0]
    play = next((media[k] for k in _MEDIA_INFO_PLAY_KEYS if media.get(k) is not None), None)
    counts = {
        "play_count": int(play) if play is not None else None,
        "like_count": int(media["like_count"]) if media.get("like_count") is not None else None,
        "comment_count": int(media["comment_count"]) if media.get("comment_count") is not None else None,
    }
    if any(v is not None for v in counts.values()):
        logger.info("IG media-info counts for %s: %s", item_id, counts)
        return counts
    return None




def _fetch_media_info_payload(item_id: str) -> tuple[dict | None, str | None]:
    """Fetch the full media-info JSON payload for a post. Never raises.

    Returns ``(payload, None)`` on success, or ``(None, error_category)``
    where the category maps into the module error taxonomy (``rate_limited``,
    ``login_required``, ``network``, ``removed``, ``no_video``). Honors the
    randomized inter-call delay and the module circuit breaker — but NOT the
    ``ig_fetch_view_counts`` config gate, which only governs optional count
    supplementation (the image-post path needs this payload regardless).
    """
    if _media_info_circuit_open():
        return None, "rate_limited"

    pk = _shortcode_to_mediaid(item_id)
    if pk is None:
        return None, "no_video"

    jar = scraper_cookies.requests_cookiejar("instagram")
    if jar is None:
        return None, "login_required"

    from requests import get as requests_get

    headers = {
        "User-Agent": _PAGE_HEADERS["User-Agent"],
        "X-IG-App-ID": _IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": _csrftoken_from_jar(jar) or "",
        "Accept": "*/*",
        "Referer": f"https://www.instagram.com/p/{item_id}/",
    }

    try:
        # Randomized delay before each authenticated call (throttle-hard).
        sleep(random.uniform(1.5, 4.0))
        resp = requests_get(_MEDIA_INFO_URL.format(pk=pk), headers=headers,
                            cookies=jar, timeout=20)
    except Exception as e:
        logger.debug("IG media-info fetch failed for %s: %s", item_id, e)
        return None, "network"

    if resp.status_code in (401, 403, 429):
        _note_media_info_block(item_id, resp.status_code)
        return None, "rate_limited"
    if resp.status_code in (400, 404):
        logger.debug("IG media-info %s → HTTP %s", item_id, resp.status_code)
        return None, "removed"
    if resp.status_code != 200:
        logger.debug("IG media-info %s → HTTP %s", item_id, resp.status_code)
        return None, "no_video"

    _reset_media_info_blocks()
    try:
        payload = resp.json()
    except Exception as e:
        logger.debug("IG media-info JSON parse failed for %s: %s", item_id, e)
        return None, "no_video"
    if not (payload.get("items") or []):
        return None, "no_video"
    return payload, None




def _fetch_media_info_counts(item_id: str) -> dict | None:
    """Fetch play/like/comment counts from the authenticated media-info endpoint.

    Returns ``{"play_count": int|None, "like_count": int|None,
    "comment_count": int|None}`` or None. Never raises — supplementation must
    not fail a scrape. Honors the config gate ``[misc] ig_fetch_view_counts``
    plus (via :func:`_fetch_media_info_payload`) the randomized inter-call
    delay and the module circuit breaker.
    """
    if not _cf()["misc"].get("ig_fetch_view_counts", True):
        return None
    payload, _ = _fetch_media_info_payload(item_id)
    if payload is None:
        return None
    return _parse_media_info_counts(payload, item_id)




# -------------------------------------------------------------------------
# Image-only posts (photos and carousels → orchestrator-assembled slideshows)
# -------------------------------------------------------------------------

# media_type values in the media-info payload.
_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_VIDEO = 2
_MEDIA_TYPE_CAROUSEL = 8


def _row_from_media_info(payload: dict, item_id: str) -> pd.DataFrame:
    """Build the raw single-row Instagram frame from a media-info payload.

    The image-post analogue of :func:`_info_to_row` — yt-dlp yields no info
    dict for image posts, so caption/author/timestamp/counts all come from
    ``payload['items'][0]``. Emits the same column set so
    :meth:`InstagramScraper.map_to_canonical` and the orchestrator's
    success predicate treat both paths identically. ``item_id`` is stamped
    from the requested shortcode, never from the payload's numeric pk.
    """
    media = (payload.get('items') or [{}])[0]
    user = media.get('user') or {}
    caption = media.get('caption') or {}
    counts = _parse_media_info_counts(payload, item_id) or {}

    try:
        create_time = datetime.fromtimestamp(int(media.get('taken_at', 0)))
    except (ValueError, TypeError, OSError):
        create_time = datetime(2000, 1, 1)

    row = {
        'item_id': str(item_id),
        'desc': caption.get('text', '') or '',
        'create_time_raw': create_time,
        'duration_raw': -1,  # prepare_raw_batch overrides to image_count × 2s
        'author_id': str(user.get('pk', '') or ''),
        'ig_author_handle': str(user.get('username', '') or ''),
        'author_name_raw': str(user.get('full_name', '') or user.get('username', '') or ''),
        'play_count_raw': counts.get('play_count') if counts.get('play_count') is not None else -1,
        'ig_like_count': counts.get('like_count') if counts.get('like_count') is not None else -1,
        'ig_comment_count': counts.get('comment_count') if counts.get('comment_count') is not None else -1,
        'video_downloaded': False,
        'last_modified': datetime.now(),
    }
    return pd.DataFrame([row])




def _extract_image_urls(payload: dict) -> list[str]:
    """Extract source-image URLs from a media-info payload (pure, no network).

    Carousels (``media_type == 8``) yield one URL per image segment, in post
    order; single images yield one URL. ``candidates[0]`` is the largest
    rendition. Video segments inside mixed carousels are skipped (phase 2),
    and a pure video payload yields ``[]``.
    """
    media = (payload.get('items') or [{}])[0]
    if media.get('media_type') == _MEDIA_TYPE_CAROUSEL:
        sources = media.get('carousel_media') or []
    elif media.get('media_type') == _MEDIA_TYPE_VIDEO:
        return []
    else:
        sources = [media]

    urls = []
    for source in sources:
        if source.get('media_type') == _MEDIA_TYPE_VIDEO:
            continue
        candidates = (source.get('image_versions2') or {}).get('candidates') or []
        if candidates and candidates[0].get('url'):
            urls.append(candidates[0]['url'])
    return urls




def _download_images(
    image_urls: list[str],
    item_id: str,
    save_path: str,
    stream_to_bucket=None,
    verbose: bool = False,
) -> bool:
    """Download an image post's source images. Returns True on success.

    Writes ``{item_id}_{NN:02}.jpeg`` (1-based, consecutive — the
    orchestrator's slideshow NN-walk contract) into ``save_path`` locally or
    streamed to the bucket.
    """
    from requests import get as requests_get

    _CHUNK = 8 * 1024 * 1024

    headers = {
        'User-Agent': _PAGE_HEADERS['User-Agent'],
        'Referer': 'https://www.instagram.com/',
    }

    cookies = scraper_cookies.requests_cookiejar("instagram")
    written: list[str] = []
    try:
        for k, one_image in enumerate(image_urls):
            if k:
                # Gentle pacing between CDN requests (ban-happy platform).
                sleep(random.uniform(0.5, 1.5))
            image_fn = f"{item_id}_{k + 1:02}.jpeg"
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
            logger.warning("Failed to download images for '%s': %s", item_id, e)
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




# Raw column names → canonical base names. The raw ig_* counts/handle translate
# to the generic base fields here (the raw names stay in _info_to_row and the
# -1 supplementation step, which run pre-canonicalization).
_RAW_TO_CANONICAL: dict[str, str] = {
    "create_time_raw": "create_time",
    "duration_raw": "duration",
    "play_count_raw": "play_count",
    "author_name_raw": "author_name",
    "last_modified": "scrape_ts",
    "ig_like_count": "fave_count",
    "ig_comment_count": "comment_count",
    "ig_author_handle": "author_handle",
}




class InstagramScraper(BaseScraper):
    """Instagram platform scraper (yt-dlp, authenticated via research-account cookies).

    Handles video posts and reels via yt-dlp; image-only posts (photos and
    carousels) via the authenticated media-info endpoint, downloading their
    source images for the orchestrator's silent-slideshow assembly (see module
    docstring). All threads share one authenticated session, so concurrency is
    capped hard — Instagram is the most ban-happy of the supported platforms.
    """

    platform = "instagram"
    # /p/ serves reel and tv shortcodes too (Instagram redirects).
    url_template = "https://www.instagram.com/p/{item_id}/"
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
        url = self.item_url(item_id)

        info, fail = _extract_metadata(url, item_id, verbose=verbose)
        if fail is not None:
            # yt-dlp found no video stream → image-only post (photo/carousel).
            # Fall back to the media-info endpoint for metadata + images.
            if fail.attrs.get('error_type') == 'no_video':
                return self._fetch_image_post(
                    item_id, save_media=save_media, save_path=save_path,
                    stream_to_bucket=stream_to_bucket, verbose=verbose,
                    fallback_detail=fail.attrs.get('error_detail', ''))
            return fail
        if info is None:
            return _empty_fail("extraction", "No info returned by yt-dlp")

        data_row = _info_to_row(info, item_id)

        # yt-dlp's Instagram extractor returns no view count for reels (and
        # sometimes no like/comment counts). Supplement the -1 sentinels from the
        # authenticated media-info endpoint first (it carries play_count), falling
        # back to the page-JSON walk when that yields nothing.
        if (data_row.loc[0, 'play_count_raw'] == -1
                or data_row.loc[0, 'ig_like_count'] == -1
                or data_row.loc[0, 'ig_comment_count'] == -1):
            counts = _fetch_media_info_counts(item_id) or _fetch_page_counts(url, item_id)
            if counts:
                if counts.get('play_count') is not None and data_row.loc[0, 'play_count_raw'] == -1:
                    data_row.loc[0, 'play_count_raw'] = counts['play_count']
                if counts.get('like_count') is not None and data_row.loc[0, 'ig_like_count'] == -1:
                    data_row.loc[0, 'ig_like_count'] = counts['like_count']
                if counts.get('comment_count') is not None and data_row.loc[0, 'ig_comment_count'] == -1:
                    data_row.loc[0, 'ig_comment_count'] = counts['comment_count']

        if not save_media:
            return data_row

        duration = data_row.loc[0, 'duration_raw']
        if not self.should_download_media(duration):
            logger.info("Item '%s' duration (%ss) exceeds %ss cap. Skipping download.",
                        item_id, duration, self.media_duration_cap())
            return data_row

        ok, media_category, media_detail = _download_media(
            url, item_id, save_path,
            stream_to_bucket=stream_to_bucket, verbose=verbose)
        if ok:
            data_row.loc[0, 'video_downloaded'] = True
        else:
            # Metadata row is still saved; the orchestrator uses these attrs
            # to keep transient media failures queued for retry (see
            # BaseScraper.fetch contract).
            data_row.attrs['media_error_type'] = media_category
            data_row.attrs['media_error_detail'] = media_detail
        return data_row


    def _fetch_image_post(
        self,
        item_id: str,
        *,
        save_media: bool,
        save_path: str,
        stream_to_bucket=None,
        verbose: bool = False,
        fallback_detail: str = "",
    ) -> pd.DataFrame:
        """Fetch an image-only post (photo/carousel) via the media-info endpoint.

        Builds the metadata row from the payload, stamps the raw ``image_list``
        column (`` | ``-joined source URLs, read by the base ``image_count``
        hook), and downloads the images for the orchestrator's slideshow
        assembly. ``no_video`` stays the terminal category when the endpoint
        yields no images; a partial image download fails transient
        ``carousel`` so the whole post is retried.
        """
        payload, err = _fetch_media_info_payload(item_id)
        if payload is None:
            if err in ("rate_limited", "login_required", "network"):
                return _empty_fail(err, f"image post: media-info unavailable ({fallback_detail})")
            return _empty_fail(err or "no_video",
                               f"image post but media-info yielded nothing: {fallback_detail}")

        image_urls = _extract_image_urls(payload)
        if not image_urls:
            return _empty_fail("no_video",
                               f"image post but no image candidates: {fallback_detail}")

        data_row = _row_from_media_info(payload, item_id)
        data_row.loc[0, 'image_list'] = " | ".join(image_urls)

        if not save_media:
            return data_row

        # No media_duration_cap check: slideshow duration is image_count × 2s
        # (≤ ~20s for the largest carousels), always far under any cap.
        ok = _download_images(image_urls, item_id, save_path,
                              stream_to_bucket=stream_to_bucket, verbose=verbose)
        if not ok:
            return _empty_fail("carousel",
                               f"failed downloading {len(image_urls)} carousel images")
        data_row.loc[0, 'video_downloaded'] = True
        return data_row


    def map_to_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        return raw.rename(columns=_RAW_TO_CANONICAL)


    def prepare_raw_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        """Raw fix-ups: image_list URLs → count (+ slideshow duration), -1 → NA.

        The raw ``image_list`` column holds `` | ``-joined source URLs at fetch
        time (what ``image_count`` reads); it is stored as the integer image
        count, and image posts get ``duration = count × 2s`` — matching the
        slideshow the orchestrator assembles.
        """
        if 'image_list' in df.columns:
            df['image_list'] = df['image_list'].map(
                lambda x: len(x.split("|")) if isinstance(x, str) and x else 0
            ).astype("int64[pyarrow]")
            mask = (df['image_list'] > 0).fillna(False)
            if 'duration_raw' in df.columns:
                df.loc[mask, 'duration_raw'] = (
                    df.loc[mask, 'image_list'] * SLIDESHOW_SECONDS_PER_IMAGE)
        if 'duration_raw' in df.columns:
            df.loc[(df['duration_raw'] < 1).fillna(False), 'duration_raw'] = pd.NA
        return df


    def classify_error(self, error_type: str | None) -> str:
        if error_type is None:
            return "ok"
        bucket = "permanent" if error_type in _PERMANENT else "transient"
        return f"{bucket}:{error_type}"


    def repair_counts(self, df: pd.DataFrame) -> pd.DataFrame:
        return df


    def throttle_limits(self, max_workers: int) -> tuple[int, int, int]:
        # One authenticated session shared by all threads; Instagram bans
        # aggressively, so the ceiling stays very low regardless of workers.
        return (min(max_workers, 2), 1, 3)


    def health_check(self) -> dict | None:
        return scraper_cookies.cookie_health("instagram", session_cookie="sessionid")


    def media_probe_url(self, item_id: str) -> dict | None:
        ydl_opts: dict = {
            'quiet': True,
            'no_warnings': True,
            **scraper_cookies.cookie_opts("instagram"),
            'skip_download': True,
            'no_color': True,
            'socket_timeout': 30,
            'format': 'best[ext=mp4]/best',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(self.item_url(item_id), download=False)
            return self._probe_target(ydl, info)
