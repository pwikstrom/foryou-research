#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import pandas as pd
import json
import os
from collections import deque
import numpy as np
import datetime as _dt

from fyp.recode_variables import infer_timezone_offset
from fyp.types import convert_dtypes_to_pyarrow 
from fyp.utils import clean_url
from zoneinfo import ZoneInfo
import fyp.data_io as data_io
#from fyp.fyp_config import fyp_cf

from typing import Literal
from abc import ABC, abstractmethod

WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}




def _day_segment_from_hour(hour: int) -> str:
    if not (0 <= hour <= 23):
        raise ValueError(f"hour must be in 0..23, got {hour}")
    if hour <= 5:
        return "night"
    if hour <= 11:
        return "morning"
    if hour <= 17:
        return "afternoon"
    return "evening"





def transform_new_activity_data_format_to_old_format():
    ddp_collections = TikTokDDPCollection()
    zeeschuimer_collections = TikTokZeeschuimerCollection()

    zeeschuimer_collections.load_raw(raw_path="/Users/<user>/fyp_local/activity_data/zeeschuimer/zeeschuimer_raw")
    ddp_collections.load_raw(raw_path="/Users/<user>/fyp_local/activity_data/ddp/ddp_raw")

    zeeschuimer_collections.process()
    ddp_collections.process()

    zeeschuimer_collections.identify_similar_file_content(drop_them=True)
    ddp_collections.identify_similar_file_content(drop_them=True)

    zeeschuimer_collections.add_local_time_features()
    ddp_collections.add_local_time_features()

    # this is a bit special
    zeeschuimer_collections.data.loc[zeeschuimer_collections.data.loc[zeeschuimer_collections.data.collection_id.map(lambda x:x.startswith("SYD_"))].index,"collection_id"] = "BASELINE_2024"
    zeeschuimer_collections.data.loc[zeeschuimer_collections.data.loc[zeeschuimer_collections.data.collection_id.map(lambda x:x.startswith("BNE_"))].index,"collection_id"] = "BASELINE_2024"
    zeeschuimer_collections.data.loc[zeeschuimer_collections.data.loc[zeeschuimer_collections.data.collection_id.map(lambda x:x != "BASELINE_2024")].index,"collection_id"] = "Zee_generic"

    combined_events_df = pd.concat([zeeschuimer_collections.data,ddp_collections.data], ignore_index=True)

    #{u:u for u in combined_events_df.columns}
        
    combined_events_df_2 = combined_events_df.rename(columns={
            'ts_added_to_dataset': 'ts_added_to_dataset',
            'utc_timestamp': 'T_utc_timestamp',
            'source_platform': 'source_platform',
            'tz_offset': 'T_tz_offset',
            'raw_file': 'raw_file',
            'event_type': 'D_feature_name',
            'item_id': 'item_id',
            'data_source': 'data_source',
            'collection_id': 'D_donation_id',
            'local_timestamp': 'T_local_timestamp',
            'local_weekday': 'T_local_weekday',
            'local_week': 'T_local_week',
            'local_day_segment': 'T_local_day_segment',
            'local_date': 'T_local_date',
            "watch_duration": "D_watch_duration",
            "extra_data":"D_primary_value"
        }, inplace=False).copy()
    
    combined_events_df_2.drop(["source_platform","raw_file","data_source"], axis=1, inplace=True)
    combined_events_df_2 = don._add_session_info_to_ddp_log(ddp_log_in=combined_events_df_2)
    
    _ = data_io.save_parquet(df=combined_events_df_2, storage_location="recoded",filename="donations_recoded.parquet", asyncronous=False)


    new_stats = don.generate_donation_metadata(
        combined_events_df_2, 
        update_col = None, 
        sort_by=None, 
        verbose=True, 
        save_to_disk_ok=True,
        load_from_disk=False)
    new_stats[('other','accepted')] = True
    new_stats[('participants', 'date')] = new_stats[('other', 'ts_added_to_dataset')]

    _ = data_io.save_parquet(df=new_stats, storage_location="ddp_main",filename="ddp_metadata.parquet", asyncronous=False)










