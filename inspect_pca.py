import sys
from fyp.data_service import get_pca_df
import pandas as pd
from datetime import datetime

# We will just print the columns in pca df
from fyp.data_service import get_accessible_studies
# Note: get_accessible_studies signature is (username, role, is_admin)
studies = get_accessible_studies('admin', 'admin', True)
if not studies:
    print("No studies found")
    sys.exit(0)
    
study = studies[0]
print("Using study:", study)

df = get_pca_df(study)
if df is not None:
    for col in df.columns:
        if "week" in col.lower():
            print(f"Week col found: {col}")
            print(df[col].dropna().unique()[:10])
else:
    print("df is None")
