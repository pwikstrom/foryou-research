#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok downloader using yt-dlp as backend.

Drop-in alternative to mypyktok — returns the same single-row DataFrame
that generate_data_row() produces so downstream code is unchanged.
"""


from typing import Optional
from datetime import datetime
from os.path import join, getsize, exists
from os import remove
from pathlib import Path
from time import sleep
from glob import glob

import pandas as pd
import yt_dlp


from fyp.fyp_config import fyp_cf





# -------------------------------------------------------------------------
# Cookie handling — adapts to local dev vs Cloud Run
# -------------------------------------------------------------------------

def _cookie_opts() -> dict:
    """Return yt-dlp cookie options appropriate for the current environment.

    Local dev: extract cookies from Chrome browser.
    Cloud Run / Docker: use a Netscape cookie file if available, otherwise no cookies.
    """
    import os

    # Cloud Run sets K_SERVICE; Docker containers won't have a browser
    if os.environ.get('K_SERVICE') or not os.path.exists('/Applications'):
        cookie_file = os.environ.get('YTDLP_COOKIE_FILE', '')
        if cookie_file and os.path.exists(cookie_file):
            return {'cookiefile': cookie_file}
        return {}

    return {'cookiesfrombrowser': ('chrome',)}





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
# Image carousel detection and download
# -------------------------------------------------------------------------

def _extract_image_urls(video_url: str) -> list[str]:
    """Fetch TikTok page and extract image carousel URLs from itemStruct.

    Uses a plain HTTP request — no browser_cookie3 dependency so it works
    on Cloud Run. Image posts are typically public so cookies are optional.
    """
    from bs4 import BeautifulSoup
    from requests import get as requests_get
    from json import loads

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }

    try:
        resp = requests_get(video_url, headers=headers, timeout=20)
        soup = BeautifulSoup(resp.text, 'html.parser')
        script = soup.find('script', attrs={'id': '__UNIVERSAL_DATA_FOR_REHYDRATION__'})
        if script is None:
            return []
        tt_json = loads(script.string)
        item_struct = tt_json['__DEFAULT_SCOPE__']['webapp.video-detail']['itemInfo']['itemStruct']
        image_post = item_struct.get('imagePost', {})
        images = image_post.get('images', [])
        return [img['imageURL']['urlList'][0] for img in images if img.get('imageURL', {}).get('urlList')]
    except Exception:
        return []





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

    try:
        for k, one_image in enumerate(image_urls):
            image_fn = f"{video_id}_{k + 1:02}.jpeg"
            if stream_to_bucket is None:
                resp = requests_get(one_image, allow_redirects=True, headers=headers, timeout=60)
                with open(join(save_path, image_fn), 'wb') as f:
                    f.write(resp.content)
            else:
                resp = requests_get(one_image, headers=headers, stream=True, timeout=60)
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
    or an empty DataFrame on failure.
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
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        if verbose:
            print(f"ERROR (yt-dlp)-1: Failed to extract info for {video_url}: {e}")
        return pd.DataFrame()
    except Exception as e:
        if verbose:
            print(f"ERROR (yt-dlp)-2: Unexpected error for {video_url}: {e}")
        return pd.DataFrame()

    if info is None:
        if verbose:
            print(f"ERROR (yt-dlp)-3: No info returned for {video_url}")
        return pd.DataFrame()

    data_row = _info_to_row(info)

    # -------------------------------------------
    # Step 2: detect image carousel
    # -------------------------------------------
    is_slideshow = False
    image_urls: list[str] = []

    # yt-dlp slideshows have duration 0 or only audio formats
    formats = info.get('formats') or []
    has_video_format = any(f.get('vcodec', 'none') != 'none' for f in formats)

    if not has_video_format:
        image_urls = _extract_image_urls(video_url)
        if image_urls:
            is_slideshow = True
            data_row.loc[0, 'image_list'] = " | ".join(image_urls)

    # -------------------------------------------
    # Step 3: download media
    # -------------------------------------------
    if not save_video:
        return data_row

    duration = data_row.loc[0, 'video_duration']
    if isinstance(duration, (int, float)) and duration > max_duration_to_save:
        if verbose:
            print(f"Video '{video_id}' duration ({duration:,}s) exceeds {max_duration_to_save:,}s. Skipping download.")
        return data_row

    if is_slideshow and image_urls:
        # Download carousel images (same as pyktok path)
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
        }

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([video_url])

            # Find the downloaded file
            downloaded = join(temp_dir, f"{video_id}.mp4")
            if not exists(downloaded):
                # yt-dlp may have chosen a different extension
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
                    # Local mode: file is already in temp or move to save_path
                    from shutil import move
                    target = join(save_path, video_fn)
                    if downloaded != target:
                        move(downloaded, target)
                    data_row.loc[0, 'video_downloaded'] = True

                # Clean up temp file
                if exists(downloaded):
                    try:
                        remove(downloaded)
                    except OSError:
                        pass
            else:
                if verbose:
                    print(f"WARNING (yt-dlp): Download succeeded but file not found for '{video_id}'")

        except yt_dlp.utils.DownloadError as e:
            if verbose:
                print(f"WARNING (yt-dlp)-2: Failed to download video for {video_url}: {e}")
            sleep(3)
        except Exception as e:
            if verbose:
                print(f"WARNING (yt-dlp)-3: Unexpected download error for {video_url}: {e}")
            sleep(3)

    return data_row
