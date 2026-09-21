#!/usr/bin/env python3
"""
YouTube scraper using yt-dlp as backend.

Fetches metadata + media for YouTube videos identified by their 11-character
video id (the ``item_id`` produced by :class:`fyp.ingest.YouTubeDDPCollection`).

Run it from a residential IP. In practice YouTube (like Instagram) does not
scrape from Cloud Run's datacenter IPs — the bot wall ("Sign in to confirm
you're not a bot") holds even with research-account cookies and proof-of-origin
(PO) tokens — so the local install, signed in through the operator's own
Chrome, drains this queue. The Cloud Run pieces remain wired but are not a
working path: the bgutil PO-token provider (pip plugin + script built in
Dockerfile.base, via :func:`_pot_extractor_args`), and ``bot_check`` as a
throttle signal so concurrency backs off.

Refused videos: the metadata leg adds the tv player client, the only one that
states WHY YouTube will not play a video, and captures that reason (see
:class:`_ReasonLog`). A video YouTube has no record of is a failure — never a
placeholder row — and one it keeps but will not play here (region, rights
claim) is scraped metadata-only and leaves the queue.

Most watch-history items are long-form and exceed the media duration cap —
they are deliberately scraped metadata-only; Shorts and clips get media.

Gotcha: YouTube's session rate-limit response is phrased as a removal
("Video unavailable. This content isn't available, try again later. The
current session has been rate-limited…"). Classification checks rate-limit
keywords before the removal keywords so it stays transient + throttled.
"""


import logging
import os
from datetime import datetime, timezone
from glob import glob
from os import remove
from os.path import exists, join
from time import sleep

import pandas as pd
import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ExtractorError, GeoRestrictedError

from fyp.scrape import scraper_cookies
from fyp.scrape.platform_scraper import BaseScraper, cleanup_temp_files, empty_fail

logger = logging.getLogger(__name__)




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf


# -------------------------------------------------------------------------
# Error classification
# -------------------------------------------------------------------------

# "bot_check" is transient AND a throttle signal (_THROTTLE_CATEGORIES in
# platform_scraper): the batch backs off instead of burning the whole queue
# against the bot wall. HTTP 403 is typically YouTube throttling (unlike
# TikTok, where it means an IP block) — kept retryable. "blocked" is a
# copyright (Content ID) block — "It was blocked due to the claimed content
# by <claimant>" — distinct from "removed" so a later run from another
# vantage point can single it out, like "geo_blocked".
_RETRYABLE = {"bot_check", "rate_limited", "network", "server_error", "unknown"}
_PERMANENT = {"removed", "private", "age_restricted", "members_only", "geo_blocked",
              "blocked"}

# Permanent verdicts that stand even when YouTube still has the video's record
# (channel, views, duration): the player refuses it HERE, on grounds that name
# the video itself — a region whitelist or a rights holder's claim. No
# throttled session has ever produced these; it answers a bare "Video
# unavailable" (2026-09-18). Anything else with the record intact goes down
# the ordinary media leg, whose verdict is distrusted and budgeted.
_UNPLAYABLE_WITH_RECORD = {"geo_blocked", "blocked"}

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

# Player clients for the metadata leg: yt-dlp's defaults plus "tv". When
# YouTube refuses a video, the default web clients all report a bare "Video
# unavailable" — the very text a throttled session returns — whereas the tv
# client states the reason: "removed by the uploader", "The uploader has not
# made this video available in your country", "It was blocked due to the
# claimed content by …". Measured 2026-09-21: ~1 s more per item, and a
# playable video resolves the same formats. The media leg keeps the defaults.
_METADATA_PLAYER_CLIENTS = ['default', 'tv']

# The bgutil PO-token provider's script directory (Dockerfile.base builds it
# and sets this env var). YouTube requires proof-of-origin tokens for media
# streams from datacenter IPs — cookies alone don't pass the bot wall.
_POT_SERVER_HOME_ENV = 'BGUTIL_POT_SERVER_HOME'




