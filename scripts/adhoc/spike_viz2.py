import numpy as np, pandas as pd, time, json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from sklearn.preprocessing import normalize
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from google import genai

E = np.load("/tmp/spike_E.npy")
m = pd.read_parquet("/tmp/spike_map.parquet").reset_index(drop=True)  # has x,y from TSNE
print(f"E={E.shape}, map={m.shape}")

# recompute PCA-50 space (cluster here, not on 2D)
En = normalize(E - E.mean(0))
P = PCA(n_components=50, random_state=0).fit_transform(En)

K = 40
km = KMeans(n_clusters=K, random_state=0, n_init=10)
m["kn"] = km.fit_predict(P)

# top terms
stories = m["video_story"].astype("string").fillna("").tolist()
vec = TfidfVectorizer(max_features=8000, min_df=3, max_df=0.4, stop_words="english", ngram_range=(1,2))
T = vec.fit_transform(stories); vocab = np.array(vec.get_feature_names_out())
def top_terms(mask,n=8):
    mm=np.asarray(T[mask.values].mean(0)).ravel(); return vocab[mm.argsort()[::-1][:n]].tolist()

client = genai.Client(vertexai=True,
                      project=os.environ["GCP_PROJECT_ID"],
                      location="us-central1")
def name_cluster(c):
    mask = m["kn"]==c; idx=np.where(mask.values)[0]
    cen=P[mask.values].mean(0); d=np.linalg.norm(P[idx]-cen,axis=1); pick=idx[np.argsort(d)[:10]]
    ex="\n".join(f"- {m.iloc[i]['video_story'][:150]}" for i in pick)
    p=(f"TikTok video summaries in one cluster:\n{ex}\n\nGive a SHORT 2-4 word micro-genre label. Reply only the label.")
    try:
        r=client.models.generate_content(model="gemini-2.5-flash", contents=p)
        return c, r.text.strip().replace("\n"," ")[:42]
    except Exception as e: return c, f"c{c}"

names={}
with ThreadPoolExecutor(max_workers=10) as ex:
    for f in as_completed([ex.submit(name_cluster,c) for c in range(K)]):
        c,nm=f.result(); names[c]=nm

sizes = m["kn"].value_counts()
print("\nKMeans-40 niches (every video assigned), by size:")
for c in sizes.index:
    mask=m["kn"]==c
    print(f"  N{c:>2} n={int(sizes[c]):>4}  {names[c]:<32} | {' '.join(top_terms(mask)[:6])}")

# ---- two big readable figures ----
# 1) category map
cats = m["content_category"].str.split(" \| ").str[0].fillna("none")
topcats = cats.value_counts().head(14).index.tolist()
cmap = plt.cm.tab20
fig,ax=plt.subplots(figsize=(20,18))
for j,cat in enumerate(topcats):
    s=cats==cat; ax.scatter(m.loc[s,"x"],m.loc[s,"y"],s=7,alpha=0.55,color=cmap(j%20),label=f"{cat} ({s.sum()})")
s=~cats.isin(topcats); ax.scatter(m.loc[s,"x"],m.loc[s,"y"],s=5,alpha=0.2,color="lightgray",label="other")
ax.legend(markerscale=3,fontsize=12,loc="upper right"); ax.axis("off")
ax.set_title("10k-video semantic map (gemini-embedding-001 → PCA → t-SNE) — colored by content_category",fontsize=15)
plt.tight_layout(); plt.savefig("/tmp/spike_map_category.png",dpi=95,bbox_inches="tight"); plt.close()

# 2) KMeans niches named
fig,ax=plt.subplots(figsize=(20,18))
for c in range(K):
    s=m["kn"]==c; ax.scatter(m.loc[s,"x"],m.loc[s,"y"],s=7,alpha=0.55,color=cmap(c%20))
for c in range(K):
    s=m["kn"]==c; cx,cy=m.loc[s,"x"].median(),m.loc[s,"y"].median()
    ax.text(cx,cy,names[c],fontsize=10,ha="center",fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.2",fc="white",ec="gray",alpha=0.8))
ax.axis("off"); ax.set_title(f"Same map — {K} KMeans micro-genres (every video assigned), Gemini-named",fontsize=15)
plt.tight_layout(); plt.savefig("/tmp/spike_map_niches.png",dpi=95,bbox_inches="tight"); plt.close()
print("\nsaved /tmp/spike_map_category.png and /tmp/spike_map_niches.png")