class ForYouBaseCollection(ABC):


    REQUIRED_COLUMNS = {
        "collection_id": "string[pyarrow]",
        "raw_file": "string[pyarrow]",
        "source_platform": "string[pyarrow]",
        "data_source": "string[pyarrow]",
        "event_type": "string[pyarrow]",
        "utc_timestamp": "timestamp[ns][pyarrow]",
        "tz_offset": "int64[pyarrow]",
        "item_id": "string[pyarrow]",
        "ts_added_to_dataset": "timestamp[ns][pyarrow]",
        "extra_data": "string[pyarrow]"
    }
    



    def __init__(self, collection_id: str = None, verbose: bool = False):
        self.collection_id = collection_id
        self.verbose = verbose
        self.data = pd.DataFrame()
        self.state: Literal["empty", "raw", "processed"] = "empty"
        #self.ts_added_to_dataset = None # Should be set by subclasses
        self.additional_columns = {}


    def clear(self):
        self.data = pd.DataFrame()
        self.state = "empty"



    def load_processed(
        self, 
        processed_fn: str, 
        storage_location: str = "cache",
        drop_similar_event_sequences: bool = True
        ):
        
        new_processed_data = data_io.load_parquet(storage_location=storage_location,filename=processed_fn, verbose=self.verbose)

        if len(self.data) > 0:
            if self.state != "processed":
                print(f"Warning: There is data in this collection but the state is {self.state}. Existing data must be processed. Terminating.")
                return
            print(f"Adding {len( new_processed_data):,} new processed events to existing {len(self.data):,} events.")
            self.data = pd.concat([self.data, new_processed_data], ignore_index=True)
        else:
            print(f"Loading {len(new_processed_data):,} processed events.")
            self.data = new_processed_data.copy()

        self.state = "processed"

        if drop_similar_event_sequences:
            print("Dropping events from files with overlapping/similar event sequences")
            self.identify_similar_file_content(drop_them=True)

        print(f"There are now {len(self.data):,} events in the collection.")






    def load_raw(self, raw_path: str, min_required_rows_per_file: int = 10):

        if self.state != "empty":
            print("Note that this collection is not empty. The current data will be replaced.")


        if os.path.isdir(raw_path):
            all_the_files = [os.path.join(raw_path, f) for f in os.listdir(raw_path) if not f.startswith(".")]
        else:
            all_the_files = [raw_path]

        many_dfs = []
        # load all files in the directory
        for fn in all_the_files:
            one_df = pd.DataFrame()
            try:

                one_df = self.load_single_raw(fn)

                if len(one_df) > 0:
                    one_df["ts_added_to_dataset"] = pd.to_datetime(os.path.getmtime(fn), unit="s")
                    one_df["raw_file"] = os.path.basename(fn)

                    if self.verbose: print(f"Loaded file: {fn}. Number of rows: {len(one_df)}")
            except Exception as e:
                if self.verbose: print(f"Cannot load file: {fn}")

            # I will keep data from this file if there are at least 10 events. (just an arbitrary number)
            if len(one_df) >= min_required_rows_per_file:
                many_dfs.append(one_df)
            else:
                if self.verbose: print(f"Discarding file: {fn}. Too few rows: {len(one_df)}")
            

        if len(many_dfs) > 1:
            self.data = pd.concat(many_dfs, ignore_index=True)
            self.state = "raw"

        elif len(many_dfs) == 1:
            self.data = many_dfs[0]
            self.state = "raw"
        else:
            self.data = pd.DataFrame()
            self.state = "empty"



    @abstractmethod
    def load_single_raw(self, filename: str) -> pd.DataFrame:
        """Subclasses must implement this logic."""
        pass



    def process(self):

        if self.state != "raw":
            raise ValueError("Collection is not in raw state. Please load raw data first.")

        df = self.process_single(self.data.copy())

        good_columns = list((set(self.additional_columns.keys()) | set(list(self.REQUIRED_COLUMNS.keys()))) & set(df.columns))
        
        self.data = df[good_columns].copy()
        self._standardize()
        self.state = "processed"
        


    @abstractmethod
    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Subclasses must implement this logic."""
        pass



        

    def save(self):
        if self.state != "processed":
            raise ValueError("Collection is not in processed state. Please process data first.")
        
        collection_id_count = self.data['collection_id'].nunique()
        print(f"There are {collection_id_count} collection IDs in this dataset")
        for i,grp in self.data.groupby("collection_id"):
            print(f"Saving collection ID: {i}")
            metadata = {
                "collection_id": i,
                "source_platform": self.source_platform,
                "data_source": self.data_source,
                "ts_added_to_dataset": self.ts_added_to_dataset,
                "state": self.state
            }

            data_io.save_parquet(
                df=grp,
                storage_location = "events",
                filename=f"{i}.parquet",
                verbose=self.verbose
            )
            data_io.save_json(
                data = metadata,
                storage_location = "events",
                filename = f"{i}.json",
                verbose=self.verbose
            )
        





    def identify_similar_file_content(
        self, 
        overlap_threshold: float = 0.2,
        group_identifier: str = "raw_file",
        timestamp_column: str = "utc_timestamp",
        drop_them: bool = True
        ) -> dict[str, set]:
        """
        Identify similar event collections based on timestamp overlap.

        check for similarities in the event collections by looking for the same timestamps based on some kind of grouping variable. 
        The assumption is that if two event collections have a lot of the same timestamps, they are likely to be duplicates

        Parameters
        ----------
        overlap_threshold : float, default 0.2
            The threshold for timestamp overlap ratio to consider event collections as similar.
        group_identifier : str, default "raw_file"
            The column to group the data by.
        timestamp_column : str, default "utc_timestamp"
            The column containing the timestamps.
        drop_them : bool, default False
            Whether to drop the event collections that are similar.

        Returns
        -------
        dict
            A dictionary containing identifiers of event collections:
            - "drops": identifiers of event collections to be dropped.
            - "keepers": identifiers of event collections to keep.
        """

        if self.state != "processed":
            raise ValueError("Collection is not in processed state. Please process the collection first.")

        # starting off with basic deduplication.
        self.data = self.data.drop_duplicates(subset=["item_id","utc_timestamp","event_type","tz_offset"]).copy()

        # dropping df cols and changing timestamp column to integers which makes set operations faster
        fine_events_df = self.data[[group_identifier,timestamp_column]].copy()
        fine_events_df[timestamp_column] = fine_events_df[timestamp_column].astype('int64') / 1e9


        # the logic is based on comparing sets of timestamps, assuming that it is unlikely that two event collections have
        # the same set of timestamps
        ts_sets = fine_events_df.groupby(group_identifier, observed=False)[timestamp_column].apply(set).to_dict()
        unique_idenfifiers = list(ts_sets.keys())
        unique_idenfifiers = sorted(unique_idenfifiers, key=lambda x: len(ts_sets[x]), reverse=False)

        drops = set()

        for identifier_a in unique_idenfifiers:
            if identifier_a not in drops:
                for identifier_b in unique_idenfifiers:
                    if (identifier_a != identifier_b) and (identifier_b not in drops):
                        ts_overlap = len(ts_sets[identifier_a] & ts_sets[identifier_b]) / (min(len(ts_sets[identifier_b]), len(ts_sets[identifier_a])))   
                        if (ts_overlap > overlap_threshold):
                            if len(ts_sets[identifier_b]) > len(ts_sets[identifier_a]):
                                drops.add(identifier_a)
                            else:
                                drops.add(identifier_b)
                            break

        keepers = set(unique_idenfifiers) - drops

        if drop_them:
            if self.verbose:
                print(f"Dropping {len(drops)} event collections from the dataset.")
            self.data = self.data[self.data[group_identifier].isin(keepers)].copy()
        else:
            return {"drops": drops, "keepers": keepers}





    def add_local_time_features(self) -> None:
        df = self.data

        offset_timedelta = pd.to_timedelta(df['tz_offset'], unit='h')
        df["local_timestamp"] = df["utc_timestamp"] + offset_timedelta


        ts = df["local_timestamp"]
        
        iso = ts.dt.isocalendar()  # DataFrame: year, week, day
        iso["day"] = iso["day"].map(WEEKDAY_MAPPER)
        iso["year_week"] = iso["year"].astype(str) + "-" + iso["week"].astype(str)

        # these dtype fixes feel stupid, but I cannot seem to get around it any other way
        df["local_weekday"] = iso["day"].to_list()
        df["local_weekday"] = df["local_weekday"].convert_dtypes(dtype_backend="pyarrow")
        df["local_week"] = iso["year_week"].to_list()
        df["local_week"] = df["local_week"].convert_dtypes(dtype_backend="pyarrow")

        local_hour = ts.dt.hour.astype("uint8[pyarrow]")

        df["local_day_segment"] = local_hour.map(_day_segment_from_hour).convert_dtypes(dtype_backend="pyarrow")

        df["local_date"] = ts.dt.date.astype("date32[pyarrow]")






    def _standardize(self):
        """
        Ensures the dataframe has all required columns and correct dtypes.
        """
        df = self.data.copy()

        df['source_platform'] = self.source_platform
        df['data_source'] = self.data_source

        if "collection_id" not in df.columns:
            if self.collection_id is not None:
                df['collection_id'] = self.collection_id
            elif "raw_file" in df.columns:
                df["collection_id"] = df["raw_file"]
            else:
                df["collection_id"] = pd.NA


        # 1. Ensure all required columns exist
        for col, dtype in self.REQUIRED_COLUMNS.items():
            if col not in df.columns:
                if self.verbose:
                    print(f"Warning: Missing column {col}, filling with NA.")
                df[col] = pd.NA

        # 2. Enforce dtypes
        # We try to use the dictionary to cast, but pandas/pyarrow can be finicky with dictionary casting
        # so we iterate.
        for col, dtype in self.REQUIRED_COLUMNS.items():
            if col in df.columns:
                try:
                    df[col] = df[col].astype(dtype)
                except Exception as e:
                    if self.verbose:
                        print(f"Error casting {col} to {dtype}: {e}. Trying fyp.types.convert_dtypes_to_pyarrow.")
                    # Fallback to the robust converter
                    # converting specific column to pyarrow backed using the helper
                    # Note: convert_dtypes_to_pyarrow works on DF, but we can try to apply it to the column or the whole DF later
                    pass
        
        # Use the robust converter for the whole DF for good measure to ensure everything is pyarrow backed where possible
        # and specifically fixing complex types if any
        try:
             df = convert_dtypes_to_pyarrow(df, verbose=self.verbose)
        except Exception as e:
             if self.verbose: print(f"Warning: convert_dtypes_to_pyarrow failed: {e}")

        # -----------------------------------------------------
        # It's important to sort by time
        df.sort_values("utc_timestamp", inplace=True, kind='mergesort')
        df.reset_index(drop=True, inplace=True)

        self.data = df.copy()



class ForYouCollection(ForYouBaseCollection):
    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)

    def load_single_raw(self, fn: str) -> pd.DataFrame:
        raise ValueError("Don't use this class to load raw data")
    
    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        raise ValueError("Don't use this class to process raw data")





class TikTokDDPCollection(ForYouBaseCollection):


    def __init__(self, collection_id: str = None, verbose: bool = False):
        # In addition to the required event variables, this ingester adds one extra variable:
        # watch_duration [int64[pyarrow]] - the duration of the watch in seconds
        # 
        # The extra_data column is used for the comment string, the account name that was just followed, etc...
        super().__init__(collection_id, verbose)
        self.source_platform = "tiktok"
        self.data_source = "ddp"

        self.additional_columns = {
            "watch_duration": "int64[pyarrow]"
        }




    def load_single_raw(self, fn: str) -> pd.DataFrame:
        with open(fn, "r") as f:
            donation_dict = json.load(f)

        # find list of dicts
        donation_items = []
        
        stack = deque([(None, donation_dict)])
        while stack:
            feature, obj = stack.pop()
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict) and item:
                        donation_items.append({
                            "event_type": (feature or '').lower(),
                            "variable_list": [k.lower() for k in item.keys()],
                            "value_list": list(item.values())
                        })
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    stack.append((k, v))

        # initialising the dataframe from the raw data.
        if len(donation_items) > 0:
            one_df = pd.DataFrame.from_records(donation_items)

        # a data donation package without at least a few watch events is not useful       
        # watch events are referred to as 'videolist' by TikTok 
        n_watch_events = len(one_df[one_df['event_type'] == 'videolist'])
        if n_watch_events <= 10:
            if self.verbose: print(f"Discarding {fn} as it only has {n_watch_events} watch events.")
            return pd.DataFrame()

        return one_df






    def process_single(self, df: pd.DataFrame):

        df = df.copy()

        # -----------------------------------------------------
        # unpack the variable/value list. The two lists variable & value list contain a label (e.g. 'link')
        # and the value (e.g. 'https://www.tiktok.com/...') at the corresponding indeces. At index 0 is always the date
        # and I'm only unpacking index 1 in addition of date even though there may be additional data in the lists.

        # if 'date' is not among the variables in the list, something is wrong with this event
        # so I keep events/rows that have at least one element in the variable_list and one of these elements is 'date'
        mask_date = df['variable_list'].map(lambda lst: 'date' in lst)
        df = df[mask_date & (df['variable_list'].map(len) > 0)].copy()

        # get the date from index zero (I don't need the variable name)
        df['date'] = pd.to_datetime(df['value_list'].str[0])

        # get the variable name and the associated value from index 1 and assign them to primary_label and extra_data
        # primary_label is just a temporary holder in this function
        try:
             df['primary_label'] = df['variable_list'].str[1]
             df['extra_data'] = df['value_list'].str[1]
        except:
             df['primary_label'] = pd.NA
             df['extra_data'] = pd.NA


        # -----------------------------------------------------
        # item_id: 

        # extract item_id and clean it up (from the video_url)
        item_ids_from_url = df["extra_data"].astype("string").str.rsplit("/", n=2).str[-2]
        digits = item_ids_from_url.str.fullmatch(r"\d+")
        item_ids = item_ids_from_url.where(digits)

        # since items are extracted from the video url, the primary label must be 'link' and
        # the event type must not be null (assuming it is watch, fave or something like that)
        mask = (df["primary_label"]=="link") & (df["event_type"].notna())
        df["item_id"] = item_ids.where(mask)
        
        # nullify extra_data where item_id was extracted to get rid of redundant data - I don't 
        # need the url any longer
        df.loc[df["item_id"].notnull(), "extra_data"] = pd.NA

        # convert item_id to pyarrow string
        df["item_id"] = df["item_id"].astype("string[pyarrow]")


        # -----------------------------------------------------
        # event_type: 

        # map event types
        df["event_type"] = df["event_type"].map({
            'videolist':'watch', 'commentslist':'comment', 'post':'post',
            'searchlist':'search', 'fanslist':'followed_by', 'following':'following',
            'itemfavoritelist':'fave', 'favoritevideolist':'fave'
        })
        
        # event_type is NA for login events - this fixes that by creating a new event type
        df.loc[df[df["primary_label"]=="ip"].index,"event_type"] = "login_event"
        
        # Convert event_type to pyarrow string
        df["event_type"] = df["event_type"].astype("string[pyarrow]")

        # cleanup - remove watch events that don't have an item_id
        df = df[((df["event_type"] != "watch") | (df["item_id"].notna()))].copy()
        

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
        # It seems like the data donation packages keep watch logs for a certain time back
        # in time, but they keep other engagement stats for longer. It is difficult to handle
        # engagement stats without connection to a watch event, so I remove all events before 
        # the first watch event. It feels a bit brutal to throw away data, but I'm not sure what else to do.
        if (df["event_type"] == "watch").any():
            first_watch_idx = df[df["event_type"] == "watch"].index[0]
            df = df.loc[first_watch_idx:].copy()


        # ----------------------------------------------------------------------------------------------
        # in the code block below I am associating events without items to the item_id of the previous event (if possible) 
        # it is principally donw for comments that doesn't have an item_id in the data. So my assumption is that
        # the comment is associated with the item_id of the previous event.

        # 1. calculate time between events (in seconds)
        df['delta'] = df['utc_timestamp'] - df['utc_timestamp'].shift(1)
        df['delta'] = df['delta'].dt.total_seconds()

        # 2. use the time delta to establish groups of events that are very close to each other
        # and which I can assume were part of the same TikTok session. I set the limit to 180
        # seconds - this is arbitrary, but it feels like a reasonable amount of max time to 
        # spend on a video and potentially engage with it, making a comment for instance
        df['session_break'] = (df['delta'].isna()) | (df['delta'] > 180)
        df['session_id'] = df['session_break'].astype(bool).cumsum()

        # 3. I need to get rid of events at the beginning of sessions that doesn't have an item.
        # since I am searching for the item of the preceding event. I create a binary mask
        # indicating if a row has a valid item_id (1) or not (0)
        has_item = df['item_id'].notna().astype(int) 
        
        # 4. By cumulatively adding up the 'has_item', I get a non-zero value for all events following the
        # first event with an item in a session.
        cumulative_items = has_item.groupby(df['session_id']).cumsum()
        
        # 5. As I cannot associate the events at the beginning of sessions with an item, 
        # I might as well drop those rows.
        df = df[cumulative_items > 0].copy()

        # 6. Propagate the last valid item_id forward within each session to associate with subsequent events
        df['item_id'] = df.groupby('session_id')['item_id'].ffill()
        

        # -----------------------------------------------------
        # watch_duration is a crucial property of the watch event in ddps. I am using delta to get a value for watch_duration. 
        # I assume that if delta is larger a certain value the user is no longer watching. How long? I'm guessing
        # 10 minutes (600 seconds) even though I recognise that you can in theory remain longer on a video.
        # But doing that doesn't feel very TikTok-like
        df.rename(columns={"delta": "watch_duration"}, inplace=True)
        df["watch_duration"] = df["watch_duration"].map(lambda x: x if pd.notna(x) and x <= 600 else pd.NA).astype("int64[pyarrow]")

        return df












class TikTokZeeschuimerCollection(ForYouBaseCollection):


    def __init__(self, collection_id: str = None, verbose: bool = False):
        # The extra_data column is used for the timezone name

        super().__init__(collection_id, verbose)
        self.source_platform = "tiktok"
        self.data_source = "zeeschuimer"
        self.accepted_tiktok_urls = [
            'https://www.tiktok.com/foryou',
            'https://www.tiktok.com/',
            'https://www.tiktok.com/en',
        ]





    def load_single_raw(self, fn: str) -> pd.DataFrame:
        data = []
        with open(fn, 'r') as file:
            for line in file:
                data.append(json.loads(line))

        if len(data) > 0:
            one_df = pd.json_normalize(data)

            # Only keeping data from accepted tiktok urls
            if 'source_platform_url' in one_df.columns:
                one_df = one_df[one_df['source_platform_url'].isin(self.accepted_tiktok_urls)].copy()
        
        return one_df






    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        # zeeschuimer data is really basic - well, there is a lot of useful data in the ndjson, but to generate
        # an event collection, which is the purpose here, I am only using the item_id and the timestamp

        df = df.copy()
        
        # Extract lots of useful data from the source_url to get tz_offset etc
        source_details = []
        for ii in df.index:
            source_details += [clean_url(df['source_url'][ii])]
        source_details = pd.DataFrame(source_details)
        df = pd.merge(left=df, right=source_details, left_index=True, right_index=True)

        # -----------------------------------------------------
        # CONSIDER THIS: I call all events from zeeschuimer 'observe' to keep them separate from the 'watch' events from ddps
        df["event_type"] = "watch"


        # -----------------------------------------------------
        # item_id: 
        # Filter valid item_ids to make sure they're not corrupted
        if 'item_id' in df.columns:
             df = df[df.item_id.map(lambda x:all([u in "0123456789" for u in x]) and len(x) == 19)].copy()
        

        # -----------------------------------------------------
        # tz_offset and utc_timestamp:

        # Convert the zeeschuimer timestamp to a datetime object
        df["timestamp_collected"] = df["timestamp_collected"].astype(np.int64)
        df["timestamp_collected"] = df["timestamp_collected"].apply(lambda x: _dt.datetime.fromtimestamp(np.int64(x/1000)))

        unique_tz = df["source_url.tz_name"].dropna().unique()


        # Derive UTC timestamp
        if len(unique_tz) == 1:
            if self.verbose: print("fast extraction of local time based features")
            # Fast path: everything in same tz
            tz = ZoneInfo(unique_tz[0])
            # Localize -> Convert to UTC
            df["utc_timestamp"] = (
                df["timestamp_collected"]
                .dt.tz_localize(tz, ambiguous='NaT', nonexistent='NaT')
                .dt.tz_convert("UTC")
            )
        else:
            if self.verbose: print("slow extraction of local time based features")
            # Slower path: per-timezone blocks
            utc_parts = []
            for tz_name, block in df.groupby("source_url.tz_name", sort=False):
                tz = ZoneInfo(tz_name)
                # Localize -> Convert to UTC immediately
                part = (
                    block["timestamp_collected"]
                    .dt.tz_localize(tz, ambiguous='NaT', nonexistent='NaT')
                    .dt.tz_convert("UTC")
                )
                utc_parts.append(part)
            # Concatenate identical Dtypes (all UTC)
            df["utc_timestamp"] = pd.concat(utc_parts).sort_index()

        if len(unique_tz) > 0:
            df["tz_offset"] = df["timestamp_collected"] - df["utc_timestamp"].dt.tz_localize(None)
            df["tz_offset"] = df["tz_offset"].dt.total_seconds() / 3600
        
        # I'm keeping this information in the extra_data column. It's a string so it works fine
        df.rename(columns={"source_url.tz_name": "extra_data"}, inplace=True)

        return df


        