def _pot_extractor_args() -> dict:
    """yt-dlp opts wiring the bgutil PO-token provider (script mode).

    Returns an ``extractor_args`` fragment pointing the bgutil plugin
    (``bgutil-ytdlp-pot-provider`` in requirements.txt) at the provider
    script, or ``{}`` when the script isn't present (e.g. local dev, where a
    residential IP passes the bot wall without PO tokens).
    """
    server_home = os.environ.get(_POT_SERVER_HOME_ENV, '')
    if server_home and exists(join(server_home, 'build', 'generate_once.js')):
        return {'extractor_args': {'youtubepot-bgutilscript': {'server_home': [server_home]}}}
    return {}


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify a yt-dlp error into (category, detail) for retry decisions.

    Returns:
        (category, detail) where category is one of:
        - "bot_check"      — "Sign in to confirm you're not a bot" wall, captcha
        - "rate_limited"   — HTTP 429/403, too many requests
        - "removed"        — video deleted/unavailable, account terminated
        - "private"        — private video
        - "age_restricted" — age gate (cookies already applied → permanent)
        - "members_only"   — channel-membership gate
        - "geo_blocked"    — GeoRestrictedError
        - "blocked"        — copyright (Content ID) claim block
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

    return _classify_message(msg)


def _classify_message(msg: str) -> tuple[str, str]:
    """Classify a yt-dlp error or warning text; see :func:`_classify_error`.

    Split out so the metadata leg can classify the playability reason it
    captures from a warning (see :class:`_ReasonLog`) — there is no exception
    object there, only the text.
    """
    # YouTube uses typographic apostrophes ("confirm you’re not a bot") —
    # normalize so ASCII keyword matching works.
    msg_lower = msg.lower().replace('’', "'")

    if ("confirm you're not a bot" in msg_lower or 'not a robot' in msg_lower
            or 'captcha' in msg_lower):
        return "bot_check", msg

    # Rate-limit detection must precede the "removed" keywords: YouTube's
    # session rate-limit response reads "Video unavailable. This content isn't
    # available, try again later. The current session has been rate-limited…"
    # — it contains the removal phrasing, but the item is fine and retryable.
    if ('rate-limited' in msg_lower or 'rate limit' in msg_lower
            or 'too many requests' in msg_lower
            or 'http error 403' in msg_lower or 'http error 429' in msg_lower):
        return "rate_limited", msg

    if 'private video' in msg_lower or 'video is private' in msg_lower:
        return "private", msg

    if 'confirm your age' in msg_lower or 'age-restricted' in msg_lower or 'age restricted' in msg_lower:
        return "age_restricted", msg

    if 'members-only' in msg_lower or 'members only' in msg_lower or 'join this channel' in msg_lower:
        return "members_only", msg

    # Geo restrictions sometimes surface as a flattened message instead of a
    # GeoRestrictedError instance. A territorial copyright block ("…who has
    # blocked it in your country on copyright grounds") lands here too — it
    # is region-bound, which is what the category records.
    if 'in your country' in msg_lower or 'geo restriction' in msg_lower:
        return "geo_blocked", msg

    # A Content ID block names the rights holder: "It was blocked due to the
    # claimed content by Paramount Global (PMN)." / "…who has blocked it on
    # copyright grounds." Only the tv player client states it (the web clients
    # say a bare "Video unavailable"). A copyright TAKEDOWN ("no longer
    # available due to a copyright claim") says nothing of blocking and falls
    # through to "removed".
    if 'claimed content' in msg_lower or ('copyright' in msg_lower and 'blocked' in msg_lower):
        return "blocked", msg

    # "This content isn't available, try again later" without the rate-limit
    # sentence is YouTube's soft-block/removal phrasing — kept as removed.
    # "This video is unavailable" is the phrasing for an id YouTube has no
    # record of; it matched none of these until 2026-09-21 and churned as
    # "unknown" for days (every one of the nine seen was gone for good). The
    # tv client words takedowns as "It was removed following a copyright
    # removal request by <claimant>".
    if any(kw in msg_lower for kw in ('video unavailable', 'video is unavailable',
                                       'has been removed', 'was removed', 'removal request',
                                       'no longer available', 'account associated',
                                       'terminated', 'does not exist', 'not available')):
        return "removed", msg

    if any(kw in msg_lower for kw in ('timed out', 'timeout', 'connection', 'network',
                                       'ssl', 'certificate', 'dns', 'reset by peer')):
        return "network", msg

    return "unknown", msg




