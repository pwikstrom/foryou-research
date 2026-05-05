#!/usr/bin/env python3
"""Test the rewritten identify_similar_file_content against the real top
dataset and the three new VERIFY_*_May.json files in ddp_raw.

Expected behaviour:
  - All three new files contribute their unique rows.
  - Wilma May's raw_file is clustered with the existing UUID raw_file
    063ec203-... and shares its canonical collection_id (the May file is the
    newest, so its collection_id wins).
  - Karrie May's raw_file is clustered with b34bdc91-... similarly.
  - Clara May has no prior data and stays as its own cluster.
  - Discarded raw_files set is NOT mutated by this function.
"""


import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fyp import data_io, fyp_config
from fyp.ingest import ForYouCollection, TikTokDDPCollection


fyp_config.initialize()


NEW_FILES = [
    "VERIFY_Clara_May.json",
    "VERIFY_Karrie_May.json",
    "VERIFY_Wilma_May.json",
]




def _load_top() -> pd.DataFrame:
    return data_io.load_parquet(
        storage_location="recoded",
        filename="collections_recoded.parquet",
    )




def _process_new_files() -> pd.DataFrame:
    sub = TikTokDDPCollection(verbose=False)
    dfs = []
    for fn in NEW_FILES:
        df = sub.load_single_raw(fn)
        df["raw_file"] = fn
        df["ts_added_to_dataset"] = pd.Timestamp.utcnow()
        dfs.append(df)
    sub.data = pd.concat(dfs, ignore_index=True)
    sub.state = "raw"
    sub.process()
    return sub.data




def main() -> None:
    top_df = _load_top()
    new_df = _process_new_files()
    print(f"Top: {len(top_df):,} rows")
    print(f"New (after process_single, all 3 files): {len(new_df):,} rows")
    print(f"  by raw_file: {new_df['raw_file'].value_counts().to_dict()}")

    top = ForYouCollection(verbose=True)
    top.data = pd.concat([top_df, new_df], ignore_index=True)
    top.state = "processed"
    discarded_before = list(top.discarded_raw_files)

    print(f"\nCombined before clustering: {len(top.data):,} rows")
    cid_remap = top.identify_similar_file_content(drop_them=True)
    print(f"Combined after  clustering: {len(top.data):,} rows")
    print(f"cid_remap returned: {cid_remap}")
    assert isinstance(cid_remap, dict), f"expected dict, got {type(cid_remap)}"
    assert len(cid_remap) >= 2, (
        f"expected at least 2 cid remaps (Wilma+Karrie clusters), got {cid_remap}"
    )

    # Assertions / checks
    new_set = set(NEW_FILES)
    survivors_by_file = (
        top.data[top.data["raw_file"].isin(new_set)]["raw_file"]
        .value_counts()
        .to_dict()
    )
    print("\nNew-file rows surviving:")
    for fn in NEW_FILES:
        print(f"  {fn}: {survivors_by_file.get(fn, 0)}")

    # All three new files should still have rows in the dataset.
    assert all(fn in survivors_by_file for fn in NEW_FILES), (
        f"Expected all three May files to survive, got {survivors_by_file}"
    )

    # No new entries added to the blacklist.
    new_discarded = set(top.discarded_raw_files) - set(discarded_before)
    assert not new_discarded, (
        f"identify_similar_file_content must not mutate discarded_raw_files; "
        f"new entries: {new_discarded}"
    )

    # Cluster check: Wilma's May file should share collection_id with its
    # historical sibling 063ec203-...; Karrie's with b34bdc91-...
    # Note: under keep='last', if every row of an older sibling overlaps with
    # the new May file (Wilma's case), the sibling raw_file may end up with
    # 0 surviving rows — that's correct, the May data fully supersedes it.
    expected_clusters = {
        "VERIFY_Wilma_May.json": "063ec203-9b13-4e2a-8242-34d24ae1aa7a",
        "VERIFY_Karrie_May.json": "b34bdc91-315e-4bc7-8a71-4fb362d0e2d5",
    }
    for may_file, sibling_uuid in expected_clusters.items():
        may_cid = top.data[top.data["raw_file"] == may_file]["collection_id"].iloc[0]
        sibling_rows = top.data[top.data["raw_file"] == sibling_uuid]
        print(
            f"\nCluster check: {may_file} cid={may_cid!r}; "
            f"sibling {sibling_uuid} surviving rows={len(sibling_rows)}"
        )
        if len(sibling_rows) > 0:
            sibling_cid = sibling_rows["collection_id"].iloc[0]
            assert may_cid == sibling_cid, (
                f"{may_file} and {sibling_uuid} should share collection_id "
                f"after clustering; got {may_cid!r} vs {sibling_cid!r}"
            )
            # The canonical collection_id wins from the newest donation
            # (the May file). It should differ from the older sibling's UUID.
            assert sibling_cid != sibling_uuid, (
                f"Canonical collection_id for cluster containing {may_file} "
                f"should be the May file's, not the older sibling's UUID "
                f"({sibling_uuid}); got {sibling_cid!r}."
            )

    # Clara should be a singleton cluster — her collection_id unchanged.
    clara_rows = top.data[top.data["raw_file"] == "VERIFY_Clara_May.json"]
    print(
        f"\nClara: {len(clara_rows)} rows kept, collection_id={clara_rows['collection_id'].iloc[0]}"
    )
    assert len(clara_rows) == 342, f"Clara should have 342 rows, got {len(clara_rows)}"

    print("\nAll assertions passed.")




if __name__ == "__main__":
    main()
