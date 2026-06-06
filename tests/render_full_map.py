import json, numpy as np, pandas as pd
import fyp.data_io as data_io, fyp.embeddings as emb
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

m = data_io.load_parquet_selective("recoded","video_map.parquet")
niches = data_io.load_json("recoded","video_niches.json")
m = m[m["x"].notna()].copy()              # the 30k mapped sample
m["item_id"] = m["item_id"].astype("string")
print(f"mapped points: {len(m):,}  niches: {len(niches)}")

# join held-out content_category (NOT used in the embedding) for validation
anno = data_io.load_parquet_selective("recoded", emb.ANNOTATIONS_FILE, columns=["item_id","content_category"])
anno["item_id"]=anno["item_id"].astype("string")
anno["cat1"]=anno["content_category"].apply(lambda x: str(x[0]) if x is not None and hasattr(x,'__len__') and len(x)>0 else "none")
m = m.merge(anno[["item_id","cat1"]], on="item_id", how="left")
m["name"]=m["niche"].map(lambda n: niches.get(str(int(n)),{}).get("name",str(n)))

fig,axes=plt.subplots(1,2,figsize=(30,15))
cmap=plt.cm.tab20
# LEFT: colored by held-out content_category
cats=m["cat1"].fillna("none"); top=cats.value_counts().head(16).index.tolist()
for j,c in enumerate(top):
    s=cats==c; axes[0].scatter(m.loc[s,"x"],m.loc[s,"y"],s=4,alpha=0.5,color=cmap(j%20),label=f"{c} ({s.sum()})")
s=~cats.isin(top); axes[0].scatter(m.loc[s,"x"],m.loc[s,"y"],s=3,alpha=0.2,color="lightgray",label="other")
axes[0].legend(markerscale=4,fontsize=11,loc="upper right"); axes[0].axis("off")
axes[0].set_title("256k-video map (30k shown) — colored by HELD-OUT content_category\n(category was NOT used to build the embedding — validates topical organization)",fontsize=14)
# RIGHT: colored by niche, label the 45 largest
sizes=m["niche"].value_counts()
big=sizes.head(45).index.tolist()
for j,n in enumerate(sizes.index):
    s=m["niche"]==n; axes[1].scatter(m.loc[s,"x"],m.loc[s,"y"],s=4,alpha=0.5,color=cmap(int(n)%20))
for n in big:
    s=m["niche"]==n; cx,cy=m.loc[s,"x"].median(),m.loc[s,"y"].median()
    nm=niches.get(str(int(n)),{}).get("name",str(n))
    axes[1].text(cx,cy,nm,fontsize=7.5,ha="center",fontweight="bold",
                 bbox=dict(boxstyle="round,pad=0.15",fc="white",ec="gray",alpha=0.75))
axes[1].axis("off"); axes[1].set_title("Same map — 150 niches (largest 45 labeled), Gemini-named",fontsize=14)
plt.tight_layout(); plt.savefig("/tmp/full_map.png",dpi=100,bbox_inches="tight"); plt.close()
print("saved /tmp/full_map.png")

# niche summary table (top 50 by size)
rows=[(int(k),v["size"],v["name"]," ".join(v["terms"][:5]),",".join(v["top_categories"][:2])) for k,v in niches.items()]
rows.sort(key=lambda r:-r[1])
print("\nTop 50 of 150 niches (full corpus):")
for nid,sz,nm,terms,cats in rows[:50]:
    print(f"  N{nid:>3} n={sz:>5}  {nm:<30.30} [{cats[:20]:<20}] {terms}")