def _empty_fail(error_type: str = "unknown", error_detail: str = "", *,
                corroborated: bool = False) -> pd.DataFrame:
    """Return an empty DataFrame tagged with error classification metadata."""
    return empty_fail(error_type, error_detail, corroborated=corroborated)


def _cleanup_temp_files(temp_dir: str, item_id: str) -> None:
    """Remove any partial download files for an item from the temp directory."""
    cleanup_temp_files(temp_dir, item_id)




def _parse_create_time(info: dict) -> datetime:
    """Upload time from ``timestamp``, falling back to ``upload_date`` (YYYYMMDD)."""
    ts = info.get('timestamp')
    if ts:
        try:
            # Parsed as UTC then made naive so the value does not depend on
            # the scraping machine's timezone. See the contract dtype.
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
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




def _metadata_extractor_args() -> dict:
    """``extractor_args`` for the metadata leg: the PO-token wiring + tv client."""
    args = dict(_pot_extractor_args().get('extractor_args', {}))
    args['youtube'] = {'player_client': list(_METADATA_PLAYER_CLIENTS)}
    return {'extractor_args': args}


class _ReasonLog:
    """yt-dlp logger that keeps the playability reasons of one extraction.

    The metadata leg runs with ``ignore_no_formats_error``, under which yt-dlp
    downgrades a player's refusal ("This video has been removed by the
    uploader") to a warning and returns an info dict anyway — the reason
    exists nowhere else. Only ``[youtube]`` extractor warnings are kept;
    plugin chatter (the PO-token provider) and the generic no-formats
    follow-ups are not reasons. yt-dlp routes errors here too once a logger
    is set; our own "attempt failed" line re-reports them, so they go to
    debug.
    """

    _NOT_REASONS = ('[pot', 'no video formats found', 'requested format is not available',
                    'n challenge', 'formats have been skipped', 'sabr')

    def __init__(self):
        self.reasons: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        text = str(msg)
        if not text.startswith('[youtube] '):
            return
        text = text[len('[youtube] '):]
        lowered = text.lower()
        if any(marker in lowered for marker in self._NOT_REASONS):
            return
        self.reasons.append(text)

    def error(self, msg):
        logger.debug("yt-dlp: %s", msg)


def _playability_verdict(reasons: list[str]) -> tuple[str, str] | None:
    """Classify the reasons one extraction reported; ``None`` when there were none.

    Safety first: if any reason reads as throttling (bot wall, captcha, rate
    limit), that verdict wins — a permanent reason must never mask a session
    problem. Otherwise the first reason that names a category wins over an
    unrecognised one.
    """
    if not reasons:
        return None
    verdicts = [_classify_message(r) for r in reasons]
    for category, detail in verdicts:
        if category in ("bot_check", "rate_limited"):
            return category, detail
    for category, detail in verdicts:
        if category != "unknown":
            return category, detail
    return verdicts[0]


def _has_no_record(info: dict) -> bool:
    """True when yt-dlp returned only a placeholder for the video.

    For a live video the player refuses here (geo- or copyright-blocked) the
    info dict still carries the channel, view count and duration. For one that
    no longer exists it carries none of them — only a synthesised title
    ("youtube video #<id>") — and until 2026-09-21 that shell was saved as a
    scraped row: no author, -1 plays, created 2000-01-01.
    """
    return all(info.get(k) is None for k in ('channel_id', 'uploader_id', 'view_count', 'duration'))


