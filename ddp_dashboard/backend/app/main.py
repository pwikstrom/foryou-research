# backend/app/main.py
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import polars as pl
from .data import DF

app = FastAPI(title="TikTok View Explorer (polars)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],     # or ["*"] for dev
    allow_methods=["*"],
    allow_headers=["*"],
)

def to_iso(s):                                   # unchanged
    return s.dt.strftime("%Y-%m-%dT%H:%M:%S")



@app.get("/views")
def list_views(
    limit:  int                  = Query(100, ge=1, le=1000),
    after:  datetime | None      = None,          # ← typed!
    before: datetime | None      = None,          # ← typed!
    video_id: int     | None     = None,
):
    q = DF
    if after:
        q = q.filter(pl.col("viewed_at") >= after)
    if before:
        q = q.filter(pl.col("viewed_at") <= before)
    if video_id:
        q = q.filter(pl.col("video_id") == video_id)

    out = (
        q.sort("viewed_at", descending=True)
          .head(limit)
          .with_columns(to_iso(pl.col("viewed_at")).alias("viewed_at"))
          .to_dicts()
    )
    return out


@app.get("/daily_counts")
def daily_counts():
    daily = (
        DF.groupby_dynamic("viewed_at", every="1d", closed="left")
          .agg(pl.len().alias("views"))
          .sort("viewed_at")
          .with_columns(to_iso(pl.col("viewed_at")).alias("date"))
          .select(["date", "views"])
          .to_dicts()
    )
    return daily


@app.get("/top_videos")
def top_videos(limit: int = 50):
    top = (
        DF.groupby("video_id")
          .len()
          .sort("len", descending=True)
          .head(limit)
          .rename({"len": "views"})
          .to_dicts()
    )
    return top