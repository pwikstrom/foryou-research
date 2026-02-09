#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import pandas as pd
import os
from collections import deque
import numpy as np
import datetime as _dt

from fyp.recode_variables import infer_timezone_offset
from fyp.types import convert_dtypes_to_pyarrow 
from fyp.utils import clean_url
from zoneinfo import ZoneInfo
import fyp.data_io as data_io










class BaseIngester:
    """
    Base class for ingestion logic. 
    Enforces a consistent output schema (columns and dtypes).
    """

    REQUIRED_COLUMNS = {
        "collection_id": "string[pyarrow]",
        "collection_group": "string[pyarrow]",
        "source_platform": "string[pyarrow]",
        "data_source": "string[pyarrow]",
        "event_type": "string[pyarrow]",
        "utc_timestamp": "timestamp[ns][pyarrow]",
        "tz_offset": "int64[pyarrow]",
        "item_id": "string[pyarrow]",
        "ts_added_to_dataset": "timestamp[ns][pyarrow]",
        "extra_data": "string[pyarrow]"
    }

    def __init__(self, filename: str, collection_id: str = None, collection_group: str = None, verbose: bool = False):
        self.filename = filename
        self.collection_id = collection_id if collection_id else os.path.basename(filename)
        self.collection_group = collection_group
        self.verbose = verbose
        self.ts_added_to_dataset = None # Should be set by subclasses

    def process(self) -> pd.DataFrame:
        """
        Main processing method. Must be implemented by subclasses.
        Should return a standardized DataFrame.
        """
        raise NotImplementedError("Subclasses must implement process()")

    def standardize_output(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ensures the dataframe has all required columns and correct dtypes.
        """
        df = df.copy()

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

        # 3. Filter to only required columns (plus potentially others if we want to keep them? 
        # The prompt implies loose structure but specific columns. Let's keep specific ones + extra if relevant, 
        # but the request was specific about "result should be a dataframe with specific columns".
        # Let's keep ONLY required columns to be strict, as per "generate outputs following the same structure".
        df = df[list(self.REQUIRED_COLUMNS.keys())].copy()
        
        return df




class DDPIngester(BaseIngester):
    
    def process(self) -> pd.DataFrame:
        if not data_io.exists(storage_location = "ddp_raw", filename = self.filename):
             raise FileNotFoundError(f"File {self.filename} not found")

        donation_dict = data_io.load_json(storage_location = "ddp_raw", filename = self.filename)
        self.ts_added_to_dataset = data_io.getmtime(storage_location = "ddp_raw", filename = self.filename)

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
        
        if not donation_items:
             if self.verbose: print(f"ERROR: No collection items found in file {self.filename}")
             return pd.DataFrame(columns=self.REQUIRED_COLUMNS.keys()) # Return empty compliant DF

        df = pd.DataFrame.from_records(donation_items)
        df['collection_id'] = self.collection_id

        # keep rows that have at least one variable and contain 'date'
        mask_date = df['variable_list'].map(lambda lst: 'date' in lst)
        df = df[mask_date & (df['variable_list'].map(len) > 0)].copy()

        # unpack value list
        df['date'] = pd.to_datetime(df['value_list'].str[0])

        try:
             df['primary_label'] = df['variable_list'].str[1]
             df['extra_data'] = df['value_list'].str[1]
        except:
             df['primary_label'] = pd.NA
             df['extra_data'] = pd.NA
        
        # Extract item_id
        item_ids_from_url = df["extra_data"].astype("string").str.rsplit("/", n=2).str[-2]
        digits = item_ids_from_url.str.fullmatch(r"\d+")
        item_ids = item_ids_from_url.where(digits)
        
        mask = (df["primary_label"]=="link") & (df["event_type"].notna())
        df["item_id"] = item_ids.where(mask)
        
        # nullify extra_data where item_id was extracted
        df.loc[df["item_id"].notnull(), "extra_data"] = pd.NA

        # timestamps
        df['timestamp'] = (df['date'].astype("int64") // 1_000_000_000)

        # map event types
        df["event_type"] = df["event_type"].map({
            'videolist':'watch', 'commentslist':'comment', 'post':'post',
            'searchlist':'search', 'fanslist':'followed_by', 'following':'following',
            'itemfavoritelist':'fave', 'favoritevideolist':'fave'
        })
        
        df.loc[df[df["primary_label"]=="ip"].index,"event_type"] = "login_event"
        
        # Convert critical columns to pyarrow to ensure consistent filtering behaviour / avoid crashes
        df["event_type"] = df["event_type"].astype("string[pyarrow]")
        df["item_id"] = df["item_id"].astype("string[pyarrow]")

        df = df.rename(columns={"timestamp": "utc_timestamp"})
        df["utc_timestamp"] = pd.to_datetime(df["utc_timestamp"], unit='s', utc=True)
        df["tz_offset"] = infer_timezone_offset(df["utc_timestamp"])

        # cleanup
        df = df[((df["event_type"] != "watch") | (df["item_id"].notna()))].copy()
        
        df['collection_group'] = self.collection_group
        df["ts_added_to_dataset"] = pd.to_datetime(self.ts_added_to_dataset, unit="s")
        df["source_platform"] = "tiktok"
        df["data_source"] = "ddp"
        
        # Sorting
        df.sort_values("utc_timestamp", inplace=True, kind='mergesort')
        df.reset_index(drop=True, inplace=True)

        # Logic for first watch event
        # Use .any() for safety with extension types
        if (df["event_type"] == "watch").any():
            first_watch_idx = df[df["event_type"] == "watch"].index[0]
            df = df.loc[first_watch_idx:].copy()


        df['delta'] = df['utc_timestamp'] - df['utc_timestamp'].shift(1)
        df['delta'] = df['delta'].dt.total_seconds()
        df['session_break'] = (df['delta'].isna()) | (df['delta'] > 180)
        df['session_id'] = df['session_break'].astype(bool).cumsum()

        # Ensure has_item is numpy int to avoid pyarrow groupby issues if any
        has_item = df['item_id'].notna().astype(int) 
        
        cumulative_items = has_item.groupby(df['session_id']).cumsum()
        df = df[cumulative_items > 0].copy()
        
        df['item_id'] = df.groupby('session_id')['item_id'].ffill()
        
        session_counts = df.groupby("session_id")["session_id"].transform("count")
        df = df[session_counts > 1].copy()

        return self.standardize_output(df)




class ZeeschuimerIngester(BaseIngester):

    def process(self) -> pd.DataFrame:
        zeeschuimer_list = data_io.read_ndjson_file(storage_location="zeeschuimer_raw", filename = self.filename)
        df = pd.json_normalize(zeeschuimer_list)
        
        # Filter FYP
        if 'source_platform_url' in df.columns:
            df = df[df['source_platform_url'] == 'https://www.tiktok.com/foryou'].copy()
        
        # Filter valid item_ids
        if 'item_id' in df.columns:
             df = df[df.item_id.map(lambda x:all([u in "0123456789" for u in x]) and len(x) == 19)].copy()
        
        # Clean URL to get tz_offset etc
        source_details = []
        for ii in df.index:
            source_details += [clean_url(df['source_url'][ii])]
        source_details = pd.DataFrame(source_details)
        df = pd.merge(left=df, right=source_details, left_index=True, right_index=True)

        # Time conversions
        df["timestamp_collected"] = df["timestamp_collected"].astype(np.int64)
        df["timestamp_collected"] = df["timestamp_collected"].apply(lambda x: _dt.datetime.fromtimestamp(np.int64(x/1000)))

        unique_tz = df["source_url.tz_name"].unique()
        if len(unique_tz) > 1:
            raise ValueError(f"Multiple timezones found: {unique_tz}")
        
        if len(unique_tz) == 1:
             tz = ZoneInfo(unique_tz[0])
             df["utc_timestamp"] = (
                 df["timestamp_collected"]
                 .dt.tz_localize(tz, ambiguous='NaT', nonexistent='NaT')
                 .dt.tz_convert("UTC")
             )
             df["tz_offset"] = df["timestamp_collected"] - df["utc_timestamp"].dt.tz_localize(None)
             df["tz_offset"] = df["tz_offset"].dt.total_seconds() / 3600

        
        ts_added = data_io.getmtime(storage_location = "zeeschuimer_raw", filename = self.filename)
        df['ts_added_to_dataset'] = pd.to_datetime(ts_added, unit="s")
        
        df['collection_id'] = self.collection_id
        df['collection_group'] = self.collection_group
        df["source_platform"] = "tiktok"
        df["data_source"] = "zeeschuimer"
        df["event_type"] = "watch"
        df["extra_data"] = pd.NA
        
        return self.standardize_output(df)




def flatten_single_tiktok_ddp_from_raw_file(
    filename: str = None,
    collection_id: str = None,
    collection_group: str = None,
    verbose: bool = False) -> pd.DataFrame:
    
    ingester = DDPIngester(filename, collection_id, collection_group, verbose)
    return ingester.process()




def flatten_single_tiktok_zeeschuimer_dict(
    filename: str = None,
    collection_id: str = None,
    collection_group: str = None,
    verbose: bool = False) -> pd.DataFrame:

    ingester = ZeeschuimerIngester(filename, collection_id, collection_group, verbose)
    return ingester.process()




