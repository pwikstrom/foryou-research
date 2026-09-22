"""TikTok collection classes: DDP, AIO, and Zeeschuimer captures.

Carved out of the flat ``fyp/ingest.py`` in the subpackage restructure; shared
helpers stay in ``fyp.ingest.base``. Imports of siblings go through the
package directly (never the old-path shims) — see the shim-poisoning rule in
docs/fyp-import-graph.md.
"""

import os
import re
from collections import deque
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.ingest.base import (
    ForYouBaseCollection,
    derive_play_duration,
)
from fyp.logging_setup import get_logger
from fyp.utils import clean_url

logger = get_logger(__name__)


class TikTokDDPCollection(ForYouBaseCollection):
    """TikTok data-download export (``user_data_tiktok.json``) ingester.

    Watch history becomes ``play``; the Like List becomes ``fave``, the
    donor's bookmarks (Favorite Videos) ``save``, share history and reposts
    ``share`` (the share method or ``repost`` in ``extra_data``), comments
    ``comment`` (text in ``extra_data``), and followed accounts ``follow``
    (username in ``extra_data``). Every item-bearing engagement row folds
    onto its play through ``derive_play_duration``; follows carry no item and
    stay standalone.

    A comment record names its video only in newer exports
    (``originalPostUrl``); older ones give no video id at all, so the comment
    borrows the id of the last activity within 180 s and says so in
    ``link_method`` (``ffill_180s``).
    """

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"
    source_platform = "tiktok"
    # Historical name: "ddp_raw" resolves to activity_data/ddp/ddp_raw (a
    # static fyp_config entry from when TikTok was the only platform), not to
    # activity_data/tiktok/ddp_raw as the self-registration convention would
    # give. The Instagram/YouTube classes use platform-keyed folders. See the
    # note in fyp_config.py before renaming.
    raw_path = "ddp_raw"

    # Lowercased export list key -> activity_type. This is the whitelist of
    # sections process_single keeps; it also drives the pre-upload review UI
    # (review_manifest), so the two can never drift apart. Matched on the
    # list's own key, so the parent section may be renamed between export
    # vintages ("Activity" → "Your Activity", "Like List" under either
    # "Likes and Favorites" or "Your Activity") without losing rows.
    #   ItemFavoriteList  — the Like List (a heart)            → fave
    #   FavoriteVideoList — the donor's bookmarks               → save
    #   ShareHistoryList  — {Date, SharedContent, Link, Method} → share
    #   RepostList        — {Date, Link}, a public re-share     → share
    #   Following         — {Date, UserName}, no item           → follow
    # Sections TikTok exports but the Hub does not ingest (favourite sounds /
    # hashtags / effects / collections, DMs, ads, settings, ...) are absent
    # here and stripped in the browser before upload.
    _ACTIVITY_TYPE_MAP = {
        'videolist': 'play', 'commentslist': 'comment',
        'searchlist': 'search', 'fanslist': 'followed_by', 'following': 'follow',
        'itemfavoritelist': 'fave', 'favoritevideolist': 'save',
        'sharehistorylist': 'share', 'repostlist': 'share',
    }
    emitted_activity_types = frozenset({
        'play', 'comment', 'search', 'followed_by', 'follow', 'fave', 'save',
        'share', 'login',
    })
    # Record keys that name the video an activity was about, looked up by
    # name rather than by position: TikTok puts the link at index 1 for most
    # sections but at index 2 in ShareHistoryList (after SharedContent), and
    # newer comment records carry the video under `originalPostUrl`.
    _LINK_KEYS = ('link', 'originalposturl')
    _VIDEO_ID_RE = re.compile(r"/video/(\d+)")
    # Sections whose 'VideoList' holds the donor's OWN uploads rather than
    # watch history. TikTok reuses the key for both — Your Activity -> Watch
    # History -> VideoList is what they watched, Post -> Posts -> VideoList is
    # what they posted — so the key alone cannot tell them apart and posted
    # videos used to count toward the play floor in load_single_raw. Matched on
    # the PARENT section, and deliberately a denylist: an export vintage that
    # renames the watch-history section must keep ingesting, not silently lose
    # every play. ("Videos" is the older layout's Video -> Videos -> VideoList.)
    _POSTED_VIDEO_SECTIONS = {"posts", "videos"}

    # Participant-facing card titles for the review UI, keyed by section id.
    _REVIEW_TITLES = {
        'videolist': 'Videos you watched',
        'commentslist': 'Comments you made',
        'searchlist': 'Your searches',
        'fanslist': 'Accounts that follow you',
        'following': 'Accounts you follow',
        'itemfavoritelist': 'Videos you liked',
        'favoritevideolist': 'Videos you saved',
        'sharehistorylist': 'Videos you shared',
        'repostlist': 'Videos you reposted',
    }

    @classmethod
    def accepted_upload_suffixes(cls) -> list[str]:
        return [".json"]

    @classmethod
    def review_manifest(cls) -> dict:
        """Pre-upload review manifest: the whitelist sections plus login history.

        ``unmapped_policy: "strip"`` — every list-of-dicts section the client's
        DFS walk finds that is not listed here (DMs, settings, ads data, …) is
        removed from the upload in the browser and disclosed to the donor as
        "not included". The ``second_key_ip`` rule mirrors process_single's
        login detection (a list whose records' second key is ``ip``).
        """
        sections = [
            {"id": sid, "title": cls._REVIEW_TITLES.get(sid, sid), "row_delete": True}
            for sid in cls._ACTIVITY_TYPE_MAP
        ]
        sections.append({
            "id": "__login__", "id_rule": "second_key_ip",
            "title": "Login history (IP addresses)", "row_delete": True,
        })
        # The donor's own uploads sit under a second 'VideoList'. Keyed on that
        # name alone the client showed them as a *second* "Videos you watched"
        # card and counted them toward the viability floor below — the same
        # confusion _section_activity_type fixes on the parser side, so the
        # parent sections listed here are the same ones.
        sections.append({
            "id": "posted_videolist", "id_rule": "parent_in",
            "match_key": "videolist", "parents": sorted(cls._POSTED_VIDEO_SECTIONS),
            "title": "Videos you posted", "row_delete": True,
        })
        return {
            "kind": "json_sections",
            "unmapped_policy": "strip",
            "viability": {
                "section": "videolist", "min_rows": 11,
                "message": "A TikTok donation needs at least 11 watched videos to be usable.",
            },
            "sections": sections,
        }

    def __init__(self, collection_id: str = None, verbose: bool = False):
        # The extra_data column is used for the comment string, the account name that was just followed, etc...
        # play_duration is a base activity-contract column (derive_play_duration), no extras needed here.
        super().__init__(collection_id, verbose)
        self.source_platform = "tiktok"
        self.data_source = "ddp"
        self.raw_path = "ddp_raw"
        self.min_required_rows_per_raw_file = 10




    @classmethod
    def _walk_sections(cls, donation_dict: dict) -> list[dict]:
        """Flatten an export into one record per list item, named by section.

        Depth-first over the whole document; every list of dicts becomes
        records ``{"activity_type": <section name>, "variable_list": [lowercased
        keys], "value_list": [values]}``. The stack carries the PARENT key
        alongside each node because the section name alone is ambiguous:
        TikTok uses the same 'VideoList' key for watch history and for the
        donor's own uploads (see _POSTED_VIDEO_SECTIONS). Shared with the
        stored-data migration so both read an export the same way.
        """
        donation_items = []
        stack = deque([(None, None, donation_dict)])
        while stack:
            parent, feature, obj = stack.pop()
            if isinstance(obj, list):
                activity_type = cls._section_activity_type(parent, feature)
                for item in obj:
                    if isinstance(item, dict) and item:
                        donation_items.append({
                            "activity_type": activity_type,
                            "variable_list": [k.lower() for k in item.keys()],
                            "value_list": list(item.values())
                        })
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    stack.append((feature, k, v))
        return donation_items


    @classmethod
    def _unpack_record(cls, variables, values) -> tuple:
        """Return ``(primary_label, extra_data, link, context)`` for one record.

        ``variables`` are the record's lowercased keys with ``date`` at
        index 0. ``primary_label`` / ``extra_data`` are the name and value at
        index 1; ``link`` is the value of the first key in ``_LINK_KEYS``
        wherever it sits (None when the record names no video); ``context`` is
        the share ``method`` (None otherwise).
        """
        variables = list(variables)
        values = list(values)
        by_name = dict(zip(variables, values))
        primary = variables[1] if len(variables) > 1 else None
        extra = values[1] if len(values) > 1 else None
        link = None
        for key in cls._LINK_KEYS:
            candidate = by_name.get(key)
            if isinstance(candidate, str) and cls._VIDEO_ID_RE.search(candidate):
                link = candidate
                break
        context = by_name.get("method")
        if not isinstance(context, str) or not context.strip():
            context = None
        return primary, extra, link, context


    @classmethod
    def _section_activity_type(cls, parent: str | None, feature: str | None) -> str:
        """Name the section a list belongs to, disambiguating reused keys.

        Returns the lowercased key, except for a 'VideoList' sitting under a
        posted-videos section, which gets a name outside _ACTIVITY_TYPE_MAP so
        process_single books it as an excluded-by-design section instead of a
        play.
        """
        name = (feature or '').lower()
        if name == "videolist" and (parent or '').lower() in cls._POSTED_VIDEO_SECTIONS:
            return "posted_videolist"
        return name


    def load_single_raw(self, filename: str) -> pd.DataFrame:

        donation_dict = data_io.load_json(storage_location = self.raw_path, filename = filename)

        # load_json swallows parse errors and returns None — e.g. when the raw
        # TikTok export .zip was uploaded instead of the extracted .json.
        if not isinstance(donation_dict, dict):
            raise ValueError(
                f"'{filename}' is not readable as a JSON document. TikTok DDP "
                f"ingestion expects the extracted user_data_tiktok.json, not "
                f"the export .zip."
            )

        donation_items = self._walk_sections(donation_dict)

        # initialising the dataframe from the raw data.
        if len(donation_items) == 0:
            return pd.DataFrame()
        df = pd.DataFrame.from_records(donation_items)

        # a data donation package without at least a few play activities is not useful.
        # Watch history is 'videolist'; the donor's own uploads were relabelled above
        # so they cannot prop a donation up over this floor (see _section_activity_type).
        n_play_activities = len(df[df['activity_type'] == 'videolist'])
        if n_play_activities <= 10:
            if self.verbose: logger.info(f"Discarding {filename} as it only has {n_play_activities} play activities.")
            return pd.DataFrame()

        return df






    def process_single(self, df: pd.DataFrame):

        df = df.copy()

        # An empty group never reaches the unpacking below intact: `.map()` on
        # an empty column returns a non-boolean Series, which pandas reads as a
        # list of *column labels* rather than a row mask, so `df[mask]` comes
        # back with zero columns and the next lookup raises a baffling
        # KeyError. Bail out while the frame still has its schema.
        if len(df) == 0:
            return df

        # `variable_list` / `value_list` arrive as pyarrow-backed list columns
        # whenever every donated value was a string, because the per-file
        # frames then stack cleanly inside fast_vertical_concat — exports
        # carrying nested values force its object-dtype pandas fallback
        # instead, which is why this only bites the flat ones. On an Arrow
        # list column `.map()` hands the callback a numpy array instead of a
        # list, so the `isinstance(x, list)` test below rejects every row.
        # Unpack back to plain object-dtype lists so the dtype the concat
        # happened to pick cannot change what this parser sees.
        for col in ("variable_list", "value_list"):
            if col in df.columns and df[col].dtype != object:
                df[col] = pd.Series(list(df[col]), index=df.index, dtype=object)

        # Records in sections the parser never ingests (Off TikTok Activity,
        # ads data, direct messages, settings, ...) are counted per file as
        # "outside_whitelist", so the ledger tells a by-design exclusion from
        # a record the parser failed to read. Login records carry no section
        # in the whitelist and are recognised by their second key, as below.
        in_whitelist = df["activity_type"].isin(list(self._ACTIVITY_TYPE_MAP))
        is_login = df["variable_list"].map(lambda x: isinstance(x, list) and len(x) > 1 and x[1] == "ip")
        outside = ~(in_whitelist | is_login)
        if outside.any() and "raw_file" in df.columns:
            self._record_file_drops(df.loc[outside, "raw_file"].value_counts(), "outside_whitelist")
        df = df[~outside].copy()
        if len(df) == 0:
            return df

        # -----------------------------------------------------
        # unpack the variable/value list. The two lists variable & value list contain a label (e.g. 'link')
        # and the value (e.g. 'https://www.tiktok.com/...') at the corresponding indeces. At index 0 is always the date
        # and I'm only unpacking index 1 in addition of date even though there may be additional data in the lists.

        # if 'date' is not the first element in the variable_list, something is wrong with this activity
        # so I keep activities/rows that have at least two elements in the variable_list and the first element is 'date'
        mask_date = df['variable_list'].map(lambda x: isinstance(x, list) and len(x) > 1 and x[0] == 'date')
        df = df[mask_date].copy()
        if len(df) == 0:
            return df

        mask_activity_type = df['activity_type'].map(lambda x:"chat history with" not in x)
        df = df[mask_activity_type].copy()
        if len(df) == 0:
            return df

        # get the date from index zero (I don't need the variable name)
        df['date'] = pd.to_datetime(df['value_list'].str[0], format='%Y-%m-%d %H:%M:%S', errors='coerce')

        # remove rows with invalid dates
        df = df[df['date'].notna()].copy()
        if len(df) == 0:
            return df

        if self.verbose:
            logger.info(f"   [{df['raw_file'].iloc[0]}] Keeping {len(df):,} rows w OK timestamp.")


        # Unpack the record. `primary_label` / `extra_data` are the name and
        # value at index 1 (the comment text, search term, followed username,
        # or — for most sections — the video link). The video link is looked
        # up by NAME as well, because ShareHistoryList puts it at index 2 and
        # newer comment records carry it under `originalPostUrl`, so a
        # position-only unpack would lose every share and every observed
        # comment link. `_link` holds that value; `_context` the extra field a
        # share carries (Method).
        try:
            unpacked = [
                self._unpack_record(v, x)
                for v, x in zip(df['variable_list'], df['value_list'])
            ]
            df['primary_label'] = [u[0] for u in unpacked]
            df['extra_data'] = [u[1] for u in unpacked]
            df['_link'] = [u[2] for u in unpacked]
            df['_context'] = [u[3] for u in unpacked]
        except Exception as e:
            logger.warning(f"Could not unpack variable_list/value_list ({e}); filling with NA.")
            df['primary_label'] = pd.NA
            df['extra_data'] = pd.NA
            df['_link'] = pd.NA
            df['_context'] = pd.NA


        # -----------------------------------------------------
        # item_id: the video id inside the link, for any section that has one.
        # Comments carry no link in older exports (item_id NA, back-filled
        # below); a share of a LIVE has a non-video link and stays NA too.
        df["item_id"] = (
            df["_link"].astype("string").str.extract(self._VIDEO_ID_RE, expand=False)
            .where(df["activity_type"].notna())
            .astype("string[pyarrow]")
        )

        # The link was the whole payload for the link-at-index-1 sections;
        # drop that redundant copy. A comment keeps its text, a share keeps
        # its method, a follow keeps the username.
        df.loc[df["primary_label"] == "link", "extra_data"] = pd.NA
        # A repost record has no method field: say what kind of share it was.
        is_repost = df["activity_type"] == "repostlist"
        df.loc[is_repost, "_context"] = "repost"
        has_context = df["_context"].notna()
        df.loc[has_context, "extra_data"] = df.loc[has_context, "_context"].astype("string").str.lower()
        df.drop(columns=["_link", "_context"], inplace=True)


        # -----------------------------------------------------
        # activity_type:

        # map activity types (whitelist shared with review_manifest)
        df["activity_type"] = df["activity_type"].map(self._ACTIVITY_TYPE_MAP)

        # activity_type is NA for login activities - this fixes that by creating a new activity type
        df.loc[df[df["primary_label"]=="ip"].index,"activity_type"] = "login"
        
        # Convert activity_type to pyarrow string
        df["activity_type"] = df["activity_type"].astype("string[pyarrow]")

        # cleanup - remove play activities that don't have an item_id
        df = df[((df["activity_type"] != "play") | (df["item_id"].notna()))].copy()
        

        # -----------------------------------------------------
        # utc_timestamp and tz_offset:

        # tiktok timestamps are in nanoseconds - convert date to seconds since epoch
        # rename timestamp to utc_timestamp and convert to datetime
        df['timestamp'] = (df['date'].astype("int64") // 1_000_000_000)
        df = df.rename(columns={"timestamp": "utc_timestamp"})
        df["utc_timestamp"] = pd.to_datetime(df["utc_timestamp"], unit='s', utc=True)

        # tz_offset comes from the shared tail: a donor timezone supplied at upload
        # (the manifest `tz`) is authoritative; without one the offset is inferred
        # from the activity rhythm, which assumes an 'actual' TikTok user using
        # TikTok like a normal TikTok user does — an artificially produced export
        # will mislead it. Until 2026-09 this parser called the inference directly
        # and silently ignored a supplied zone on the platform with the most
        # donations. The tail also sorts chronologically and resets the index.
        df = self._finalize_activity_frame(df)


        # -----------------------------------------------------
        # It seems like the data donation packages keep play logs for a certain time back
        # in time, but they keep other engagement stats for longer. It is difficult to handle
        # engagement stats without connection to a play activity, so I remove all activities before 
        # the first play activity. It feels a bit brutal to throw away data, but I'm not sure what else to do.
        #if (df["activity_type"] == "play").any():
        #    first_play_idx = df[df["activity_type"] == "play"].index[0]
        #    df = df.loc[first_play_idx:].copy()

        #print(len(df))


        # ----------------------------------------------------------------------------------------------
        # Associate comments without an item_id to the item_id of the preceding activity within
        # the same short burst. Only comments are backfilled — other activity types retain their
        # original item_id (or null).
        #
        # NOTE: this is a TRANSIENT, per-raw-file grouping used ONLY for comment item_id
        # backfill — hence the deliberately short 180s gap. It is dropped at the end of this
        # block and is NOT the persistent "sitting" session_id assigned to every activity later
        # (see assign_session_ids / add_session_ids: 900s gap, computed on the full
        # per-collection sequence after migration, and persisted for downstream analysis).

        # 1. calculate time between activities (in seconds)
        df['delta'] = df['utc_timestamp'] - df['utc_timestamp'].shift(1)
        df['delta'] = df['delta'].dt.total_seconds()

        # 2. use the time delta to establish bursts of activities very close together, which I
        # assume belong to the same brief engagement (e.g. watching a video and commenting on
        # it). The 180s limit is a reasonable max time to spend on one video and engage with it.
        df['_assoc_break'] = (df['delta'].isna()) | (df['delta'] > 180)
        df['_assoc_session'] = df['_assoc_break'].astype(bool).cumsum()

        # 3. Forward-fill item_id within each burst, then apply only to comment rows that
        # are missing an item_id. All other activity types keep their original value.
        ffilled_item_id = df.groupby('_assoc_session')['item_id'].ffill()
        comment_missing = (df['activity_type'] == 'comment') & df['item_id'].isna()
        df.loc[comment_missing, 'item_id'] = ffilled_item_id[comment_missing]

        # 4. Say so on the row. A comment whose item_id was supplied by the
        # forward fill carries link_method="ffill_180s", so an analysis can tell
        # an observed video id from an inferred one (the contract documents the
        # inference; this column makes it filterable).
        df["link_method"] = pd.array([pd.NA] * len(df), dtype="string[pyarrow]")
        df.loc[comment_missing & df["item_id"].notna(), "link_method"] = "ffill_180s"

        df.drop(columns=['_assoc_break', '_assoc_session', 'delta'], inplace=True)


        # -----------------------------------------------------
        # play_duration: forward time-delta to the next recorded activity, attributed
        # to play events (shared, platform-agnostic derivation — see derive_play_duration).

        return derive_play_duration(df)












class TikTokAIOCollection(TikTokDDPCollection):
    """TikTok DDP data fetched from AIO AWS infrastructure (S3/DynamoDB).

    Uses the same DDP JSON format as TikTokDDPCollection but loads data
    from the Australian Internet Observatory's AWS S3 bucket instead of
    user-uploaded files. Also fetches participant metadata from DynamoDB.
    """

    ingestion_mode = "fetch"
    raw_path = "aio_raw"

    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.data_source = "aio"
        self.raw_path = "aio_raw"


    @staticmethod
    def _aws_fetch_enabled() -> bool:
        """Whether the ingest refresh should auto-fetch AIO data from AWS.

        Controlled by ``[features] aio_aws_fetch`` in the config. When the key
        is absent the default is "on Cloud Run only" (``K_SERVICE`` set): the
        deployed research instance keeps fetching with zero configuration,
        while a fresh local install — where ambient ``~/.aws`` credentials may
        belong to an unrelated account — stays quiet. The manual "Fetch AIO"
        button in Data Management is unaffected.
        """
        from fyp.fyp_config import fyp_cf

        configured = fyp_cf.get("features", {}).get("aio_aws_fetch")
        if configured is not None:
            return bool(configured)
        return bool(os.environ.get("K_SERVICE"))


    def load_raw(self, skip_these_raw_files: list[str] = [],
                 held_for_review: set[str] | None = None):
        """Fetch recent donations and participant metadata from AWS, then load files."""
        from fyp.donations import (
            get_donation_metadata_from_aio_aws,
            get_recent_data_donations_from_aio_aws,
        )
        if not self._aws_fetch_enabled():
            if self.verbose:
                logger.info(
                    "AIO AWS auto-fetch disabled ([features].aio_aws_fetch; "
                    "default off outside Cloud Run). Processing existing local files."
                )
            super().load_raw(skip_these_raw_files=skip_these_raw_files,
                             held_for_review=held_for_review)
            return

        if self.verbose:
            logger.info("Fetching recent AIO donations from AWS...")
        try:
            get_recent_data_donations_from_aio_aws(
                storage_location=self.raw_path
            )
        except Exception as e:
            if self.verbose:
                logger.warning(f"AWS data fetch failed: {e}. Processing existing local files.")

        if self.verbose:
            logger.info("Fetching AIO participant metadata from DynamoDB...")
        try:
            get_donation_metadata_from_aio_aws(verbose=self.verbose)
        except Exception as e:
            if self.verbose:
                logger.warning(f"AWS metadata fetch failed: {e}.")

        super().load_raw(skip_these_raw_files=skip_these_raw_files,
                             held_for_review=held_for_review)





class TikTokZeeschuimerCollection(ForYouBaseCollection):

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"
    source_platform = "tiktok"

    raw_path = "zeeschuimer_raw"
    # A browser capture sees what the feed served, never a like or a comment.
    emitted_activity_types = frozenset({"observe"})

    @classmethod
    def accepted_upload_suffixes(cls) -> list[str]:
        return [".ndjson"]

    def __init__(self, collection_id: str = None, verbose: bool = False):
        # The extra_data column is used for the timezone name

        super().__init__(collection_id, verbose)
        self.raw_path = "zeeschuimer_raw"
        self.min_required_rows_per_raw_file = 1
        self.source_platform = "tiktok"
        self.data_source = "zeeschuimer"
        self.accepted_tiktok_urls = [
            'https://www.tiktok.com/foryou',
            'https://www.tiktok.com/',
            'https://www.tiktok.com/en',
        ]





    def load_single_raw(self, filename: str) -> pd.DataFrame:
        #data = []
        #with open(filename, 'r') as file:
        #    for line in file:
        #        data.append(json.loads(line))
            
        data = data_io.read_ndjson_file(storage_location = self.raw_path, filename = filename)

        if data is not None and len(data) > 0:
            df = pd.json_normalize(data)

            # Only keeping data from accepted tiktok urls
            if 'source_platform_url' in df.columns:
                df = df[df['source_platform_url'].isin(self.accepted_tiktok_urls)].copy()
        
        return df






    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        # zeeschuimer data is really basic - well, there is a lot of useful data in the ndjson, but to generate
        # an activity collection, which is the purpose here, I am only using the item_id and the timestamp

        df = df.copy()
        
        # Extract lots of useful data from the source_url to get tz_offset etc
        source_details = []
        for ii in df.index:
            source_details += [clean_url(df['source_url'][ii])]
        source_details = pd.DataFrame(source_details, index=df.index)
        df = pd.merge(left=df, right=source_details, left_index=True, right_index=True)


        # -----------------------------------------------------
        # I call all activities from zeeschuimer 'observe' to distinguish it from 'play'
        df["activity_type"] = "observe"


        # -----------------------------------------------------
        # item_id: 
        # Filter valid item_ids to make sure they're not corrupted
        if 'item_id' in df.columns:
             df = df[df.item_id.map(lambda x:all([u in "0123456789" for u in x]) and len(x) == 19)].copy()
        

        # -----------------------------------------------------
        # tz_offset and utc_timestamp:

        # timestamp_collected is a Unix epoch in milliseconds — parse directly
        # as tz-aware UTC. The prior implementation used datetime.fromtimestamp()
        # which returns a naive datetime in the *server's* local timezone, then
        # relocalised it as if it were in source_url.tz_name. That only produced
        # correct UTC when the ingestion server and the user happened to share a
        # timezone — off by the local offset otherwise.
        df["utc_timestamp"] = pd.to_datetime(
            df["timestamp_collected"].astype(np.int64), unit='ms', utc=True
        )

        unique_tz = df["source_url.tz_name"].dropna().unique()

        # tz_offset is the user's offset from UTC at the time of the event,
        # derived from source_url.tz_name so DST boundaries are respected.
        if len(unique_tz) == 1:
            tz = ZoneInfo(unique_tz[0])
            df["tz_offset"] = (
                df["utc_timestamp"].dt.tz_convert(tz).apply(
                    lambda t: t.utcoffset().total_seconds() / 3600 if pd.notna(t) else np.nan
                )
            )
        elif len(unique_tz) > 1:
            offset_parts = []
            for tz_name, block in df.groupby("source_url.tz_name", sort=False):
                tz = ZoneInfo(tz_name)
                part = block["utc_timestamp"].dt.tz_convert(tz).apply(
                    lambda t: t.utcoffset().total_seconds() / 3600 if pd.notna(t) else np.nan
                )
                offset_parts.append(part)
            df["tz_offset"] = pd.concat(offset_parts).sort_index()
        
        # I'm keeping this information in the extra_data column. It's a string so it works fine
        df.rename(columns={"source_url.tz_name": "extra_data"}, inplace=True)

        return df











