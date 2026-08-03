"""Instagram DDP collection class.

Carved out of the flat ``fyp/ingest.py`` (Phase 8); shared helpers stay in
``fyp.ingest.base``. Imports of siblings go through the package directly
(never the old-path shims) — see the shim-poisoning rule in
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
    (``ads_and_topics/posts_viewed.json``) → ``activity_type='play'``, plus
    liked posts (``likes/liked_posts.json`` → ``activity_type='fave'``). The
    feed-impression streams are what give a liked reel/post a play row to fold
    onto — likes are mostly on feed items, which never appear in
    ``stories_viewed``. Both the current ``label_values`` record schema and the
    classic ``string_list_data`` / ``string_map_data`` schema are supported. The donated
    caption and owner are captured as an enrichment seed via the base ``seed_*``
    contract. Structural failures (unreadable zip, missing members, invalid
    JSON) raise so the file stays pending instead of being silently discarded.
    """

    # /p/ rather than /reel/: the ingested streams mix reels, feed videos and
    # image posts, and Instagram redirects /p/<shortcode> to the right surface
    # for all three, while /reel/<shortcode> 404s on an image post.
    platform_url_template = "https://www.instagram.com/p/{item_id}/"
    source_platform = "instagram"
    raw_path = "instagram_raw"

    # (inner zip-member suffix, activity_type) for each stream we ingest.
    _STREAMS = [
        ("story_interactions/stories_viewed.json", "play"),
        ("ads_and_topics/videos_watched.json", "play"),
        ("ads_and_topics/posts_viewed.json", "play"),
        ("likes/liked_posts.json", "fave"),
    ]
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
            if "label_values" in payload or "string_list_data" in payload:
                return [payload]
            for value in payload.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value
        return []





    @classmethod
    def _extract(cls, record: dict) -> tuple[str | None, str | None, str | None, str | None, int | None]:
        """Return ``(item_id, desc, author_id, author_name, timestamp)`` for one record.

        Supports both Instagram export record schemas: the current
        ``label_values`` list (URL/Caption labels + doubly-nested ``Owner``
        block) and the classic ``string_list_data`` / ``string_map_data`` shape
        (record-level ``title`` is the media owner's username; the href and
        timestamp live in the first ``string_list_data`` entry).
        """
        url = desc = author_id = author_name = None
        timestamp = record.get("timestamp")

        if "label_values" in record:
            for lv in record.get("label_values", []):
                label = lv.get("label")
                if label == "URL" and not url:
                    url = lv.get("value") or lv.get("href")
                elif label == "Caption" and not desc:
                    desc = lv.get("value")
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
            if timestamp is None:
                smd = record.get("string_map_data") or {}
                for entry in smd.values():
                    if isinstance(entry, dict) and entry.get("timestamp"):
                        timestamp = entry["timestamp"]
                        break

        item_id = None
        if url:
            match = cls._SHORTCODE_RE.search(url)
            if match:
                item_id = match.group(1)
        return item_id, desc, author_id, author_name, timestamp





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
                item_id, desc, author_id, author_name, timestamp = self._extract(record)
                # item_id is nullable in the activity contract (classic story
                # views carry no URL); a row without a timestamp is useless.
                if timestamp is None:
                    continue
                rows.append({
                    "item_id": item_id if item_id else pd.NA,
                    "activity_type": activity_type,
                    "ig_timestamp": timestamp,
                    "seed_desc": repair_mojibake(desc) if desc else pd.NA,
                    "seed_author_id": author_id if author_id else pd.NA,
                    "seed_author_name": repair_mojibake(author_name) if author_name else pd.NA,
                })

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame.from_records(rows)





    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert the unix view/like timestamps to UTC and finalize the frame."""
        df = df.copy()
        df["utc_timestamp"] = pd.to_datetime(
            df["ig_timestamp"], unit="s", utc=True, errors="coerce"
        )
        return derive_play_duration(self._finalize_activity_frame(df))





