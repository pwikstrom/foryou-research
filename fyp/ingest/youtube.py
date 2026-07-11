"""YouTube DDP collection class.

Carved out of the flat ``fyp/ingest.py`` (Phase 8); shared helpers stay in
``fyp.ingest.base``. Imports of siblings go through the package directly
(never the old-path shims) — see the shim-poisoning rule in
docs/fyp-import-graph.md.
"""

import html
import io
import json
import re

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.ingest.base import (
    ForYouBaseCollection,
    _config_timezone_offset,
    derive_play_duration,
    parse_donor_timezone,
)
from fyp.logging_setup import get_logger
from fyp.utils import read_zip_members

logger = get_logger(__name__)


class YouTubeDDPCollection(ForYouBaseCollection):
    """YouTube / Google Takeout watch-history ingester.

    Parses ``history/watch-history.json`` or ``history/watch-history.html``
    from the uploaded Takeout zip — Takeout emits one or the other depending on
    the account's export settings; JSON is preferred when both are present
    because its timestamps are unambiguous ISO-8601 UTC. Organic video watches
    become ``activity_type='play'``; served ad impressions ("From Google Ads")
    become ``activity_type='ad_play'``; non-video events (Shorts-creation,
    community-post views) are dropped. The donated title and channel are
    captured as an enrichment seed via the base ``seed_*`` contract.

    Engagement is read from the Takeout CSVs alongside the watch history:
    ``comments/comments.csv`` → ``comment`` rows (comment text in
    ``extra_data``), ``playlists/Liked videos.csv`` → ``fave`` rows and
    ``playlists/Favorites videos.csv`` → ``save`` rows. All three carry exact
    ISO-8601 timestamps and video ids, so ``derive_play_duration`` folds them
    into the matching watch-history play's ``extra_data``.
    """

    platform_url_template = "https://www.youtube.com/watch?v={item_id}"
    source_platform = "youtube"
    raw_path = "youtube_raw"

    _MEMBER_SUFFIX_HTML = "history/watch-history.html"
    _MEMBER_SUFFIX_JSON = "history/watch-history.json"
    # (zip-member suffix, activity_type, timestamp column) for the engagement
    # CSVs. Every member is optional — most Takeout accounts have only some.
    _ENGAGEMENT_MEMBERS = [
        ("comments/comments.csv", "comment", "Comment create timestamp"),
        ("playlists/Liked videos.csv", "fave", "Playlist video creation timestamp"),
        ("playlists/Favorites videos.csv", "save", "Playlist video creation timestamp"),
    ]
    _VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")
    _BODY_RE = re.compile(r'body-1[^>]*>(.*?)</div>', re.S)
    _CAPTION_RE = re.compile(r'mdl-typography--caption">(.*?)</div>', re.S)
    _VIDEO_RE = re.compile(r'watch\?v=([\w-]{11})')
    _TITLE_RE = re.compile(r'watch\?v=[\w-]{11}[^"]*">(.*?)</a>', re.S)
    _CHANNEL_RE = re.compile(r'/channel/([\w-]+)"[^>]*>(.*?)</a>', re.S)
    _CHANNEL_ID_RE = re.compile(r'/channel/([\w-]+)')
    # Takeout renders timestamps in the account's display locale. Supported:
    # day-first ("29 Jun 2026, 21:43:42 AEST", September as "Sept", June/July in
    # full) and US month-first 12-hour ("Apr 4, 2024, 5:36:02 PM PDT"), with an
    # abbreviated zone or a "GMT+05:30"-style offset. AM/PM may be preceded by
    # a narrow/regular no-break space in newer exports.
    _TS_RE = re.compile(
        r'(\d{1,2} [A-Za-z]{3,9} \d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4}), '
        r'(\d{1,2}:\d{2}:\d{2})'
        r'(?:[\s\u202f\u00a0]*([APap][Mm]))?'
        r'[\s\u202f\u00a0]+([A-Z]{2,5}(?:[+-]\d{1,2}:?\d{2})?)'
    )
    _GMT_OFFSET_RE = re.compile(r'^(?:GMT|UTC)([+-])(\d{1,2}):?(\d{2})$')
    _MONTH_NORM_RE = re.compile(r'([A-Za-z]{3})[A-Za-z]*')

    # Timezone abbreviation -> UTC offset (hours). Some abbreviations are
    # ambiguous across regions; those get the interpretation most common in
    # Takeout exports (IST: India, not Ireland/Israel; CST/EST/PST: US) and are
    # reported per file so mislabelled donations stay auditable. Unknown
    # abbreviations fall back to the project timezone's offset.
    _TZ_OFFSETS = {
        "AEST": 10, "AEDT": 11, "ACST": 9.5, "ACDT": 10.5, "AWST": 8,
        "NZST": 12, "NZDT": 13, "GMT": 0, "UTC": 0, "BST": 1, "IST": 5.5,
        "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "WET": 0, "WEST": 1,
        "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6,
        "PST": -8, "PDT": -7, "HKT": 8, "JST": 9, "KST": 9, "SGT": 8,
    }
    _AMBIGUOUS_TZ = {"IST", "CST", "BST", "EST"}





    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.source_platform = "youtube"
        self.data_source = "ddp"
        self.min_required_rows_per_raw_file = 10





    @classmethod
    def zip_member_suffixes(cls) -> list[str]:
        """Watch-history members (JSON or HTML) plus the engagement CSVs."""
        return [cls._MEMBER_SUFFIX_JSON, cls._MEMBER_SUFFIX_HTML] + [
            suffix for suffix, _, _ in cls._ENGAGEMENT_MEMBERS
        ]





    @classmethod
    def _parse_history(cls, html_text: str) -> list[dict]:
        """Return one raw row per watch-history cell that references a video.

        Cells without a ``watch?v=`` link (Shorts-creation, community-post views)
        are skipped. Ad impressions are flagged from the details/caption cell
        (not the whole block, whose title text could contain the marker
        phrase); removed/unavailable videos keep their id but carry no donated
        title.
        """
        rows: list[dict] = []
        for block in html_text.split('<div class="outer-cell')[1:]:
            body_match = cls._BODY_RE.search(block)
            body = body_match.group(1) if body_match else ""

            video_match = cls._VIDEO_RE.search(body)
            if not video_match:
                continue
            item_id = video_match.group(1)

            title_match = cls._TITLE_RE.search(body)
            title = html.unescape(title_match.group(1)) if title_match else None
            if title and "watch?v=" in title:
                title = None  # removed/unavailable video: title is just the URL

            channel_match = cls._CHANNEL_RE.search(body)
            channel_id = channel_match.group(1) if channel_match else None
            channel_name = html.unescape(channel_match.group(2)) if channel_match else None

            caption_match = cls._CAPTION_RE.search(block)
            caption = caption_match.group(1) if caption_match else ""

            ts_match = cls._TS_RE.search(body)
            rows.append({
                "item_id": item_id,
                "is_ad": "From Google Ads" in caption,
                "yt_date": ts_match.group(1) if ts_match else None,
                "yt_time": ts_match.group(2) if ts_match else None,
                "yt_ampm": ts_match.group(3) if ts_match else None,
                "yt_tz": ts_match.group(4) if ts_match else None,
                "seed_desc": title if title else pd.NA,
                "seed_author_id": channel_id if channel_id else pd.NA,
                "seed_author_name": channel_name if channel_name else pd.NA,
            })
        return rows





    @classmethod
    def _parse_history_json(cls, payload: list) -> list[dict]:
        """Return one raw row per JSON watch-history record that references a video.

        Records without a ``watch?v=`` video id in ``titleUrl`` (Shorts-creation
        tools, Shorts-ad views with no target video, community posts) are
        skipped. Ad impressions carry ``details: [{"name": "From Google Ads"}]``;
        removed/unavailable videos echo the watch URL as the title and carry no
        donated title. Timestamps are ISO-8601 UTC strings, emitted verbatim in
        ``yt_json_time`` and converted in ``load_single_raw``.
        """
        rows: list[dict] = []
        for record in payload:
            if not isinstance(record, dict):
                continue
            video_match = cls._VIDEO_RE.search(record.get("titleUrl") or "")
            if not video_match:
                continue

            title = (record.get("title") or "").removeprefix("Watched ").strip()
            if not title or "watch?v=" in title:
                title = None  # removed/unavailable video: title is just the URL

            subtitles = record.get("subtitles") or []
            channel = subtitles[0] if subtitles and isinstance(subtitles[0], dict) else {}
            channel_match = cls._CHANNEL_ID_RE.search(channel.get("url") or "")

            details = record.get("details") or []
            is_ad = any(
                isinstance(detail, dict) and detail.get("name") == "From Google Ads"
                for detail in details
            )

            rows.append({
                "item_id": video_match.group(1),
                "is_ad": is_ad,
                "yt_json_time": record.get("time"),
                "seed_desc": title if title else pd.NA,
                "seed_author_id": channel_match.group(1) if channel_match else pd.NA,
                "seed_author_name": channel.get("name") or pd.NA,
            })
        return rows





    @classmethod
    def _parse_engagement(cls, members: dict[str, bytes | None], filename: str) -> pd.DataFrame:
        """Parse the optional engagement CSVs into activity rows.

        Comments become ``comment`` rows with the comment text in
        ``extra_data`` (Takeout serialises it as ``{"text": "..."}``); liked /
        favorited videos become ``fave`` / ``save`` rows. Rows without a valid
        11-char video id or a parseable timestamp are skipped.

        Args:
            members: The ``read_zip_members`` result for this upload.
            filename: The upload's name, for error messages.

        Returns:
            A frame with ``item_id`` / ``activity_type`` / ``utc_timestamp`` /
            ``extra_data`` columns — empty when no engagement member is present.

        Raises:
            ValueError: when a present member cannot be parsed as CSV —
                a structural failure that must leave the file pending.
        """
        frames = []
        for suffix, activity_type, ts_column in cls._ENGAGEMENT_MEMBERS:
            raw = members.get(suffix)
            if raw is None:
                continue
            try:
                csv_df = pd.read_csv(io.BytesIO(raw))
            except Exception as exc:
                raise ValueError(f"'{filename}' member '{suffix}' is not parseable CSV: {exc}") from exc
            if "Video ID" not in csv_df.columns or ts_column not in csv_df.columns:
                raise ValueError(
                    f"'{filename}' member '{suffix}' lacks the expected "
                    f"'Video ID' / '{ts_column}' columns"
                )
            part = pd.DataFrame({
                "item_id": csv_df["Video ID"].astype("string").str.strip(),
                "utc_timestamp": pd.to_datetime(
                    csv_df[ts_column], utc=True, format="ISO8601", errors="coerce"
                ),
                "activity_type": activity_type,
            })
            if activity_type == "comment" and "Comment text" in csv_df.columns:
                part["extra_data"] = csv_df["Comment text"].astype("string").map(cls._comment_text)
            part = part[
                part["item_id"].notna()
                & part["item_id"].str.fullmatch(cls._VIDEO_ID_RE.pattern)
                & part["utc_timestamp"].notna()
            ]
            if not part.empty:
                frames.append(part)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)




    @staticmethod
    def _comment_text(cell) -> object:
        """Extract the plain text from a Takeout comment cell (``{"text": ...}``)."""
        if not isinstance(cell, str) or not cell.strip():
            return pd.NA
        try:
            payload = json.loads(cell)
            if isinstance(payload, dict) and payload.get("text"):
                return str(payload["text"])
        except ValueError:
            pass
        return cell




    @classmethod
    def _convert_timestamps(cls, df: pd.DataFrame, donor_tz=None) -> pd.Series:
        """Vectorised conversion of the parsed timestamp components to UTC.

        Normalises the month token to its 3-letter form (Takeout renders
        "Sept"/"June"/"July"), tries the day-first then the US month-first date
        format, and applies 12-hour AM/PM arithmetic to build each row's naive
        local wall-clock. The local time is then turned into UTC either by
        localising to an authoritative ``donor_tz`` (from the manifest —
        DST-correct and unambiguous) or, when none was supplied, by subtracting
        the offset read from the row's own timezone label (abbreviation map or
        an explicit GMT±HH:MM). Rows whose components did not parse come back NaT.

        Args:
            df: Frame with ``yt_date`` / ``yt_time`` / ``yt_ampm`` / ``yt_tz``.
            donor_tz: A ``tzinfo`` from the ingestion manifest, or ``None``.
        """
        dates_raw = df["yt_date"].astype("string").str.replace(cls._MONTH_NORM_RE, r"\1", regex=True)
        dates = pd.to_datetime(dates_raw, format="%d %b %Y", errors="coerce")
        dates = dates.fillna(pd.to_datetime(dates_raw, format="%b %d, %Y", errors="coerce"))

        times = pd.to_timedelta(df["yt_time"].astype("string").fillna(""), errors="coerce")
        ampm = df["yt_ampm"].astype("string").str.upper().fillna("")
        hours = times.dt.components["hours"]
        pm_shift = ((ampm == "PM") & (hours < 12)).astype("int64") * 12
        am_shift = ((ampm == "AM") & (hours == 12)).astype("int64") * -12
        times = times + pd.to_timedelta(pm_shift + am_shift, unit="h")

        naive = dates + times

        # Authoritative donor timezone from the manifest overrides the (sometimes
        # ambiguous) display label entirely.
        if donor_tz is not None:
            return naive.dt.tz_localize(
                donor_tz, ambiguous="NaT", nonexistent="shift_forward"
            ).dt.tz_convert("UTC")

        tz = df["yt_tz"].astype("string").fillna("")
        offsets = tz.map(cls._TZ_OFFSETS).astype("float64")
        gmt_parts = tz.str.extract(cls._GMT_OFFSET_RE)
        gmt_known = gmt_parts[1].notna()
        gmt_sign = np.where((gmt_parts[0] == "-").fillna(False), -1.0, 1.0)
        gmt_hours = pd.to_numeric(gmt_parts[1], errors="coerce").fillna(0)
        gmt_minutes = pd.to_numeric(gmt_parts[2], errors="coerce").fillna(0)
        gmt_offsets = gmt_sign * (gmt_hours + gmt_minutes / 60)
        offsets = offsets.where(~(offsets.isna() & gmt_known), pd.Series(gmt_offsets, index=df.index))

        unknown = offsets.isna() & (tz != "")
        ambiguous = tz.isin(cls._AMBIGUOUS_TZ)
        if unknown.any():
            logger.warning(
                f"WARNING: {int(unknown.sum())} YouTube row(s) carry an unrecognised "
                f"timezone label ({sorted(tz[unknown].unique().tolist())}); "
                f"falling back to the project timezone offset. Set a donor timezone "
                f"in the upload form to resolve this exactly."
            )
        if ambiguous.any():
            logger.info(
                f"NOTE: {int(ambiguous.sum())} YouTube row(s) use an ambiguous timezone "
                f"abbreviation ({sorted(tz[ambiguous].unique().tolist())}); using the "
                f"most common Takeout interpretation. Set a donor timezone in the "
                f"upload form to resolve this exactly."
            )
        offsets = offsets.fillna(_config_timezone_offset())

        return (naive - pd.to_timedelta(offsets, unit="h")).dt.tz_localize("UTC")





    def load_single_raw(self, filename: str) -> pd.DataFrame:
        """Extract the watch-history member from the Takeout zip and parse it.

        Takeout exports the history as JSON or HTML depending on the account's
        export settings; both members are requested in one archive pass and the
        JSON one is preferred (unambiguous ISO-8601 UTC timestamps, no display
        locale involved). Timestamps are converted here (not in
        ``process_single``) so an unsupported locale is detected while the file
        can still be left pending rather than ledger-blacklisted.

        Raises:
            ValueError: when the zip is unreadable, has no watch-history
                member, holds invalid JSON, or none of its rows yields a
                parseable timestamp (unsupported display locale) — structural
                failures that must stay pending rather than be discarded as
                too-small.
        """
        local_path = data_io.local_copy(storage_location=self.raw_path, filename=filename)
        if not local_path:
            raise ValueError(f"could not fetch '{filename}' from '{self.raw_path}'")

        try:
            members = read_zip_members(local_path, type(self).zip_member_suffixes())
        finally:
            data_io.release_local_copy(local_path)
        raw_json = members[self._MEMBER_SUFFIX_JSON]
        raw_html = members[self._MEMBER_SUFFIX_HTML]
        if raw_json is None and raw_html is None:
            raise ValueError(
                f"'{filename}' contains no '{self._MEMBER_SUFFIX_JSON}' or "
                f"'{self._MEMBER_SUFFIX_HTML}' member"
            )

        donor_tz = parse_donor_timezone(self._current_file_tz)
        if raw_json is not None:
            try:
                payload = json.loads(raw_json.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"'{filename}': '{self._MEMBER_SUFFIX_JSON}' is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, list):
                raise ValueError(
                    f"'{filename}': '{self._MEMBER_SUFFIX_JSON}' is not a list of records"
                )
            rows = self._parse_history_json(payload)
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame.from_records(rows)
            df["utc_timestamp"] = pd.to_datetime(
                df["yt_json_time"], utc=True, format="ISO8601", errors="coerce"
            )
            hint = ""
        else:
            rows = self._parse_history(raw_html.decode("utf-8", errors="replace"))
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame.from_records(rows)
            df["utc_timestamp"] = self._convert_timestamps(df, donor_tz=donor_tz)
            hint = (
                "" if donor_tz is not None
                else " — probably an unsupported display locale; set a donor "
                "timezone in the upload form to bypass the label."
            )

        if df["utc_timestamp"].isna().all():
            raise ValueError(
                f"'{filename}': {len(df)} watch event(s) found but no timestamp "
                f"could be parsed{hint}"
            )

        engagement = self._parse_engagement(members, filename)
        if not engagement.empty:
            df = pd.concat([df, engagement], ignore_index=True)
        return df





    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Flag ads vs organic watches and finalize the frame.

        Engagement rows (comment/fave/save) arrive with ``activity_type``
        already set by ``_parse_engagement``; only watch-history rows (where it
        is absent) get the ad/organic split.
        """
        df = df.copy()
        if "activity_type" not in df.columns:
            df["activity_type"] = pd.NA
        history = df["activity_type"].isna()
        df.loc[history, "activity_type"] = np.where(
            df.loc[history, "is_ad"].eq(True), "ad_play", "play"
        )
        return derive_play_duration(self._finalize_activity_frame(df))





