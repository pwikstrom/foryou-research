import pandas as pd
import pyarrow.parquet as pq
import os
for root, dirs, files in os.walk('.'):
    if 'enrichment_status.parquet' in files:
        p = os.path.join(root, 'enrichment_status.parquet')
        print(p)
        df = pd.read_parquet(p)
        print("Columns:", df.columns.tolist())
        print("Scrape/Annotate States:", df[['scrape_status', 'annotation_status']].drop_duplicates().to_dict('records') if 'scrape_status' in df.columns else "Not found")
        print("Sample row:")
        print(df.head(1).T)
        break
