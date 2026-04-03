#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import re
import pandas as pd
from collections import deque
import numpy as np
import datetime as _dt

from fyp.recode_variables import infer_timezone_offset
from fyp.donations import generate_donation_metadata
from fyp.types import convert_dtypes_to_pyarrow 
from fyp.utils import clean_url
from zoneinfo import ZoneInfo
import fyp.data_io as data_io

from typing import Literal, Type
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










class ForYouBaseCollection(ABC):

    platform_url_template: str | None = None

    REQUIRED_COLUMNS = {
        "collection_id": "string[pyarrow]",
        "raw_file": "string[pyarrow]",
        "source_platform": "string[pyarrow]",
        "data_source": "string[pyarrow]",
        "activity_type": "string[pyarrow]",
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
        self.additional_columns = {}
        self.raw_path = None
        self.processed_storage_location = "recoded"
        self.min_required_rows_per_raw_file = 10
        self.discarded_raw_files = []
        self.source_platform = None
        self.data_source = None
        self.collections = []


    def clear(self):
        self.data = pd.DataFrame()
        self.state = "empty"



    def load_processed(
        self, 
        processed_fn: str, 
        drop_similar_activity_sequences: bool = True):
        
        if self.verbose:
            print(f"Loading processed data from {processed_fn}. Data source: {self.source_platform}_{self.data_source}")

        new_processed_data = data_io.load_parquet(
            storage_location=self.processed_storage_location,
            filename=processed_fn,
            verbose=False#self.verbose
        )

        if len(self.data) > 0:
            if self.state != "processed":
                if self.verbose:
                    print(f"Warning: There is data in this collection but the state is '{self.state}'. Existing data must be processed. Cannot load new data.")
                return
            if self.verbose:
                print(f"Adding {len( new_processed_data):,} new processed activities to existing {len(self.data):,} activities.")
            self.data = pd.concat([self.data, new_processed_data], ignore_index=True)
        else:
            if self.verbose:
                print(f"Loading {len(new_processed_data):,} processed activities.")
            self.data = new_processed_data.copy()

        self.state = "processed"

        if drop_similar_activity_sequences:
            if self.verbose:
                print("Dropping activities from files with overlapping/similar activity sequences")
            self.identify_similar_file_content(drop_them=True)

        if self.verbose:
            print(f"There are now {len(self.data):,} activities in the collection.")




    def save_processed(self):

        if self.state != "processed":
            print(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot save this data. Please process data first.")
            return
        
        fn = f"{self.source_platform}_{self.data_source}_processed_activities.parquet"

        if len(self.data) > 0:
            local_time_cols = [c for c in self.data.columns if c.startswith("local_")]
            if len(local_time_cols) > 0:
                print("This dataset seem to have 'local time features' added. I am dropping these columns when saving.")
                self.data.drop(local_time_cols, axis=1, inplace=True)
                
            _ = data_io.save_parquet(
                df=self.data,
                storage_location="recoded",
                filename=fn)

        for collection in self.collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))


        data_io.save_json(
            data=self.discarded_raw_files,
            storage_location=self.processed_storage_location,
            filename=f"discarded_raw_files.json",
            verbose=False#self.verbose
        )






    def load_raw(self, skip_these_raw_files: list[str] = []):
        if self.verbose:
            print(f"Loading raw data for collection '{self.source_platform}_{self.data_source}'.")

        if self.state != "empty":
            print(f"This collection '{self.source_platform}_{self.data_source}' is not empty. The current data will be replaced.")

        if self.raw_path is None:
            raise ValueError("No raw path has been set for this collection.")



        all_the_files = [fn for fn in data_io.listdir(self.raw_path) if not fn.startswith(".")]

        #if os.path.isdir(self.raw_path):
        #    all_the_files = [os.path.join(self.raw_path, f) for f in os.listdir(self.raw_path) if not f.startswith(".")]
        #else:
        #    all_the_files = [self.raw_path]

        #all_the_files = [f for f in all_the_files if os.path.basename(f) not in skip_these_raw_files+self.discarded_raw_files]

        all_the_files = [fn for fn in all_the_files if fn not in skip_these_raw_files+self.discarded_raw_files]




        many_dfs = []
        # load all files in the directory
        for fn in all_the_files:
            one_df = pd.DataFrame()
            if True:#try:

                one_df = self.load_single_raw(fn)

                if len(one_df) > 0:
                    mtime = data_io.getmtime(storage_location=self.raw_path, filename = fn)
                    one_df["ts_added_to_dataset"] = pd.to_datetime(mtime, unit="s")
                    one_df["raw_file"] = fn

                    if self.verbose: print(f"Loaded file: {fn}. Number of rows: {len(one_df):,}")
            if False:#except Exception as e:
                if self.verbose: print(f"Cannot load file: {fn}")

            # I will keep data from this file if there are at least 10 activities. (just an arbitrary number)
            if len(one_df) >= self.min_required_rows_per_raw_file:
                many_dfs.append(one_df)
            else:
                if self.verbose: print(f"Discarding file: {fn}. Too few rows: {len(one_df):,}")
                self.discarded_raw_files.append(fn)
            

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

        if self.state == "empty":
            if self.verbose:
                print(f"Collection '{self.source_platform}_{self.data_source}' is empty. Nothing for me to do.")
            return


        if self.state != "raw":
            if self.verbose:
                print(f"Collection '{self.source_platform}_{self.data_source}' is not in raw state. Cannot process. Please load raw data first.")
            return

        if self.verbose:
            print(f"Processing {len(self.data):,} rows for collection '{self.source_platform}_{self.data_source}'...")

        self.data = self.data.groupby("raw_file", group_keys=False)[self.data.columns].apply(self.process_single)

        good_columns = list((set(self.additional_columns.keys()) | set(list(self.REQUIRED_COLUMNS.keys()))) & set(self.data.columns))
        
        self.data = self.data[good_columns].copy()
        self._standardize()
        self.state = "processed"

        if self.verbose:
            print(f"Collection '{self.source_platform}_{self.data_source}' is now processed. Number of rows: {len(self.data):,}")        


    @abstractmethod
    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Subclasses must implement this logic."""
        pass






    def identify_similar_file_content(
        self, 
        overlap_threshold: float = 0.2,
        group_identifier: str = "raw_file",
        timestamp_column: str = "utc_timestamp",
        drop_them: bool = True
        ) -> dict[str, set]:
        """
        Identify similar activity collections based on timestamp overlap.

        check for similarities in the activity collections by looking for the same timestamps based on some kind of grouping variable. 
        The assumption is that if two activity collections have a lot of the same timestamps, they are likely to be duplicates

        Parameters
        ----------
        overlap_threshold : float, default 0.2
            The threshold for timestamp overlap ratio to consider activity collections as similar.
        group_identifier : str, default "raw_file"
            The column to group the data by.
        timestamp_column : str, default "utc_timestamp"
            The column containing the timestamps.
        drop_them : bool, default False
            Whether to drop the activity collections that are similar.

        Returns
        -------
        dict
            A dictionary containing identifiers of activity collections:
            - "drops": identifiers of activity collections to be dropped.
            - "keepers": identifiers of activity collections to keep.
        """

        if self.state != "processed":
            print(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot identify similar file content. Please process data first.")
            return {"drops": set(), "keepers": set()}



        raw_files_1 = set(self.data["raw_file"].values)

        # starting off with basic deduplication.
        self.data = self.data.drop_duplicates(subset=["item_id","utc_timestamp","activity_type","tz_offset"]).copy()

        raw_files_2 = set(self.data["raw_file"].values)

        dropped_raw_files = raw_files_1 - raw_files_2
        self.discarded_raw_files.extend(dropped_raw_files)

        if self.verbose and len(dropped_raw_files) > 0:
            print(f"Dropped {len(dropped_raw_files)} raw files from the dataset.")



        # dropping df cols and changing timestamp column to integers which makes set operations faster
        fine_activities_df = self.data[[group_identifier,timestamp_column]].copy()
        fine_activities_df[timestamp_column] = fine_activities_df[timestamp_column].astype('int64') / 1e9


        # the logic is based on comparing sets of timestamps, assuming that it is unlikely that two activity collections have
        # the same set of timestamps
        ts_sets = fine_activities_df.groupby(group_identifier, observed=False)[timestamp_column].apply(set).to_dict()
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

        self.discarded_raw_files += list(drops)

        if drop_them:
            if self.verbose and len(drops) > 0:
                print(f"Dropping {len(drops)} activity collections from the dataset.")
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
             df = convert_dtypes_to_pyarrow(df, verbose=False)
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
        self.source_platform = "all"
        self.data_source = "foryou"
        self.collections = []
        if data_io.exists(storage_location=self.processed_storage_location, filename="discarded_raw_files.json"):
            self.discarded_raw_files = data_io.load_json(
                storage_location=self.processed_storage_location,
                filename=f"discarded_raw_files.json",
                verbose=False#self.verbose
            )
        else:
            self.discarded_raw_files = []


    def load_single_raw(self, fn: str) -> pd.DataFrame:
        raise ValueError("Don't use this class to load raw data")
    
    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        raise ValueError("Don't use this class to process raw data")



    def register_collection_class(self, collection_class: Type[ForYouBaseCollection]):
        if not issubclass(collection_class, ForYouBaseCollection):
            raise ValueError(f"{collection_class} is not a subclass of ForYouBaseCollection")
        if collection_class in [type(x) for x in self.collections]:
            if self.verbose:
                print(f"{collection_class} is already registered.")
            return
        self.collections.append(collection_class(verbose=self.verbose))
        self.collections[-1].discarded_raw_files = self.discarded_raw_files
        if self.verbose:
            print(f"Registered collection class: {collection_class}")



    def load_processed(self):

        processed_activity_files = [fn for fn in data_io.listdir(storage_location=self.processed_storage_location) if fn.endswith("_processed_activities.parquet")]

        if len(processed_activity_files) == 0:
            if self.verbose:
                print("No processed activity files found.")
            return

        concatation_required = len(processed_activity_files)>1 or len(self.data)>0

        for i,fn in enumerate(processed_activity_files):
            if len(processed_activity_files)>1:
                if self.verbose:
                    print(i, end=": ")
            ForYouBaseCollection.load_processed(
                self, 
                processed_fn=fn, 
                drop_similar_activity_sequences=(concatation_required and i>=len(processed_activity_files)-1) # drop similar on the last one only
            )




    def process(self):
        if len(self.collections) == 0:
            print(f"This ForYouCollection does not have any sub collections. You need to register a collection class first.")
            return
        if self.verbose:
            print("Processing the registered sub collections...")

        for collection in self.collections:
            collection.process()

        if self.verbose:
            print("Done processing the registered sub collections.")




    def load_raw(self):
        if len(self.collections) == 0:
            if self.verbose:
                print(f"This ForYouCollection does not have any sub collections. You need to register a collection class first.")
            return
        if self.verbose:
            print("Loading new raw data for the registered sub collections...")

        for collection in self.collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))

        if len(self.data) > 0:
            skip_these_raw_files = self.data['raw_file'].unique().tolist() + self.discarded_raw_files
            if self.verbose:
                print(f"Skipping {len(skip_these_raw_files):,} raw files that are already discarded or already in the collection.")
        else:
            skip_these_raw_files = self.discarded_raw_files

        for collection in self.collections:
            collection.load_raw(skip_these_raw_files=skip_these_raw_files)
        
        if self.verbose:
            print(f"Done loading raw {sum([len(collection.data) for collection in self.collections]):,} rows for the registered sub collections.")




    def migrate_sub_collections(self):

        processed_collections = [collection for collection in self.collections if collection.state == "processed"]

        if len(processed_collections) == 0:
            if self.verbose:
                print(f"No processed sub collections to migrate. Nothing for me to do.")
            return

        if self.verbose:
            print(f"Migrating {len(processed_collections):,} processed sub collections to the top...")
            print(f"There are {len(self.data):,} rows in the top collection already.")

        if len(self.data) > 0:
            self.data = pd.concat([self.data]+[collection.data for collection in processed_collections], ignore_index=True)
        else:
            self.data = pd.concat([collection.data for collection in processed_collections], ignore_index=True)

        self.state = "processed"
        self.identify_similar_file_content(drop_them=True)

        for collection in processed_collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))


        for collection in processed_collections:
            print(f"Migrated {len(collection.data):,} activities from '{collection.source_platform}_{collection.data_source}'.")
            collection.data = pd.DataFrame()
            collection.state = "empty"

        if self.verbose:
            print(f"Done migrating the sub collections. There are now {len(self.data):,} activities in the top collection. Sub collections are empty.")





    def convert_to_old_format(self):

        mask = (self.data.data_source=="zeeschuimer") & (self.data.collection_id.map(lambda x:x.startswith("SYD_")))
        self.data.loc[mask,"collection_id"] = "BASELINE_2024"
        mask = (self.data.data_source=="zeeschuimer") & (self.data.collection_id.map(lambda x:x.startswith("BNE_")))
        self.data.loc[mask,"collection_id"] = "BASELINE_2024"
        mask = (self.data.data_source=="zeeschuimer") & (self.data.collection_id.map(lambda x:x != "BASELINE_2024"))
        self.data.loc[mask,"collection_id"] = "Zee_generic"

        #{u:u for u in combined_activities_df.columns}
            
        self.data_old_format = self.datacopy()
        #rename(columns={
        #        #'ts_added_to_dataset': 'ts_added_to_dataset',
        #        'utc_timestamp': 'T_utc_timestamp',
        #        'source_platform': 'source_platform',
        #        'tz_offset': 'T_tz_offset',
        #        #'raw_file': 'raw_file',
        #        'activity_type': 'D_feature_name',
        #        #'item_id': 'item_id',
        #        'data_source': 'data_source',
        #        'collection_id': 'collection_id',
        #        'local_timestamp': 'T_local_timestamp',
        #        'local_weekday': 'T_local_weekday',
        #        'local_week': 'T_local_week',
        #        'local_day_segment': 'T_local_day_segment',
        #        'local_date': 'T_local_date',
        #        "play_duration": "D_watch_duration",
        #        "extra_data":"D_primary_value"
        #    }, inplace=False).copy()
        
        #self.data_old_format.drop(["source_platform","raw_file","data_source"], axis=1, inplace=True)
        
        _ = data_io.save_parquet(
            df=self.data_old_format,
            storage_location="recoded",
            filename="donations_recoded.parquet",
            asyncronous=False)


        self.stats = generate_donation_metadata(
            self.data_old_format, 
            update_col = None, 
            sort_by=None, 
            verbose=True, 
            save_to_disk_ok=True,
            load_from_disk=False)
        self.stats[('other','accepted')] = True
        self.stats[('participants', 'date')] = self.stats[('other', 'ts_added_to_dataset')]

        _ = data_io.save_parquet(
            df=self.stats,
            storage_location="recoded",
            filename="ddp_metadata.parquet",
            asyncronous=False)



    def refresh_collection(self):
        self.load_processed()
        self.load_raw()
        self.process()
        self.migrate_sub_collections()
        self.save_processed()




class TikTokDDPCollection(ForYouBaseCollection):

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"

    def __init__(self, collection_id: str = None, verbose: bool = False):
        # In addition to the required activity variables, this ingester adds one extra variable:
        # play_duration [int64[pyarrow]] - the duration of the play in seconds
        # 
        # The extra_data column is used for the comment string, the account name that was just followed, etc...
        super().__init__(collection_id, verbose)
        self.source_platform = "tiktok"
        self.data_source = "ddp"
        self.raw_path = "ddp_raw" #"/Users/<user>/fyp_local/activity_data/ddp/ddp_raw"
        self.min_required_rows_per_raw_file = 10

        self.additional_columns = {
            "play_duration": "int64[pyarrow]"
        }




    def load_single_raw(self, filename: str) -> pd.DataFrame:

        donation_dict = data_io.load_json(storage_location = self.raw_path, filename = filename)

        #with open(filename, "r") as f:
        #    donation_dict = json.load(f)

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
        if len(donation_items) > 0:
            df = pd.DataFrame.from_records(donation_items)

        # a data donation package without at least a few play activities is not useful       
        # play activities are referred to as 'videolist' by TikTok 
        n_play_activities = len(df[df['activity_type'] == 'videolist'])
        if n_play_activities <= 10:
            if self.verbose: print(f"Discarding {filename} as it only has {n_play_activities} play activities.")
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

        mask_activity_type = df['activity_type'].map(lambda x:not ("chat history with" in x))
        df = df[mask_activity_type].copy()

        # get the date from index zero (I don't need the variable name)
        df['date'] = pd.to_datetime(df['value_list'].str[0], format='%Y-%m-%d %H:%M:%S', errors='coerce')

        # remove rows with invalid dates
        df = df[df['date'].notna()].copy()

        if self.verbose:
            print(f"   [{df["raw_file"].iloc[0]}] Keeping {len(df):,} rows w OK timestamp.")


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
        # the same session. Only comments are backfilled — other activity types retain their
        # original item_id (or null).

        # 1. calculate time between activities (in seconds)
        df['delta'] = df['utc_timestamp'] - df['utc_timestamp'].shift(1)
        df['delta'] = df['delta'].dt.total_seconds()

        # 2. use the time delta to establish groups of activities that are very close to each other
        # and which I can assume were part of the same TikTok session. I set the limit to 180
        # seconds - this is arbitrary, but it is a reasonable amount of max time to
        # spend on a video and potentially engage with it, making a comment for instance
        df['session_break'] = (df['delta'].isna()) | (df['delta'] > 180)
        df['session_id'] = df['session_break'].astype(bool).cumsum()

        # 3. Forward-fill item_id within each session, then apply only to comment rows that
        # are missing an item_id. All other activity types keep their original value.
        ffilled_item_id = df.groupby('session_id')['item_id'].ffill()
        comment_missing = (df['activity_type'] == 'comment') & df['item_id'].isna()
        df.loc[comment_missing, 'item_id'] = ffilled_item_id[comment_missing]

        df.drop(columns=['session_break', 'session_id'], inplace=True)


        # -----------------------------------------------------
        # play_duration: assigned only to 'play' activities, derived from delta (time elapsed
        # since the previous activity). Delta serves as a proxy for how long the user spent
        # on the video before the next recorded event.
        #
        # When a play activity is directly preceded by other activities sharing the same
        # item_id (e.g. a comment on the same video), those deltas represent time spent on
        # the same item and should be attributed to the first play in the run. All other
        # rows in such a run get play_duration = NA. Non-play activities always get NA.
        # Durations > 600 seconds are capped to NA (10 min max assumed per video).

        # 4. Precompute forward_delta once on the full dataframe: for each row, the time
        # until the *next* event. This is the correct attribution of dwell time to an activity.
        forward_delta = df['delta'].shift(-1)

        # Default assignment: play activities get forward_delta, everything else gets NA.
        df['play_duration'] = forward_delta.where(df['activity_type'] == 'play')

        # 5. Detect consecutive same-item_id runs of length > 1. A row is a non-first member
        # of a run when its item_id equals the previous row's item_id (and item_id is not null).
        # Such runs are vanishingly rare (~1/10,000 activities are non-play), so we iterate.
        is_continuation = df['item_id'].notna() & (df['item_id'] == df['item_id'].shift(1))

        if is_continuation.any():
            # Walk each continuation backward to find the full run, then aggregate.
            continuation_idxs = df.index[is_continuation].tolist()
            visited: set[int] = set()
            for idx in continuation_idxs:
                if idx in visited:
                    continue
                # Find the start of this run by walking back
                run_item = df.at[idx, 'item_id']
                run_start = idx
                while run_start - 1 in df.index and pd.notna(df.at[run_start - 1, 'item_id']) and df.at[run_start - 1, 'item_id'] == run_item:
                    run_start -= 1
                # Find the end of the run by walking forward
                run_end = idx
                while run_end + 1 in df.index and pd.notna(df.at[run_end + 1, 'item_id']) and df.at[run_end + 1, 'item_id'] == run_item:
                    run_end += 1
                run_slice = list(range(run_start, run_end + 1))
                visited.update(run_slice)

                # Find the first play activity in the run
                play_rows = [i for i in run_slice if df.at[i, 'activity_type'] is not pd.NA and df.at[i, 'activity_type'] == 'play']
                if not play_rows:
                    df.loc[run_slice, 'play_duration'] = pd.NA
                    continue

                # Sum forward_delta across all rows in the run using the full-df precomputed
                # series, so the last row's contribution (gap to the row after the run) is
                # correctly included — slicing before shifting would lose it.
                first_play = play_rows[0]
                total_delta = forward_delta.loc[run_slice].sum()
                df.loc[run_slice, 'play_duration'] = pd.NA
                df.at[first_play, 'play_duration'] = total_delta

                # Record the activity types of the non-lead rows in the run on the lead play's
                # extra_data column, as a comma-separated string (e.g. "fave" or "fave,comment").
                other_parts = []
                for i in run_slice:
                    if i == first_play:
                        continue
                    atype = df.at[i, 'activity_type']
                    if atype is pd.NA:
                        continue
                    edata = df.at[i, 'extra_data']
                    if edata is not pd.NA:
                        edata_clean = re.sub(r'[\s,]+', ' ', str(edata)).strip()
                        other_parts.append(f"{atype}:{edata_clean}")
                    else:
                        other_parts.append(atype)
                if other_parts:
                    df.at[first_play, 'extra_data'] = ",".join(other_parts)

        df.drop(columns=['delta'], inplace=True)

        # 6. Cap play_duration at 600 seconds and cast to the project dtype.
        df["play_duration"] = df["play_duration"].map(
            lambda x: x if pd.notna(x) and x <= 600 else pd.NA
        ).astype("int64[pyarrow]")

        return df












class TikTokZeeschuimerCollection(ForYouBaseCollection):

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"

    def __init__(self, collection_id: str = None, verbose: bool = False):
        # The extra_data column is used for the timezone name

        super().__init__(collection_id, verbose)
        self.raw_path = "zeeschuimer_raw" #"/Users/<user>/fyp_local/activity_data/zeeschuimer/zeeschuimer_raw"
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


        # Convert the zeeschuimer timestamp to a datetime object
        df["timestamp_collected"] = df["timestamp_collected"].astype(np.int64)
        df["timestamp_collected"] = df["timestamp_collected"].apply(lambda x: _dt.datetime.fromtimestamp(np.int64(x/1000)))

        unique_tz = df["source_url.tz_name"].dropna().unique()


        # Derive UTC timestamp
        if len(unique_tz) == 1:
            #if self.verbose: print("fast extraction of local time based features")
            # Fast path: everything in same tz
            tz = ZoneInfo(unique_tz[0])
            # Localize -> Convert to UTC
            df["utc_timestamp"] = (
                df["timestamp_collected"]
                .dt.tz_localize(tz, ambiguous='NaT', nonexistent='NaT')
                .dt.tz_convert("UTC")
            )
        else:
            #if self.verbose: print("slow extraction of local time based features")
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







def get_main_collection(verbose: bool = False) -> ForYouCollection:
    """Factory function to initialize and configure the main collection."""
    main_collection = ForYouCollection(verbose=verbose)
    main_collection.register_collection_class(TikTokDDPCollection)
    main_collection.register_collection_class(TikTokZeeschuimerCollection)
    
    return main_collection