def _extract_metadata(url: str, item_id: str, verbose: bool = False):
    """yt-dlp metadata extraction with retry. Returns (info, None) or (None, fail_df).

    A video YouTube has no record of is a failure, not a row: the verdict is
    the stated reason, and when that reason is itself permanent the failure is
    corroborated (see :meth:`BaseScraper.fetch`) — two independent signals
    agree that the item, not the session, is the problem. When the record
    exists but no format does, the classified reason travels to
    :meth:`YouTubeScraper.fetch` as ``info['_fyp_unplayable']``.
    """
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
        **_metadata_extractor_args(),
        # Metadata must never depend on the n-challenge solver: without a JS
        # runtime + yt-dlp-ejs, format extraction fails ("No video formats
        # found") even though all metadata fields are present. The media phase
        # runs its own extraction and does need the solver. The flag also
        # swallows a refused video's reason, hence the capturing logger.
        'ignore_no_formats_error': True,
    }

    for attempt in range(_META_MAX_RETRIES):
        reason_log = _ReasonLog()
        try:
            with yt_dlp.YoutubeDL({**ydl_opts, 'logger': reason_log}) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and not info.get('formats'):
                verdict = _playability_verdict(reason_log.reasons)
                if _has_no_record(info):
                    category, detail = verdict or (
                        "unknown", "no record of the video and no stated reason")
                    logger.warning("Scrape %s metadata: no record of the video — [%s] %s",
                                   item_id, category, detail)
                    return None, _empty_fail(category, detail,
                                             corroborated=category in _PERMANENT)
                if verdict is not None:
                    info['_fyp_unplayable'] = verdict
            return info, None
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
    """Download the video to temp and move/upload it.

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
        **_pot_extractor_args(),
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




# Raw column names → canonical base names. The raw yt_* counts/handle translate
# to the generic base fields here; the genuinely platform-specific yt_* columns
# (yt_channel_follower_count, yt_categories) pass through unchanged.
_RAW_TO_CANONICAL: dict[str, str] = {
    "create_time_raw": "create_time",
    "duration_raw": "duration",
    "play_count_raw": "play_count",
    "author_name_raw": "author_name",
    "last_modified": "scrape_ts",
    "yt_like_count": "fave_count",
    "yt_comment_count": "comment_count",
    "yt_author_handle": "author_handle",
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
    residential_ip_only = True


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

        unplayable = info.get('_fyp_unplayable')
        if unplayable is not None and unplayable[0] in _UNPLAYABLE_WITH_RECORD:
            # YouTube keeps the video's record but will not play it here, and
            # says why in terms of the video itself. The media leg could only
            # repeat that, so the verdict is corroborated: the metadata row
            # stands and the id leaves the queue instead of burning retries.
            logger.info("Item '%s' is unplayable here — [%s] %s. Metadata only.",
                        item_id, *unplayable)
            data_row.attrs['media_error_type'], data_row.attrs['media_error_detail'] = unplayable
            data_row.attrs['verdict_corroborated'] = True
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


    # Pacing. Every request rides ONE signed-in session (locally: the user's
    # own Chrome cookies on a residential IP), and YouTube throttles that
    # session, not the individual videos — on 2026-09-18 it soft-blocked after
    # ~700 media pulls in 34 min at 2-4 concurrent with a 1.5 s delay, and
    # answered a bare "Video unavailable" for every stream after that. These
    # defaults keep a drain to roughly a dozen pulls a minute; override under
    # ``[misc]`` with scraper_youtube_max_concurrency /
    # scraper_youtube_inter_request_delay / scraper_youtube_max_batch_size.
    def _pacing(self, key: str, default):
        try:
            return type(default)(_cf()["misc"].get(f"scraper_youtube_{key}", default))
        except Exception:
            return default


    def throttle_limits(self, max_workers: int) -> tuple[int, int, int]:
        # bot_check events shrink concurrency further via the throttle controller.
        cap = max(1, self._pacing("max_concurrency", 2))
        return (min(max_workers, cap), 1, cap)


    def inter_request_delay(self) -> float:
        return max(0.0, self._pacing("inter_request_delay", 5.0))


    def max_batch_size(self) -> int | None:
        cap = self._pacing("max_batch_size", 250)
        return cap if cap > 0 else None


    def health_check(self) -> dict | None:
        return scraper_cookies.cookie_health("youtube", session_cookie="__Secure-3PSID")


    def media_probe_url(self, item_id: str) -> dict | None:
        # Unlike the metadata path, format resolution here depends on the
        # n-challenge solver (JS runtime + yt-dlp-ejs) and PO tokens — the same
        # plumbing the media download uses, which is exactly what the probe
        # should exercise.
        ydl_opts: dict = {
            'quiet': True,
            'no_warnings': True,
            **scraper_cookies.cookie_opts("youtube"),
            'skip_download': True,
            'noplaylist': True,
            'no_color': True,
            'socket_timeout': 30,
            'format': _FORMAT,
            'js_runtimes': _JS_RUNTIMES,
            **_pot_extractor_args(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(self.item_url(item_id), download=False)
            return self._probe_target(ydl, info)
