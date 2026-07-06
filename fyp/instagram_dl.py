#!/usr/bin/env python3
"""
Instagram scraper using yt-dlp as backend.

Fetches metadata + media for Instagram posts/reels identified by their URL
shortcode (the ``item_id`` produced by :class:`fyp.ingest.InstagramDDPCollection`).
Requires a logged-in research account's cookies — anonymous Instagram access
fails for most content (see :mod:`fyp.scraper_cookies`).

Image-only posts (liked photos/carousels) are classified as
``permanent:no_video`` in this first version: yt-dlp cannot extract them, and
Instagram has no unauthenticated page-JSON equivalent of TikTok's carousel
extraction. Their donated enrichment-seed metadata still surfaces downstream.
"""


import logging
import os
import re
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

from fyp import scraper_cookies
from fyp.fyp_config import fyp_cf
from fyp.platform_scraper import BaseScraper

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Error classification
# -------------------------------------------------------------------------

# Instagram's catch-all "Requested content is not available, rate-limit reached
# or login required" conflates removed, throttled, and logged-out — it is kept
# retryable (rate_limited) so genuinely throttled items stay queued; permanently
# removed items behind it will keep retrying (accepted for now).
_RETRYABLE = {"rate_limited", "login_required", "network", "server_error", "unknown"}
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

    msg_lower = msg.lower()

    if 'there is no video in this post' in msg_lower:
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
    df = pd.DataFrame()
    df.attrs['error_type'] = error_type
    df.attrs['error_detail'] = error_detail
    return df




def _cleanup_temp_files(temp_dir: str, item_id: str) -> None:
    """Remove any partial download files for an item from the temp directory."""
    for f in glob(join(temp_dir, f"{item_id}.*")):
        try:
            remove(f)
        except OSError:
            pass




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
    temp_dir = fyp_cf['paths']['temp']
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




# Raw column names → canonical base names. Contract-named ig_* columns pass
# through unchanged.
_RAW_TO_CANONICAL: dict[str, str] = {
    "create_time_raw": "create_time",
    "duration_raw": "duration",
    "play_count_raw": "play_count",
    "author_name_raw": "author_name",
    "last_modified": "scrape_ts",
}




class InstagramScraper(BaseScraper):
    """Instagram platform scraper (yt-dlp, authenticated via research-account cookies).

    Handles video posts and reels; image-only posts fail permanently with
    ``no_video`` (see module docstring). All threads share one authenticated
    session, so concurrency is capped hard — Instagram is the most ban-happy
    of the supported platforms.
    """

    platform = "instagram"
    # /p/ serves reel and tv shortcodes too (Instagram redirects).
    url_template = "https://www.instagram.com/p/{item_id}/"
    slideshow_image_column = None


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
            return fail
        if info is None:
            return _empty_fail("extraction", "No info returned by yt-dlp")

        data_row = _info_to_row(info, item_id)

        # yt-dlp's Instagram extractor usually returns no view count (and sometimes
        # no like/comment counts) — supplement the -1 sentinels from the page JSON.
        if (data_row.loc[0, 'play_count_raw'] == -1
                or data_row.loc[0, 'ig_like_count'] == -1
                or data_row.loc[0, 'ig_comment_count'] == -1):
            counts = _fetch_page_counts(url, item_id)
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


    def map_to_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        return raw.rename(columns=_RAW_TO_CANONICAL)


    def prepare_raw_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        """Raw fix-ups: the -1 unknown-duration sentinel becomes NA."""
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
