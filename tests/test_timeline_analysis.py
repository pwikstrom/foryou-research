"""Test that analyse_timeline correctly filters by first_activity_date."""
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from fyp.fyp_config import fyp_cf
from fyp.studies import init_study_defs
from web_interface.data_service import get_timeline_data
from fyp.timeline_analysis import analyse_timeline
import fyp.data_io as data_io
import pandas as pd

init_study_defs()

donation_id = "88f5fb9a-0c4c-4abc-916d-f47d990becc4"

# Load first_event_ts from ddp_metadata
df_meta = data_io.load_parquet(storage_location="recoded", filename="ddp_metadata.parquet", verbose=False)
first_event_col = ('personas', 'first_event_ts') if ('personas', 'first_event_ts') in df_meta.columns else 'first_event_ts'
ts = df_meta.loc[donation_id, first_event_col] if donation_id in df_meta.index else None
first_date = str(ts)[:10] if pd.notna(ts) else None
print(f"Donation: {donation_id}")
print(f"First activity date: {first_date}")

# Get timeline data
tdata = get_timeline_data(donation_id, interval='day')
dates = tdata.get("dates", [])
print(f"Total timeline dates: {len(dates)}")
print(f"Date range: {dates[0]} to {dates[-1]}")

# Find start_offset 
start_offset = 0
for idx, d in enumerate(dates):
    if d >= first_date:
        start_offset = idx
        break
print(f"Start offset: {start_offset} (skipping {start_offset} periods before first activity)")

# Run analysis WITH first_activity_date
analysis = analyse_timeline(tdata, interval='day', first_activity_date=first_date)

# Print summary for content_category
if 'G_content_category' in analysis:
    cats = analysis['G_content_category']['categories']
    print(f"\nG_content_category: {len(cats)} categories, start_offset={analysis['G_content_category'].get('start_offset', 0)}")
    for cat in cats[:3]:
        print(f"  {cat['label']}: score={cat['score']}, trend={cat['trend']['total_change']}pp")
        print(f"    trend intercept={cat['trend']['intercept']}, slope={cat['trend']['slope']}")
        if cat['anomalies']:
            print(f"    anomalies: {[a['index'] for a in cat['anomalies']]}")
        print(f"    break: index={cat['break']['index']}, delta={cat['break']['delta']}pp")

# Save
for interval in ['day', 'week', 'month']:
    tdata_i = get_timeline_data(donation_id, interval=interval)
    if tdata_i and tdata_i.get("dates"):
        a = analyse_timeline(tdata_i, interval=interval, first_activity_date=first_date)
        if a:
            fname = f"timeline_analysis_{donation_id}_{interval}.json"
            data_io.save_json(a, storage_location="cache", filename=fname)
            print(f"Saved {fname}")

print("\nDone!")
