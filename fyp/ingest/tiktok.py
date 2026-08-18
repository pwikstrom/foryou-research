"""TikTok collection classes: DDP, AIO, and Zeeschuimer captures.

Carved out of the flat ``fyp/ingest.py`` in the subpackage restructure; shared
helpers stay in ``fyp.ingest.base``. Imports of siblings go through the
package directly (never the old-path shims) — see the shim-poisoning rule in
docs/fyp-import-graph.md.
"""

import os
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
from fyp.recode_variables import infer_timezone_offset
from fyp.utils import clean_url

logger = get_logger(__name__)


class TikTokDDPCollection(ForYouBaseCollection):

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"
    source_platform = "tiktok"
    raw_path = "ddp_raw"

    @classmethod
    def accepted_upload_suffixes(cls) -> list[str]:
        return [".json"]

    def __init__(self, collection_id: str = None, verbose: bool = False):
        # The extra_data column is used for the comment string, the account name that was just followed, etc...
        # play_duration is a base activity-contract column (derive_play_duration), no extras needed here.
        super().__init__(collection_id, verbose)
        self.source_platform = "tiktok"
        self.data_source = "ddp"
        self.raw_path = "ddp_raw"
        self.min_required_rows_per_raw_file = 10




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

        # find list of dicts
        donation_items = []
        
        stack = deque([(None, donation_dict)])
        while stack:
            feature, obj = stack.pop()
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict) and item:
                        donation_items.append({
                            "activity_type": (feature or '').lower(),
                            "variable_list": [k.lower() for k in item.keys()],
                            "value_list": list(item.values())
                        })
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    stack.append((k, v))

        # initialising the dataframe from the raw data.
        if len(donation_items) == 0:
            return pd.DataFrame()
        df = pd.DataFrame.from_records(donation_items)

        # a data donation package without at least a few play activities is not useful
        # play activities are referred to as 'videolist' by TikTok
        n_play_activities = len(df[df['activity_type'] == 'videolist'])
        if n_play_activities <= 10:
            if self.verbose: logger.info(f"Discarding {filename} as it only has {n_play_activities} play activities.")
            return pd.DataFrame()

        return df






    def process_single(self, df: pd.DataFrame):

        df = df.copy()

        # -----------------------------------------------------
        # unpack the variable/value list. The two lists variable & value list contain a label (e.g. 'link')
        # and the value (e.g. 'https://www.tiktok.com/...') at the corresponding indeces. At index 0 is always the date
        # and I'm only unpacking index 1 in addition of date even though there may be additional data in the lists.

        # if 'date' is not the first element in the variable_list, something is wrong with this activity
        # so I keep activities/rows that have at least two elements in the variable_list and the first element is 'date'
        mask_date = df['variable_list'].map(lambda x: isinstance(x, list) and len(x) > 1 and x[0] == 'date')
        df = df[mask_date].copy()

        mask_activity_type = df['activity_type'].map(lambda x:"chat history with" not in x)
        df = df[mask_activity_type].copy()

        # get the date from index zero (I don't need the variable name)
        df['date'] = pd.to_datetime(df['value_list'].str[0], format='%Y-%m-%d %H:%M:%S', errors='coerce')

        # remove rows with invalid dates
        df = df[df['date'].notna()].copy()

        if self.verbose:
            logger.info(f"   [{df['raw_file'].iloc[0]}] Keeping {len(df):,} rows w OK timestamp.")


        # get the variable name and the associated value from index 1 and assign them to primary_label and extra_data
        # primary_label is just a temporary holder in this function
        try:
             df['primary_label'] = df['variable_list'].str[1]
             df['extra_data'] = df['value_list'].str[1]
        except Exception as e:
             logger.warning(f"Could not extract primary_label/extra_data from variable_list/value_list ({e}); filling with NA.")
             df['primary_label'] = pd.NA
             df['extra_data'] = pd.NA


        # -----------------------------------------------------
        # item_id: 

        # extract item_id and clean it up (from the video_url)
        item_ids_from_url = df["extra_data"].astype("string").str.rsplit("/", n=2).str[-2]
        digits = item_ids_from_url.str.fullmatch(r"\d+")
        item_ids = item_ids_from_url.where(digits)

        # since items are extracted from the video url, the primary label must be 'link' and
        # the activity type must not be null (assuming it is play, fave or something like that)
        mask = (df["primary_label"]=="link") & (df["activity_type"].notna())
        df["item_id"] = item_ids.where(mask)
        
        # nullify extra_data where item_id was extracted to get rid of redundant data - I don't 
        # need the url any longer
        df.loc[df["item_id"].notnull(), "extra_data"] = pd.NA

        # convert item_id to pyarrow string
        df["item_id"] = df["item_id"].astype("string[pyarrow]")


        # -----------------------------------------------------
        # activity_type: 

        # map activity types
        df["activity_type"] = df["activity_type"].map({
            'videolist':'play', 'commentslist':'comment', 'post':'post',
            'searchlist':'search', 'fanslist':'followed_by', 'following':'following',
            'itemfavoritelist':'fave', 'favoritevideolist':'fave'
        })
        reaction_activities = {"comment","fave","share"}
        
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

        # infer timezone offset note that inferring timezone assumes that this is
        # an 'actual' TikTok user using TikTok like a normal TikTok user does.
        # If this ddp was created in an 'artificial way', the inference will get it wrong 
        df["tz_offset"] = infer_timezone_offset(df["utc_timestamp"])

        # sort by timestamp and reset index
        df.sort_values("utc_timestamp", inplace=True, kind='mergesort')
        df.reset_index(drop=True, inplace=True)


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


    def load_raw(self, skip_these_raw_files: list[str] = []):
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
            super().load_raw(skip_these_raw_files=skip_these_raw_files)
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

        super().load_raw(skip_these_raw_files=skip_these_raw_files)





class TikTokZeeschuimerCollection(ForYouBaseCollection):

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"
    source_platform = "tiktok"

    raw_path = "zeeschuimer_raw"

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











class TikTokDemoCollection(TikTokDDPCollection):
    """Synthetic demonstration donations in the TikTok DDP JSON format.

    Same parser as TikTokDDPCollection, but a separate ``data_source`` so the
    demo material stays fully isolated from real DDP uploads: its own raw
    upload location (``demo_raw``), its own structure-sentinel baseline key
    (``tiktok_demo`` — the armed ``tiktok_ddp`` baseline never learns
    synthetic fingerprints), and its own collection ids. Demo files are
    produced by ``scripts/generate_demo_dataset.py``.
    """

    raw_path = "demo_raw"

    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.data_source = "demo"
        self.raw_path = "demo_raw"
