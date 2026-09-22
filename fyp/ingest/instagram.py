"""Instagram DDP collection class.

Carved out of the flat ``fyp/ingest.py`` in the subpackage restructure; shared
helpers stay in ``fyp.ingest.base``. Imports of siblings go through the
package directly (never the old-path shims) — see the shim-poisoning rule in
docs/fyp-import-graph.md.
"""

import json
import re

import pandas as pd

import fyp.data_io as data_io
from fyp.ingest.base import (
    ForYouBaseCollection,
    derive_play_duration,
)
from fyp.logging_setup import get_logger
from fyp.utils import read_zip_members, repair_mojibake

logger = get_logger(__name__)


class InstagramDDPCollection(ForYouBaseCollection):
    """Instagram "Download Your Information" export ingester.

    Parses the activity streams we care about from the uploaded zip: viewed
    reels (``story_interactions/stories_viewed.json``), watched feed videos
    (``ads_and_topics/videos_watched.json``) and viewed feed posts
    (``ads_and_topics/posts_viewed.json``) → ``activity_type='play'``; liked
    posts (``likes/liked_posts.json`` → ``fave``); saved posts
    (``saved/saved_posts.json`` → ``save``); and the donor's own comments
    (``comments/post_comments_1.json``, ``comments/reels_comments.json`` →
    ``comment``, text in ``extra_data``, the media owner as
    ``seed_author_id``). The feed-impression streams are what give a liked or
    saved reel/post a play row to fold onto — likes are mostly on feed items,
    which never appear in ``stories_viewed``. A comment record names the media
    owner but not the media, so comments have no ``item_id`` and never fold;
    they are counted from their standalone rows (My Collections) only. Both
    the current ``label_values`` record schema and the classic
    ``string_list_data`` / ``string_map_data`` schema are supported. The
    donated caption and owner are captured as an enrichment seed via the base
    ``seed_*`` contract. Structural failures (unreadable zip, missing members,
    invalid JSON) raise so the file stays pending instead of being silently
    discarded.
    """

    # /p/ rather than /reel/: the ingested streams mix reels, feed videos and
    # image posts, and Instagram redirects /p/<shortcode> to the right surface
    # for all three, while /reel/<shortcode> 404s on an image post.
    platform_url_template = "https://www.instagram.com/p/{item_id}/"
    source_platform = "instagram"
    raw_path = "instagram_raw"

    # (inner zip-member suffix, activity_type) for each stream we ingest.
    # Saved posts fold onto their play like a like does. Comments carry the
    # media owner and the text but NO media URL in Instagram's export, so they
    # stay standalone rows (item_id null) — see the class docstring.
    _STREAMS = [
        ("story_interactions/stories_viewed.json", "play"),
        ("ads_and_topics/videos_watched.json", "play"),
        ("ads_and_topics/posts_viewed.json", "play"),
        ("likes/liked_posts.json", "fave"),
        ("saved/saved_posts.json", "save"),
        ("comments/post_comments_1.json", "comment"),
        ("comments/reels_comments.json", "comment"),
    ]
    emitted_activity_types = frozenset({"play", "fave", "save", "comment"})
    # Participant-facing card titles for the pre-upload review UI, keyed by
    # stream suffix. A stream added to _STREAMS shows up in the review
    # automatically (falling back to its suffix as the title until named here).
    _STREAM_TITLES = {
        "story_interactions/stories_viewed.json": "Stories you viewed",
        "ads_and_topics/videos_watched.json": "Videos you watched",
        "ads_and_topics/posts_viewed.json": "Posts you viewed",
        "likes/liked_posts.json": "Posts you liked",
        "saved/saved_posts.json": "Posts you saved",
        "comments/post_comments_1.json": "Comments you made on posts",
        "comments/reels_comments.json": "Comments you made on reels",
    }
    _SHORTCODE_RE = re.compile(r"instagram\.com/(?:reel|p|tv)/([\w-]+)")





    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.source_platform = "instagram"
        self.data_source = "ddp"
        self.min_required_rows_per_raw_file = 10





    @classmethod
    def accepted_upload_suffixes(cls) -> list[str]:
        return [".zip"]




    @classmethod
    def zip_member_suffixes(cls) -> list[str]:
        """The two activity-stream members read from the export zip."""
        return [suffix for suffix, _ in cls._STREAMS]




    @classmethod
    def review_manifest(cls) -> dict:
        """Pre-upload review manifest: one row-level section per ingested stream."""
        return {
            "kind": "zip_members",
            # Counts the viewing streams only. A total across every section let
            # liked posts carry a donation over the line, which load_single_raw
            # then rejects for having too few views — the donor saw the refusal
            # only after uploading. min_rows tracks min_required_rows_per_raw_file.
            "viability": {
                "sections": [suffix for suffix, activity in cls._STREAMS if activity == "play"],
                "min_rows": 10,
                "message": "An Instagram donation needs at least 10 viewed posts, "
                           "videos or stories to be usable.",
            },
            "sections": [
                {"id": suffix, "title": cls._STREAM_TITLES.get(suffix, suffix),
                 "parser": "instagram_records", "row_delete": True}
                for suffix, _ in cls._STREAMS
            ],
        }





    @staticmethod
    def _records(payload: object) -> list[dict]:
        """Normalise a stream file's JSON into a flat list of activity records.

        Handles a bare list, a bare dict holding a single record, and wrapper
        dicts like ``{"likes_media_likes": [...]}`` (classic exports).
        """
        if payload is None:
            return []
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            if any(k in payload for k in ("label_values", "string_list_data", "string_map_data")):
                return [payload]
            for value in payload.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value
        return []





    @classmethod
    def _extract(cls, record: dict) -> tuple[str | None, str | None, str | None, str | None, int | None, str | None]:
        """Return ``(item_id, desc, author_id, author_name, timestamp, text)`` for one record.

        Supports both Instagram export record schemas: the current
        ``label_values`` list (URL/Caption labels + doubly-nested ``Owner``
        block) and the classic ``string_list_data`` / ``string_map_data`` shape
        (record-level ``title`` is the media owner's username; the href and
        timestamp live in the first ``string_list_data`` entry, or — for saved
        posts — in a ``string_map_data`` entry such as ``"Saved on"``). A
        comment record (``string_map_data`` with ``Comment`` / ``Media Owner``
        / ``Time``, or ``label_values`` with those labels) yields the comment
        as ``text`` and the media owner as ``author_id``; it has no URL.
        """
        url = desc = author_id = author_name = text = None
        timestamp = record.get("timestamp")

        if "label_values" in record:
            for lv in record.get("label_values", []):
                label = lv.get("label")
                if label == "URL" and not url:
                    url = lv.get("value") or lv.get("href")
                elif label == "Caption" and not desc:
                    desc = lv.get("value")
                elif label == "Comment" and not text:
                    text = lv.get("value")
                elif label == "Media Owner" and not author_id:
                    author_id = lv.get("value")
                elif label == "Time" and timestamp is None:
                    timestamp = lv.get("timestamp") or lv.get("value")
                elif lv.get("title") == "Owner":
                    for outer in lv.get("dict", []):
                        for inner in outer.get("dict", []):
                            if inner.get("label") == "Name" and not author_name:
                                author_name = inner.get("value")
                            elif inner.get("label") == "Username" and not author_id:
                                author_id = inner.get("value")
        else:
            author_id = record.get("title") or None
            entries = record.get("string_list_data") or []
            first = entries[0] if entries and isinstance(entries[0], dict) else {}
            url = first.get("href")
            if timestamp is None:
                timestamp = first.get("timestamp")
            smd = record.get("string_map_data") or {}
            if isinstance(smd, dict):
                for key, entry in smd.items():
                    if not isinstance(entry, dict):
                        continue
                    if url is None and entry.get("href"):
                        url = entry["href"]
                    if timestamp is None and entry.get("timestamp"):
                        timestamp = entry["timestamp"]
                    if key == "Comment" and not text:
                        text = entry.get("value")
                    elif key == "Media Owner" and not author_id:
                        author_id = entry.get("value")

        item_id = None
        if url:
            match = cls._SHORTCODE_RE.search(url)
            if match:
                item_id = match.group(1)
        try:
            timestamp = int(timestamp) if timestamp is not None else None
        except (TypeError, ValueError):
            timestamp = None
        return item_id, desc, author_id, author_name, timestamp, text





    def load_single_raw(self, filename: str) -> pd.DataFrame:
        """Extract the viewed-reels and liked-posts streams from the upload zip.

        Raises:
            ValueError: when the zip is unreadable, holds none of the expected
                members, or a member is not valid JSON — structural failures
                that must stay pending rather than be discarded as too-small.
        """
        local_path = data_io.local_copy(storage_location=self.raw_path, filename=filename)
        if not local_path:
            raise ValueError(f"could not fetch '{filename}' from '{self.raw_path}'")

        try:
            members = read_zip_members(local_path, [s for s, _ in self._STREAMS])
        finally:
            data_io.release_local_copy(local_path)
        if all(raw is None for raw in members.values()):
            raise ValueError(
                f"'{filename}' contains none of the expected Instagram activity "
                f"files ({', '.join(s for s, _ in self._STREAMS)})"
            )

        rows = []
        for suffix, activity_type in self._STREAMS:
            raw = members[suffix]
            if raw is None:
                continue
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"'{filename}' member '{suffix}' is not valid JSON: {exc}") from exc
            for record in self._records(payload):
                item_id, desc, author_id, author_name, timestamp, text = self._extract(record)
                # item_id is nullable in the activity contract (classic story
                # views carry no URL, comments name no media); a row without
                # a timestamp is useless.
                if timestamp is None:
                    continue
                is_comment = activity_type == "comment"
                rows.append({
                    "item_id": item_id if item_id else pd.NA,
                    "activity_type": activity_type,
                    "ig_timestamp": timestamp,
                    # The comment text travels in extra_data like every other
                    # platform's; it must never seed an item caption.
                    "extra_data": repair_mojibake(text) if (is_comment and text) else pd.NA,
                    "seed_desc": repair_mojibake(desc) if (desc and not is_comment) else pd.NA,
                    "seed_author_id": author_id if author_id else pd.NA,
                    "seed_author_name": repair_mojibake(author_name) if author_name else pd.NA,
                })

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame.from_records(rows)

        # Only the viewing streams make a donation useful. Liked posts are
        # engagement, so without this the generic row-count floor in the load
        # loop would admit an export of nothing but likes — a collection that
        # contributes no viewing at all. Mirrors the watch-history floor in
        # TikTokDDPCollection.load_single_raw.
        n_views = int((df["activity_type"] == "play").sum())
        if n_views < self.min_required_rows_per_raw_file:
            if self.verbose:
                logger.info(f"Discarding {filename} as it only has {n_views} viewing activities.")
            return pd.DataFrame()

        return df





    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert the unix view/like timestamps to UTC and finalize the frame."""
        df = df.copy()
        df["utc_timestamp"] = pd.to_datetime(
            df["ig_timestamp"], unit="s", utc=True, errors="coerce"
        )
        return derive_play_duration(self._finalize_activity_frame(df))





