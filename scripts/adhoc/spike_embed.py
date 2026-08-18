import pandas as pd, numpy as np, pyarrow.parquet as pq, time, sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from google import genai
from google.genai.types import EmbedContentConfig
RECODED = "/Users/<user>/fyp_local/recoded"
rng = np.random.RandomState(2026)
N = 10000

def load_sel(fn, cols):
    tbl = pq.read_table(f"{RECODED}/{fn}", columns=cols)
    meta = tbl.schema.metadata or {}
    tbl = tbl.replace_schema_metadata({k:v for k,v in meta.items() if k!=b'pandas'} or None)
    return tbl.to_pandas(types_mapper=pd.ArrowDtype)

print("loading annotations...", flush=True)
a = load_sel("machine_annotations_recoded.parquet",
    ["item_id","annotated_ok","video_story","transcript_no_repetitions","content_category",
     "objects","text_overlays","main_activity","type_of_story","notable_sounds","background_music"])
a = a[a["annotated_ok"]==True].reset_index(drop=True)
a = a.iloc[rng.choice(len(a), N, replace=False)].reset_index(drop=True)
a["item_id"] = a["item_id"].astype(str)

print("loading scrape fields...", flush=True)
s = load_sel("scrapes_recoded.parquet", ["item_id","music_title","desc_hashtags"])
s["item_id"] = s["item_id"].astype(str)
s = s.drop_duplicates("item_id")
a = a.merge(s, on="item_id", how="left")

def lst(x, cap):
    if x is None or not hasattr(x,'__len__'): return ""
    return (" ".join(str(t) for t in x))[:cap]
def txt(x, cap):
    if x is None: return ""
    return str(x)[:cap]

def build_doc(r):
    story = txt(r["video_story"], 1200)
    cat = lst(r["content_category"], 120)
    act = f'{txt(r["main_activity"],40)}; {txt(r["type_of_story"],40)}'
    spoken = txt(r["transcript_no_repetitions"], 800) or "(none)"
    overlays = lst(r["text_overlays"], 400) or "(none)"
    objs = lst(r["objects"], 300)
    sounds = f'{lst(r["notable_sounds"],120)}; {lst(r["background_music"],60)}; {txt(r["music_title"],80)}'
    tags = lst(r["desc_hashtags"], 200)
    return (f"Story: {story}\nCategory: {cat}\nActivity: {act}\n"
            f"Spoken: {spoken}\nOn-screen text: {overlays}\nObjects: {objs}\n"
            f"Sounds/music: {sounds}\nHashtags: {tags}")

a["doc"] = a.apply(build_doc, axis=1)
print(f"built {len(a)} docs; median doc chars={int(a['doc'].str.len().median())}", flush=True)

client = genai.Client(vertexai=True,
                      project=os.environ["GCP_PROJECT_ID"],
                      location="us-central1")
MODEL = "gemini-embedding-001"
docs = a["doc"].tolist()
BS = 20
batches = [(i, docs[i:i+BS]) for i in range(0, len(docs), BS)]
results = {}

def embed_batch(args):
    i, chunk = args
    for attempt in range(4):
        try:
            r = client.models.embed_content(model=MODEL, contents=chunk,
                    config=EmbedContentConfig(task_type="CLUSTERING"))
            return i, [e.values for e in r.embeddings]
        except Exception as e:
            if attempt == 3:
                return i, ("ERR", str(e)[:120])
            time.sleep(1.5*(attempt+1))

t0 = time.time(); done = 0
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(embed_batch, b) for b in batches]
    for f in as_completed(futs):
        i, vecs = f.result()
        if isinstance(vecs, tuple) and vecs[0]=="ERR":
            print(f"  batch@{i} FAILED: {vecs[1]}", flush=True); continue
        results[i] = vecs
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{len(batches)} batches  {time.time()-t0:.0f}s", flush=True)

# reassemble in order
emb = []
ok_mask = []
for i in range(0, len(docs), BS):
    if i in results:
        emb.extend(results[i]); ok_mask.extend([True]*len(results[i]))
    else:
        # fill failures with NaN rows to keep alignment, drop later
        n = len(docs[i:i+BS]); emb.extend([[np.nan]]*n); ok_mask.extend([False]*n)
ok_mask = np.array(ok_mask)
a = a[ok_mask].reset_index(drop=True)
E = np.array([e for e,m in zip(emb, ok_mask) if m], dtype=np.float32)
print(f"embedded {E.shape[0]} of {N}, dim={E.shape[1]}, total {time.time()-t0:.0f}s", flush=True)

out = a[["item_id","video_story","content_category","main_activity"]].copy()
out["content_category"] = out["content_category"].apply(lambda x: " | ".join(map(str,x)) if x is not None and hasattr(x,'__len__') else "")
np.save("/tmp/spike_E.npy", E)
out.to_parquet("/tmp/spike_meta.parquet")
print("saved /tmp/spike_E.npy and /tmp/spike_meta.parquet", flush=True)
