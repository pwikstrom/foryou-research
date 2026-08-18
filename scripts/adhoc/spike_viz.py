import numpy as np, pandas as pd, time, json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from sklearn.preprocessing import normalize
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from google import genai

E = np.load("/tmp/spike_E.npy")
meta = pd.read_parquet("/tmp/spike_meta.parquet").reset_index(drop=True)
print(f"loaded E={E.shape}, meta={meta.shape}")

# preprocess: mean-center -> L2 -> PCA(50)
Ec = E - E.mean(0)
En = normalize(Ec)
pca = PCA(n_components=50, random_state=0)
P = pca.fit_transform(En)
print(f"PCA50 var explained: {pca.explained_variance_ratio_.sum():.3f}")

# cluster on PCA-50 (NOT on the 2D map)
t0=time.time()
hdb = HDBSCAN(min_cluster_size=40, min_samples=10, metric="euclidean")
lab = hdb.fit_predict(P)
n_clusters = len(set(lab)) - (1 if -1 in lab else 0)
noise = (lab==-1).mean()*100
print(f"HDBSCAN: {n_clusters} clusters, noise={noise:.1f}%, {time.time()-t0:.0f}s")

# 2D map via TSNE on PCA-50
t0=time.time()
ts = TSNE(n_components=2, perplexity=30, init="pca", random_state=0, max_iter=1000)
XY = ts.fit_transform(P)
print(f"TSNE done {time.time()-t0:.0f}s")
meta["x"], meta["y"], meta["cluster"] = XY[:,0], XY[:,1], lab

# top TF-IDF terms per cluster (from video_story)
stories = meta["video_story"].astype("string").fillna("").tolist()
vec = TfidfVectorizer(max_features=8000, min_df=3, max_df=0.4, stop_words="english", ngram_range=(1,2))
T = vec.fit_transform(stories)
vocab = np.array(vec.get_feature_names_out())
def top_terms(mask, n=8):
    if mask.sum()==0: return []
    m = np.asarray(T[mask].mean(0)).ravel()
    return vocab[m.argsort()[::-1][:n]].tolist()

clusters = sorted([c for c in set(lab) if c!=-1], key=lambda c:-(lab==c).sum())
summary = []
for c in clusters:
    mask = lab==c
    cats = meta.loc[mask,"content_category"].str.split(" | ").explode().value_counts().head(2)
    summary.append({"cluster":int(c), "size":int(mask.sum()),
                    "terms":top_terms(mask), "top_cat":list(cats.index[:2])})

# Gemini cluster naming from exemplars (centroid-nearest stories)
client = genai.Client(vertexai=True,
                      project=os.environ["GCP_PROJECT_ID"],
                      location="us-central1")
def name_cluster(item):
    c, mask = item
    idx = np.where(mask)[0]
    cen = P[mask].mean(0)
    d = np.linalg.norm(P[idx]-cen, axis=1)
    pick = idx[np.argsort(d)[:10]]
    exemplars = "\n".join(f"- {meta.iloc[i]['video_story'][:160]}" for i in pick)
    prompt = (f"These are summaries of TikTok videos in one cluster:\n{exemplars}\n\n"
              "Give a SHORT 2-4 word label naming this micro-genre. Reply with only the label.")
    try:
        r = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return c, r.text.strip().replace("\n"," ")[:48]
    except Exception as e:
        return c, f"(err {str(e)[:30]})"

names = {}
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(name_cluster, (s["cluster"], lab==s["cluster"])) for s in summary]
    for f in as_completed(futs):
        c, nm = f.result(); names[c]=nm

print("\n"+"="*100)
print(f"{n_clusters} micro-genres ({noise:.0f}% noise). Top 30 by size:")
for s in summary[:30]:
    nm = names.get(s["cluster"],"")
    print(f"  C{s['cluster']:>3} n={s['size']:>4}  {nm:<34} cat={','.join(s['top_cat'])[:24]:<24} | {' '.join(s['terms'][:6])}")

# ---- render maps ----
meta["label"] = meta["cluster"].map(lambda c: names.get(c,"noise" if c==-1 else str(c)))
fig, axes = plt.subplots(1,2, figsize=(26,12))
# left: by content_category
cats = meta["content_category"].str.split(" | ").str[0].fillna("none")
topcats = cats.value_counts().head(12).index.tolist()
cmap = plt.cm.tab20
for j,cat in enumerate(topcats):
    m = cats==cat
    axes[0].scatter(meta.loc[m,"x"], meta.loc[m,"y"], s=4, alpha=0.5, color=cmap(j%20), label=cat)
m = ~cats.isin(topcats)
axes[0].scatter(meta.loc[m,"x"], meta.loc[m,"y"], s=3, alpha=0.2, color="lightgray", label="other")
axes[0].legend(markerscale=3, fontsize=8, loc="upper right")
axes[0].set_title("Video semantic map — colored by content_category (gemini-embedding-001)")
axes[0].axis("off")
# right: by HDBSCAN cluster, with labels at centroids
noise_m = meta["cluster"]==-1
axes[1].scatter(meta.loc[noise_m,"x"], meta.loc[noise_m,"y"], s=3, alpha=0.12, color="lightgray")
for j,c in enumerate(clusters):
    m = meta["cluster"]==c
    axes[1].scatter(meta.loc[m,"x"], meta.loc[m,"y"], s=5, alpha=0.6, color=cmap(j%20))
for s in summary[:40]:
    c=s["cluster"]; m=meta["cluster"]==c
    cx,cy = meta.loc[m,"x"].median(), meta.loc[m,"y"].median()
    axes[1].text(cx,cy, names.get(c,str(c)), fontsize=7, ha="center",
                 bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7))
axes[1].set_title(f"Same map — {n_clusters} HDBSCAN micro-genres, Gemini-named")
axes[1].axis("off")
plt.tight_layout()
plt.savefig("/tmp/spike_map.png", dpi=110, bbox_inches="tight")
print("\nsaved /tmp/spike_map.png")
meta.to_parquet("/tmp/spike_map.parquet")
json.dump({str(s["cluster"]):{"name":names.get(s["cluster"]),"size":s["size"],
          "terms":s["terms"],"top_cat":s["top_cat"]} for s in summary},
          open("/tmp/spike_clusters.json","w"), indent=2)
print("saved /tmp/spike_clusters.json")
