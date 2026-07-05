#!/usr/bin/env python3
"""
YouTube scraper using yt-dlp as backend.

Fetches metadata + media for YouTube videos identified by their 11-character
video id (the ``item_id`` produced by :class:`fyp.ingest.YouTubeDDPCollection`).

Datacenter IPs (Cloud Run) frequently hit YouTube's bot wall ("Sign in to
confirm you're not a bot"); research-account cookies partially mitigate it
(see :mod:`fyp.scraper_cookies`) and the distinct ``bot_check`` category is a
throttle signal so concurrency backs off. If bot-checks persist, a PO-token
provider (bgutil-ytdlp-pot-provider) is the known follow-up mitigation.

Most watch-history items are long-form and exceed the media duration cap —
they are deliberately scraped metadata-only; Shorts and clips get media.
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
from fyp.platform_scraper import BaseScraper

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Error classification
# -------------------------------------------------------------------------

# "bot_check" is transient AND a throttle signal (_THROTTLE_CATEGORIES in
# platform_scraper): the batch backs off instead of burning the whole queue
# against the bot wall. HTTP 403 is typically YouTube throttling (unlike
# TikTok, where it means an IP block) — kept retryable.
_RETRYABLE = {"bot_check", "rate_limited", "network", "server_error", "unknown"}
_PERMANENT = {"removed", "private", "age_restricted", "members_only", "geo_blocked"}

_META_MAX_RETRIES = 3
_DL_MAX_RETRIES = 2

# YouTube serves >360p only as separate DASH video+audio streams; the merge
# (ffmpeg is in the deploy image) caps at 720p to keep storage sane.
_FORMAT = ('bv*[height<=720][ext=mp4]+ba[ext=m4a]'
           '/b[height<=720][ext=mp4]/b[ext=mp4]/b')

# YouTube's n-challenge solver (yt-dlp-ejs) needs a JavaScript runtime. deno
# is yt-dlp's default-enabled runtime; node must be enabled explicitly and is
# used when deno is absent (e.g. local dev). An unavailable runtime is simply
# not used, so enabling both is safe everywhere.
_JS_RUNTIMES = {'deno': {'path': None}, 'node': {'path': None}}


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify a yt-dlp error into (category, detail) for retry decisions.

    Returns:
        (category, detail) where category is one of:
        - "bot_check"      — "Sign in to confirm you're not a bot" wall
        - "rate_limited"   — HTTP 429/403, too many requests
        - "removed"        — video deleted/unavailable, account terminated
        - "private"        — private video
        - "age_restricted" — age gate (cookies already applied → permanent)
        - "members_only"   — channel-membership gate
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

    if "confirm you're not a bot" in msg_lower or 'not a robot' in msg_lower:
        return "bot_check", msg

    if 'private video' in msg_lower or 'video is private' in msg_lower:
        return "private", msg

    if 'confirm your age' in msg_lower or 'age-restricted' in msg_lower or 'age restricted' in msg_lower:
        return "age_restricted", msg

    if 'members-only' in msg_lower or 'members only' in msg_lower or 'join this channel' in msg_lower:
        return "members_only", msg

    # Geo restrictions sometimes surface as a flattened message instead of a
    # GeoRestrictedError instance.
    if 'in your country' in msg_lower or 'geo restriction' in msg_lower:
        return "geo_blocked", msg

    if any(kw in msg_lower for kw in ('video unavailable', 'has been removed',
                                       'no longer available', 'account associated',
                                       'terminated', 'does not exist', 'not available')):
        return "removed", msg

    if ('http error 403' in msg_lower or 'http error 429' in msg_lower
            or 'too many requests' in msg_lower or 'rate limit' in msg_lower):
        return "rate_limited", msg

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




def _parse_create_time(info: dict) -> datetime:
    """Upload time from ``timestamp``, falling back to ``upload_date`` (YYYYMMDD)."""
    ts = info.get('timestamp')
    if ts:
        try:
            return datetime.fromtimestamp(int(ts))
        except (ValueError, TypeError, OSError):
            pass
    upload_date = info.get('upload_date')
    if upload_date:
        try:
            return datetime.strptime(str(upload_date), '%Y%m%d')
        except ValueError:
            pass
    return datetime(2000, 1, 1)




def _info_to_row(info: dict, item_id: str) -> pd.DataFrame:
    """Convert a yt-dlp info dict to the raw single-row YouTube frame.

    ``item_id`` is stamped from the *requested* video id so the
    queue/enrichment join can never drift from what was asked for.
    """
    categories = info.get('categories') or []

    row = {
        'item_id': str(item_id),
        'desc': info.get('description', '') or '',
        'create_time_raw': _parse_create_time(info),
        'duration_raw': info.get('duration') or -1,
        'author_id': str(info.get('channel_id', '') or ''),
        'yt_author_handle': str(info.get('uploader_id', '') or ''),
        'author_name_raw': str(info.get('channel', '') or info.get('uploader', '') or ''),
        'play_count_raw': info.get('view_count') if info.get('view_count') is not None else -1,
        'yt_like_count': info.get('like_count') if info.get('like_count') is not None else -1,
        'yt_comment_count': info.get('comment_count') if info.get('comment_count') is not None else -1,
        'yt_channel_follower_count': info.get('channel_follower_count') if info.get('channel_follower_count') is not None else -1,
        'yt_categories': " | ".join(str(c) for c in categories),
        'video_downloaded': False,
        'last_modified': datetime.now(),
    }
    return pd.DataFrame([row])




def _extract_metadata(url: str, item_id: str, verbose: bool = False):
    """yt-dlp metadata extraction with retry. Returns (info, None) or (None, fail_df)."""
    ydl_opts: dict = {
        'quiet': True,
        'no_warnings': not verbose,
        **scraper_cookies.cookie_opts("youtube"),
        'skip_download': True,
        'noplaylist': True,
        'no_color': True,
        'extractor_retries': 3,
        'socket_timeout': 30,
        'js_runtimes': _JS_RUNTIMES,
        # Metadata must never depend on the n-challenge solver: without a JS
        # runtime + yt-dlp-ejs, format extraction fails ("No video formats
        # found") even though all metadata fields are present. The media phase
        # runs its own extraction and does need the solver.
        'ignore_no_formats_error': True,
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
) -> bool:
    """Download the video to temp and move/upload it. Returns success."""
    temp_dir = fyp_cf['paths']['temp']
    out_template = join(temp_dir, f"{item_id}.%(ext)s")
    dl_opts: dict = {
        'quiet': True,
        'no_warnings': not verbose,
        **scraper_cookies.cookie_opts("youtube"),
        'outtmpl': out_template,
        'no_color': True,
        'overwrites': True,
        'noplaylist': True,
        'format': _FORMAT,
        'merge_output_format': 'mp4',
        'retries': 3,
        'socket_timeout': 30,
        'js_runtimes': _JS_RUNTIMES,
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
                return False

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
            return True

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
            return False

        except Exception as e:
            logger.error("Scrape %s download unexpected error: %s", item_id, e)
            _cleanup_temp_files(temp_dir, item_id)
            return False

    return False




# Raw column names → canonical base names. Contract-named yt_* columns pass
# through unchanged.
_RAW_TO_CANONICAL: dict[str, str] = {
    "create_time_raw": "create_time",
    "duration_raw": "duration",
    "play_count_raw": "play_count",
    "author_name_raw": "author_name",
    "last_modified": "scrape_ts",
}




class YouTubeScraper(BaseScraper):
    """YouTube platform scraper (yt-dlp, authenticated via research-account cookies).

    Ad impressions from watch history (``activity_type="ad_play"``) carry ad
    creative ids that usually resolve to "Video unavailable" — they fail as
    ``permanent:removed`` and are pruned from the queue naturally.
    """

    platform = "youtube"
    url_template = "https://www.youtube.com/watch?v={item_id}"
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

        if not save_media:
            return data_row

        duration = data_row.loc[0, 'duration_raw']
        if not self.should_download_media(duration):
            logger.info("Item '%s' duration (%ss) exceeds %ss cap. Skipping download.",
                        item_id, duration, self.media_duration_cap())
            return data_row

        ok = _download_media(url, item_id, save_path,
                             stream_to_bucket=stream_to_bucket, verbose=verbose)
        if ok:
            data_row.loc[0, 'video_downloaded'] = True
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
        # One authenticated session shared by all threads; bot_check events
        # shrink concurrency via the throttle controller.
        return (min(max_workers, 2), 1, 4)


    def health_check(self) -> dict | None:
        return scraper_cookies.cookie_health("youtube", session_cookie="__Secure-3PSID")
