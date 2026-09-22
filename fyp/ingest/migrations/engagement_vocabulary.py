"""Bring stored activity rows onto the 2026-09 engagement vocabulary.

Until then TikTok bookmarks (``FavoriteVideoList``) were stored as ``fave``,
indistinguishable from likes, and followed accounts as ``following``. The
vocabulary is now ``fave`` / ``save`` / ``comment`` / ``share`` (+ ``follow``
standalone) — see ``fyp.core.utils``. This module rewrites the persisted
``collections_recoded.parquet`` accordingly:

1. ``following`` → ``follow`` on ``activity_type``;
2. TikTok ``fave`` rows whose ``(item_id, utc_timestamp)`` appear in the raw
   file's ``FavoriteVideoList`` → ``save`` (the section a row came from was
   never stored, so the raw export has to be read again — it is append-only
   and still there);
3. optionally, ``share`` rows appended for TikTok raw files that still carry
   ``ShareHistoryList`` / ``RepostList`` (files uploaded through the review
   flow had those sections stripped in the browser; AIO files predate them),
   one row per send with the identical-record count (``chat_head ×3``); the
   recount option rebuilds already-stored share rows the same way;
4. every ``(source_platform, raw_file)`` group re-folded with
   :func:`fyp.ingest.base.derive_play_duration`, so the play rows'
   ``extra_data`` / ``link_method`` tokens say ``save`` and ``share`` where
   they used to say ``fave`` or nothing;
5. every row re-stamped with the active activity-contract version.

Pure over the frame it is given; raw reads go through an injectable loader
so the unit test needs no storage. Idempotent: a second run reports zeros.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from types import SimpleNamespace

import pandas as pd

from fyp import activity_versioning as _activity_versioning
from fyp.ingest.base import ForYouBaseCollection, assign_session_ids, derive_play_duration
from fyp.ingest.tiktok import TikTokDDPCollection
from fyp.logging_setup import get_logger
from fyp.utils import share_method_with_count

logger = get_logger(__name__)

# data_source -> raw storage location for TikTok exports.
_TIKTOK_RAW_LOCATIONS = {"ddp": "ddp_raw", "aio": "aio_raw"}
_TIKTOK_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_NEW_SHARE_SECTIONS = {"sharehistorylist", "repostlist"}


def default_raw_loader(data_source: str, raw_file: str):
    """Read one TikTok raw export (None when absent from every raw location).

    The stored ``data_source`` does not say where the file sits: AIO-fetched
    exports are persisted with ``data_source="ddp"`` (the AIO class inherits
    the DDP parser and the stored value followed it), so the lookup tries the
    location the source names first and then every other TikTok raw folder.
    """
    import fyp.data_io as data_io

    first = _TIKTOK_RAW_LOCATIONS.get(str(data_source))
    locations = ([first] if first else []) + [loc for loc in _TIKTOK_RAW_LOCATIONS.values() if loc != first]
    for location in locations:
        try:
            if data_io.exists(storage_location=location, filename=raw_file):
                return data_io.load_json(storage_location=location, filename=raw_file)
        except Exception as exc:  # unreadable object: report, never abort the run
            logger.warning(f"[{raw_file}] raw export unreadable in '{location}': {exc}")
            return None
    return None


def _section_records(donation_dict: dict, sections: set[str]) -> pd.DataFrame:
    """``(section, item_id, utc_timestamp, context)`` for every record of the named sections.

    Reads the export exactly as ``TikTokDDPCollection`` does (same walk, same
    key-name link lookup, same UTC parse of the ``Date`` string), so a stored
    row and its raw record meet on identical values.
    """
    rows = []
    for rec in TikTokDDPCollection._walk_sections(donation_dict):
        if rec["activity_type"] not in sections:
            continue
        variables, values = rec["variable_list"], rec["value_list"]
        if not variables or variables[0] != "date":
            continue
        _, _, link, context = TikTokDDPCollection._unpack_record(variables, values)
        match = TikTokDDPCollection._VIDEO_ID_RE.search(link) if isinstance(link, str) else None
        rows.append({
            "section": rec["activity_type"],
            "item_id": match.group(1) if match else None,
            "utc_timestamp": pd.to_datetime(values[0], format=_TIKTOK_DATE_FORMAT, errors="coerce", utc=True),
            "context": (context or "").lower() or None,
            "_record": "\x1f".join(map(str, values)),
        })
    if not rows:
        return pd.DataFrame(columns=["section", "item_id", "utc_timestamp", "context", "copies"])
    out = pd.DataFrame(rows)
    out = out[out["utc_timestamp"].notna()]
    # Byte-identical share records are one send to several friends: one row
    # per send with the record count, exactly as the ingest parser does
    # (TikTokDDPCollection._collapse_identical_shares).
    key = out["section"] + "\x1f" + out["_record"]
    out["copies"] = key.map(key.value_counts()).where(out["section"].isin(_NEW_SHARE_SECTIONS), 1)
    out = out[~(key.duplicated(keep="first") & out["section"].isin(_NEW_SHARE_SECTIONS))]
    return out.drop(columns="_record")


def _utc_ns(series: pd.Series) -> pd.Series:
    """Both sides of a timestamp comparison as tz-aware ``datetime64[ns, UTC]``."""
    return pd.to_datetime(series, utc=True).astype("datetime64[ns, UTC]")


def _string_col(df: pd.DataFrame, col: str) -> None:
    """Make ``col`` a ``string[pyarrow]`` column (an all-NA column may carry the null type)."""
    if col not in df.columns:
        df[col] = pd.Series(pd.NA, index=df.index, dtype="string[pyarrow]")
    else:
        df[col] = df[col].astype("string[pyarrow]")


def rename_following(df: pd.DataFrame) -> int:
    """``following`` → ``follow``; returns the number of rows renamed."""
    mask = df["activity_type"] == "following"
    n = int(mask.sum())
    if n:
        df.loc[mask, "activity_type"] = "follow"
    return n


def retag_tiktok_bookmarks(df: pd.DataFrame, load_raw: Callable, log: Callable = print) -> dict:
    """``fave`` rows that were TikTok bookmarks → ``save``; per-file report."""
    report: dict = {"retagged": 0, "kept_fave_ambiguous": 0, "files": {}, "missing_raw": []}
    is_tiktok = (df["source_platform"] == "tiktok") & df["data_source"].isin(list(_TIKTOK_RAW_LOCATIONS))
    candidates = df[is_tiktok & (df["activity_type"] == "fave") & df["item_id"].notna()]
    for (raw_file, data_source), grp in candidates.groupby(["raw_file", "data_source"]):
        donation = load_raw(str(data_source), str(raw_file))
        if not isinstance(donation, dict):
            report["missing_raw"].append(str(raw_file))
            continue
        records = _section_records(donation, {"favoritevideolist", "itemfavoritelist"})
        bookmarks = records[(records["section"] == "favoritevideolist") & records["item_id"].notna()]
        if bookmarks.empty:
            continue
        likes = records[(records["section"] == "itemfavoritelist") & records["item_id"].notna()]
        bookmark_keys = set(zip(bookmarks["item_id"], _utc_ns(bookmarks["utc_timestamp"])))
        like_keys = set(zip(likes["item_id"], _utc_ns(likes["utc_timestamp"])))
        # Liked AND bookmarked in the same second: the stored row could be
        # either; leave it a like rather than guess.
        ambiguous = bookmark_keys & like_keys
        keys = pd.Series(list(zip(grp["item_id"].astype(str), _utc_ns(grp["utc_timestamp"]))), index=grp.index)
        hit = keys.map(lambda k: k in bookmark_keys and k not in ambiguous)
        n_hit = int(hit.sum())
        n_amb = int(keys.map(lambda k: k in ambiguous).sum())
        if n_hit:
            df.loc[hit[hit].index, "activity_type"] = "save"
        report["retagged"] += n_hit
        report["kept_fave_ambiguous"] += n_amb
        report["files"][str(raw_file)] = {"bookmarks_in_raw": int(len(bookmarks)), "retagged": n_hit, "ambiguous": n_amb}
        log(f"  [{raw_file}] {len(bookmarks):,} bookmarks in raw → {n_hit:,} fave rows retagged to save"
            + (f", {n_amb} ambiguous kept as fave" if n_amb else ""))
    return report


def append_tiktok_shares(df: pd.DataFrame, load_raw: Callable, log: Callable = print, *,
                         replace_existing: bool = False) -> tuple[pd.DataFrame, dict]:
    """Append ``share`` rows for stored TikTok files whose raw export still holds them.

    Copies ``collection_id`` / ``source_platform`` / ``data_source`` /
    ``tz_offset`` / ``raw_file`` from a sibling row of the same file, derives
    the local-time features, and returns the enlarged frame; session ids are
    reassigned by the caller once every append is in.

    ``replace_existing`` rebuilds a file's share rows from its raw export
    instead of only adding missing ones: the stored rows are dropped and every
    send is appended again, one row per send with its record count
    (``chat_head ×3``). That is the recount of 2026-09-23 — the first append
    stored identical records as separate rows, which the next ingest's dedupe
    collapsed, losing the count and the mixed-method shares in one second.
    A file whose raw export is missing or has no share records keeps its rows.
    """
    report: dict = {"appended": 0, "files": {}, "missing_raw": []}
    if replace_existing:
        report["replaced"] = 0
    drop_index: list = []
    is_tiktok = (df["source_platform"] == "tiktok") & df["data_source"].isin(list(_TIKTOK_RAW_LOCATIONS))
    new_frames = []
    for (raw_file, data_source), grp in df[is_tiktok].groupby(["raw_file", "data_source"]):
        donation = load_raw(str(data_source), str(raw_file))
        if not isinstance(donation, dict):
            report["missing_raw"].append(str(raw_file))
            continue
        records = _section_records(donation, _NEW_SHARE_SECTIONS)
        if records.empty:
            continue
        records = records.copy()
        records["extra_data"] = [
            share_method_with_count("repost" if sec == "repostlist" else ctx, n)
            for sec, ctx, n in zip(records["section"], records["context"], records["copies"])
        ]
        existing = grp[grp["activity_type"] == "share"]
        if replace_existing and not existing.empty:
            drop_index.extend(existing.index.tolist())
            report["replaced"] += int(len(existing))
        elif not existing.empty:
            have = set(zip(existing["item_id"].astype("string").fillna(""), _utc_ns(existing["utc_timestamp"])))
            keys = list(zip(records["item_id"].fillna("").astype(str), _utc_ns(records["utc_timestamp"])))
            records = records[[k not in have for k in keys]]
        if records.empty:
            continue
        sibling = grp.iloc[0]
        part = pd.DataFrame({
            "item_id": records["item_id"].astype("string[pyarrow]").values,
            "activity_type": pd.array(["share"] * len(records), dtype="string[pyarrow]"),
            "utc_timestamp": _utc_ns(records["utc_timestamp"]).values,
            "extra_data": records["extra_data"].astype("string[pyarrow]").values,
        })
        for col in ("collection_id", "source_platform", "data_source", "raw_file", "tz_offset", "ts_added_to_dataset"):
            if col in df.columns:
                part[col] = sibling[col]
        new_frames.append(part)
        report["appended"] += int(len(part))
        report["files"][str(raw_file)] = int(len(part))
        sends_with_copies = int((records["copies"] > 1).sum())
        replaced = f", replacing {len(existing):,} stored" if replace_existing and not existing.empty else ""
        log(f"  [{raw_file}] {len(part):,} share rows appended{replaced}"
            + (f" ({sends_with_copies:,} sends carry a record count)" if sends_with_copies else ""))
    if drop_index:
        df = df.drop(index=drop_index)
    if not new_frames:
        return df, report
    added = pd.concat(new_frames, ignore_index=True)
    added["utc_timestamp"] = added["utc_timestamp"].astype(df["utc_timestamp"].dtype)
    if "tz_offset" in added.columns:
        shim = SimpleNamespace(data=added)
        ForYouBaseCollection.add_local_time_features(shim)
        added = shim.data
    out = pd.concat([df, added], ignore_index=True)
    return out, report


def refold_all(df: pd.DataFrame, log: Callable = print) -> int:
    """Re-run the engagement fold per ``(source_platform, raw_file)``; returns groups refolded.

    Play rows have their fold tokens and fold ``link_method`` cleared first;
    every other row keeps its own ``extra_data`` (comment text, share method,
    followed username, Zeeschuimer's zone name on ``observe`` rows) and a
    parser-written ``link_method`` (``ffill_180s``).
    """
    for col in ("extra_data", "link_method"):
        _string_col(df, col)
    if "play_duration" not in df.columns:
        df["play_duration"] = pd.Series(pd.NA, index=df.index, dtype="int64[pyarrow]")
    is_play = df["activity_type"] == "play"
    df.loc[is_play, "extra_data"] = pd.NA
    df.loc[is_play, "link_method"] = pd.NA
    n_groups = 0
    for _, file_grp in df.groupby(["source_platform", "raw_file"], dropna=False):
        ordered = file_grp.sort_values("utc_timestamp", kind="mergesort")
        recomputed = derive_play_duration(ordered)
        for col in ("play_duration", "extra_data", "link_method"):
            df.loc[ordered.index, col] = recomputed[col].set_axis(ordered.index)
        n_groups += 1
    for col in ("extra_data", "link_method"):
        _string_col(df, col)
    df["play_duration"] = df["play_duration"].astype("int64[pyarrow]")
    log(f"  re-folded {n_groups:,} raw-file groups")
    return n_groups


def migrate(df: pd.DataFrame, load_raw: Callable = default_raw_loader, *,
            append_new_sections: bool = False, recount_shares: bool = False,
            log: Callable = print) -> tuple[pd.DataFrame, dict]:
    """Run every step over ``df`` and return ``(migrated frame, report)``.

    ``df`` is not modified; the returned frame is a rewritten copy sorted the
    way ``save_processed`` expects (by collection then time).
    """
    df = df.copy()
    report: dict = {"rows_in": int(len(df))}
    report["before"] = {str(k): int(v) for k, v in df["activity_type"].value_counts().items()}

    log("1. following → follow")
    report["renamed_following"] = rename_following(df)
    log(f"  {report['renamed_following']:,} rows renamed")

    log("2. TikTok bookmarks stored as fave → save")
    report["retag"] = retag_tiktok_bookmarks(df, load_raw, log)

    if append_new_sections or recount_shares:
        log("3. " + ("rebuild share rows from raw exports (one row per send, with its record count)"
                     if recount_shares else "append share rows still present in raw exports"))
        df, report["append"] = append_tiktok_shares(df, load_raw, log, replace_existing=recount_shares)
        if (report["append"]["appended"] or report["append"].get("replaced")) and "collection_id" in df.columns:
            df = df.sort_values(["collection_id", "utc_timestamp"], kind="mergesort").reset_index(drop=True)
            df = assign_session_ids(df)
    else:
        report["append"] = {"skipped": True}

    log("4. re-fold engagement onto play rows")
    report["refolded_groups"] = refold_all(df, log)

    log("5. stamp the active activity-contract version")
    df = _activity_versioning.stamp_version(df)

    report["rows_out"] = int(len(df))
    report["after"] = {str(k): int(v) for k, v in df["activity_type"].value_counts().items()}
    fold_tokens = df.loc[df["activity_type"] == "play", "extra_data"].dropna().astype(str)
    token_counts: dict[str, int] = {}
    for cell in fold_tokens:
        for part in cell.split(","):
            t = part.split(":", 1)[0].strip()
            token_counts[t] = token_counts.get(t, 0) + 1
    report["fold_tokens_after"] = token_counts
    return df, report


_SAFE_STAMP = re.compile(r"[^0-9T]")


def snapshot_name(now: pd.Timestamp | None = None) -> str:
    """Archive filename for the pre-migration parquet."""
    stamp = _SAFE_STAMP.sub("", (now or pd.Timestamp.utcnow()).strftime("%Y%m%dT%H%M%S"))
    return f"collections_recoded.pre_engagement_vocabulary_{stamp}.parquet"
