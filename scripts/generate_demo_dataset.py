#!/usr/bin/env python3
"""Generate the synthetic demonstration dataset (S4 demo study).

Produces, deterministically from a seed:
  * one TikTok-DDP-format donor JSON per synthetic participant (ingested by
    ``TikTokDemoCollection`` via the normal upload → ingest refresh path),
  * one contract-conformant scrape parquet (written through the real
    ``_canonicalize_recode_save`` path, stamped with the active ``sv_``),
  * one raw machine-annotation JSON (``structured: True`` entries whose
    responses conform to ``config/annotation_contract.toml``, stamped with
    the active ``av_``) consumed by the normal refine/consolidate path.

All item ids are 19 digits starting with ``utils.DEMO_ITEM_ID_PREFIX`` so
demo material is excluded from corpus-global artifacts (embeddings map) and
from the scrape/annotation queue builders. The content model plants real
group differences (persona niche weights, per-niche political/sensitivity/
gender/advertising distributions, completion behaviour) so every analysis
tab shows teachable structure.

Usage:
    source .venv/bin/activate
    # Inspect / test: write all artifacts as plain files to a directory
    python scripts/generate_demo_dataset.py --emit-only tmp/demo_out
    # Install into the configured data store (local or prod GCS env):
    python scripts/generate_demo_dataset.py --write
    # then: DM -> Ingestion -> Refresh, DM -> Refresh -> Consolidate & Refresh,
    # DM -> Define Studies -> create the demo study over the DEMO_P* collections.
"""

import argparse
import datetime as dt
import json
import random
import sys
import zlib
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from fyp.core.utils import DEMO_ITEM_ID_PREFIX  # noqa: E402

DEFAULT_SEED = 20260730
DEFAULT_DONORS = 5
DEFAULT_DAYS = 45
DEFAULT_AS_OF = "2026-06-30"  # fixed anchor — never wall-clock (determinism)

SECOND_GRAIN = 10  # all event epochs are multiples of this plus a donor residue


# The niche content model. Each niche fixes the distributions every item drawn
# from it uses — for both the scrape row (engagement, ads) and the annotation
# row (categories, scores, gender skew) — so group differences are planted,
# not accidental.
NICHES = [
    {
        "key": "cooking",
        "categories": ["Food", "DIY & Life Hacks"],
        "activities": ["cooking a one-pan dinner", "baking sourdough", "reviewing a street-food stall", "meal-prepping lunches"],
        "objects": ["frying pan", "chopping board", "fresh herbs", "oven tray", "mixing bowl"],
        "sounds": ["sizzling", "chopping", "kitchen timer"],
        "music": ["upbeat acoustic", "lo-fi beats"],
        "hashtags": ["cooking", "foodtok", "easyrecipes", "mealprep"],
        "story_types": ["Descriptive", "Human-Interest"],
        "political": (0, 10),
        "sensitivity": (0, 10),
        "gender_weights": [("Female", 5), ("Male", 4), ("-", 1)],
        "ad_rate": 0.10,
        "completion": (0.7, 1.0),
        "duration": (25, 90),
        "trend_cultural_rate": 0.15,
        "engagement": 1.2,
    },
    {
        "key": "football",
        "categories": ["Sports"],
        "activities": ["replaying a match highlight", "explaining an offside call", "training drills at the park"],
        "objects": ["football", "goal net", "stadium stands", "boots"],
        "sounds": ["crowd roar", "referee whistle"],
        "music": ["stadium anthem", "none"],
        "hashtags": ["football", "matchday", "goals", "skills"],
        "story_types": ["Event-Based", "Descriptive"],
        "political": (0, 15),
        "sensitivity": (0, 15),
        "gender_weights": [("Male", 7), ("Female", 2), ("Multiple", 1)],
        "ad_rate": 0.06,
        "completion": (0.55, 0.9),
        "duration": (15, 60),
        "trend_cultural_rate": 0.1,
        "engagement": 1.0,
    },
    {
        "key": "news_politics",
        "categories": ["News", "Society"],
        "activities": ["explaining a policy change", "reporting from a rally", "reacting to a press conference"],
        "objects": ["microphone", "news desk", "protest signs", "parliament building"],
        "sounds": ["crowd chants", "studio jingle"],
        "music": ["none", "tense underscore"],
        "hashtags": ["news", "politics", "auspol", "breaking"],
        "story_types": ["Issue-Based", "Event-Based"],
        "political": (55, 95),
        "sensitivity": (35, 80),
        "gender_weights": [("Male", 4), ("Female", 4), ("Multiple", 2)],
        "ad_rate": 0.02,
        "completion": (0.35, 0.75),
        "duration": (40, 180),
        "trend_cultural_rate": 0.02,
        "engagement": 0.8,
    },
    {
        "key": "comedy",
        "categories": ["Comedy"],
        "activities": ["acting out a flatmate skit", "lip-syncing a punchline", "pranking a friend"],
        "objects": ["couch", "phone camera", "kitchen", "car interior"],
        "sounds": ["laugh track", "record scratch"],
        "music": ["meme sound", "trending audio"],
        "hashtags": ["funny", "skit", "relatable", "fyp"],
        "story_types": ["Human-Interest", "Descriptive"],
        "political": (0, 20),
        "sensitivity": (0, 25),
        "gender_weights": [("Male", 4), ("Female", 4), ("Nonbinary", 1), ("Multiple", 1)],
        "ad_rate": 0.04,
        "completion": (0.8, 1.0),
        "duration": (8, 45),
        "trend_cultural_rate": 0.45,
        "engagement": 1.6,
    },
    {
        "key": "fashion_beauty",
        "categories": ["Fashion & Beauty"],
        "activities": ["doing a five-minute makeup look", "styling one dress three ways", "unboxing a clothing haul"],
        "objects": ["mirror", "makeup brushes", "clothing rack", "ring light"],
        "sounds": ["voiceover", "unboxing rustle"],
        "music": ["glossy pop", "chill r&b"],
        "hashtags": ["grwm", "fashion", "beautytok", "haul"],
        "story_types": ["Descriptive", "Human-Interest"],
        "political": (0, 10),
        "sensitivity": (5, 25),
        "gender_weights": [("Female", 8), ("Nonbinary", 1), ("Male", 1)],
        "ad_rate": 0.30,
        "completion": (0.5, 0.9),
        "duration": (20, 75),
        "trend_cultural_rate": 0.25,
        "engagement": 1.4,
    },
    {
        "key": "fitness",
        "categories": ["Fitness & Physical Health"],
        "activities": ["demonstrating a kettlebell circuit", "tracking a running challenge", "stretching after leg day"],
        "objects": ["dumbbells", "yoga mat", "running shoes", "gym rack"],
        "sounds": ["gym clatter", "heavy breathing"],
        "music": ["gym phonk", "electronic pump"],
        "hashtags": ["gymtok", "fitness", "workout", "running"],
        "story_types": ["Descriptive", "Human-Interest"],
        "political": (0, 10),
        "sensitivity": (5, 30),
        "gender_weights": [("Male", 5), ("Female", 4), ("Multiple", 1)],
        "ad_rate": 0.15,
        "completion": (0.45, 0.85),
        "duration": (20, 90),
        "trend_technical_rate": 0.2,
        "trend_cultural_rate": 0.2,
        "engagement": 1.1,
    },
    {
        "key": "pets",
        "categories": ["Animals"],
        "activities": ["filming a cat ignoring commands", "teaching a puppy to sit", "feeding backyard chickens"],
        "objects": ["cat", "dog", "food bowl", "leash"],
        "sounds": ["barking", "purring"],
        "music": ["cute ukulele", "trending audio"],
        "hashtags": ["pets", "cattok", "dogsoftiktok", "animals"],
        "story_types": ["Human-Interest", "Descriptive"],
        "political": (0, 5),
        "sensitivity": (0, 10),
        "gender_weights": [("-", 4), ("Female", 3), ("Male", 3)],
        "ad_rate": 0.03,
        "completion": (0.85, 1.0),
        "duration": (8, 40),
        "trend_cultural_rate": 0.3,
        "engagement": 1.8,
    },
]

# Donor personas: niche weights + behaviour. Weights are deliberately distinct
# so per-collection category profiles differ visibly in Explore/Correlations.
DONORS = [
    {"n": 1, "name": "Demo participant 01", "weights": {"cooking": 5, "pets": 3, "comedy": 2}, "plays": (18, 30)},
    {"n": 2, "name": "Demo participant 02", "weights": {"football": 5, "comedy": 3, "fitness": 2}, "plays": (22, 36)},
    {"n": 3, "name": "Demo participant 03", "weights": {"news_politics": 5, "comedy": 2, "cooking": 1}, "plays": (15, 26)},
    {"n": 4, "name": "Demo participant 04", "weights": {"fashion_beauty": 5, "fitness": 3, "pets": 1}, "plays": (20, 34)},
    {"n": 5, "name": "Demo participant 05", "weights": {"fitness": 4, "football": 3, "news_politics": 2}, "plays": (18, 30)},
]

# Local (UTC+10) session start hours with weights — evenings dominate.
SESSION_HOURS = [(7, 2), (12, 2), (17, 3), (20, 5), (22, 3)]

FIRST_NAMES = ["alex", "sam", "jordan", "casey", "riley", "morgan", "taylor",
               "jamie", "avery", "quinn", "harper", "rowan"]

ETHNICITIES = ["Caucasian", "South Asian", "Northeast Asian", "Southeast Asian",
               "Middle Eastern", "African", "-"]






def _weighted_choice(rng: random.Random, pairs):
    """Pick a value from ``[(value, weight), ...]``."""
    values = [v for v, _ in pairs]
    weights = [w for _, w in pairs]
    return rng.choices(values, weights=weights, k=1)[0]






def build_items(rng: random.Random, as_of: dt.datetime, n_items: int = 800) -> list[dict]:
    """Build the synthetic content corpus: one dict per item, all attributes.

    Every attribute needed by both the scrape row and the annotation response
    is fixed here, so the two layers stay consistent for the same item.
    """
    items = []
    used_ids: set[str] = set()
    for _ in range(n_items):
        niche = rng.choice(NICHES)
        while True:
            item_id = DEMO_ITEM_ID_PREFIX + f"{rng.randrange(10**15):015d}"
            if item_id not in used_ids:
                used_ids.add(item_id)
                break

        author_first = rng.choice(FIRST_NAMES)
        author_handle = f"{author_first}.{niche['key']}.{rng.randrange(100):02d}"
        duration = rng.randint(*niche["duration"])
        age_days = rng.randint(2, 400)
        create_time = as_of - dt.timedelta(days=age_days, seconds=rng.randrange(86400))

        play_count = int(rng.lognormvariate(10.5, 1.4)) + 200
        eng = niche["engagement"] * rng.uniform(0.5, 1.6)
        fave_count = int(play_count * 0.04 * eng)
        comment_count = int(play_count * 0.004 * eng * rng.uniform(0.5, 1.5))
        share_count = int(play_count * 0.006 * eng * rng.uniform(0.4, 1.6))
        save_count = int(play_count * 0.008 * eng * rng.uniform(0.4, 1.6))

        hashtags = rng.sample(niche["hashtags"], k=min(3, len(niche["hashtags"])))
        activity = rng.choice(niche["activities"])
        is_ad = rng.random() < niche["ad_rate"]
        gender = _weighted_choice(rng, niche["gender_weights"])

        items.append({
            "item_id": item_id,
            "niche": niche["key"],
            "niche_def": niche,
            "author_id": str(rng.randrange(10**10, 10**11)),
            "author_handle": author_handle,
            "author_name": author_first.capitalize(),
            "duration": duration,
            "create_time": create_time,
            "play_count": play_count,
            "fave_count": fave_count,
            "comment_count": comment_count,
            "share_count": share_count,
            "save_count": save_count,
            "desc": f"{activity.capitalize()} " + " ".join(f"#{h}" for h in hashtags),
            "hashtags": hashtags,
            "activity": activity,
            "is_ad": is_ad,
            "gender": gender,
            "ethnicity": rng.choice(ETHNICITIES),
            "political": rng.randint(*niche["political"]),
            "sensitivity": rng.randint(*niche["sensitivity"]),
            "story_type": rng.choice(niche["story_types"]),
            "categories": niche["categories"][: rng.randint(1, len(niche["categories"]))],
            "music_title": rng.choice(niche["music"]),
            "trend_cultural": rng.random() < niche.get("trend_cultural_rate", 0.1),
            "trend_technical": rng.random() < niche.get("trend_technical_rate", 0.08),
            "completion": rng.uniform(*niche["completion"]),
        })
    return items






def build_donor_events(rng: random.Random, donor: dict, donor_index: int,
                       items: list[dict], days: int, as_of: dt.datetime) -> dict:
    """Simulate one donor's activity: plays (with realistic dwell), faves,
    searches and logins.

    Every epoch second is ``SECOND_GRAIN * k + 2 * donor_index`` — donors can
    therefore never share a per-second timestamp, which keeps the ingest-side
    same-content clustering (>0.2 per-second overlap merges files into one
    collection) provably inert.

    Returns:
        {"plays": [(ts, item)], "faves": [(ts, item)], "searches": [(ts, term)],
         "logins": [(ts, ip)]}
    """
    residue = (2 * donor_index) % SECOND_GRAIN
    weights = donor["weights"]
    pool = [it for it in items if it["niche"] in weights]
    pool_weights = [weights[it["niche"]] for it in pool]

    def _align(ts: float) -> int:
        return int(ts // SECOND_GRAIN) * SECOND_GRAIN + residue

    plays, faves, searches, logins = [], [], [], []
    start_day = as_of - dt.timedelta(days=days)
    for day in range(days):
        # Occasional day off keeps active_days realistic but plentiful.
        if rng.random() < 0.06:
            continue
        date = start_day + dt.timedelta(days=day)
        n_plays_today = rng.randint(*donor["plays"])
        n_sessions = rng.randint(1, 3)
        per_session = max(4, n_plays_today // n_sessions)
        for _ in range(n_sessions):
            local_hour = _weighted_choice(rng, SESSION_HOURS)
            # Stored timestamps are UTC; donors browse at UTC+10 local time.
            utc_hour = (local_hour - 10) % 24
            ts = _align(dt.datetime(
                date.year, date.month, date.day, utc_hour,
                rng.randrange(60), tzinfo=dt.timezone.utc,
            ).timestamp() + rng.randrange(0, 1200))
            if rng.random() < 0.5:
                logins.append((ts - SECOND_GRAIN, f"203.0.113.{donor['n']}"))
            for _ in range(per_session):
                item = rng.choices(pool, weights=pool_weights, k=1)[0]
                plays.append((ts, item))
                if rng.random() < 0.05:
                    faves.append((ts + SECOND_GRAIN * rng.randint(1, 3), item))
                # Dwell: the item's completion tendency drives the gap to the
                # next event (ingest derives play_duration from that gap).
                dwell = max(SECOND_GRAIN,
                            _align(item["duration"] * item["completion"]) - residue + SECOND_GRAIN)
                ts += dwell + SECOND_GRAIN * rng.randint(0, 3)
            if rng.random() < 0.3:
                searches.append((ts, rng.choice(
                    ["dinner ideas", "highlights", "news today", "workout plan",
                     "outfit ideas", "funny videos", "cat videos"])))

    return {"plays": plays, "faves": faves, "searches": searches, "logins": logins}






def _ddp_ts(ts: int) -> str:
    """Epoch seconds -> the DDP export's UTC wall-clock string."""
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")






def donor_ddp_json(events: dict) -> dict:
    """Assemble one donor's TikTok-DDP-format export document."""
    link = "https://www.tiktokv.com/share/video/{item_id}/"
    by_ts = lambda pair: pair[0]  # noqa: E731 — tuples tie-break on dicts otherwise
    video_list = [
        {"Date": _ddp_ts(ts), "Link": link.format(item_id=item["item_id"])}
        for ts, item in sorted(events["plays"], key=by_ts)
    ]
    fave_list = [
        {"Date": _ddp_ts(ts), "Link": link.format(item_id=item["item_id"])}
        for ts, item in sorted(events["faves"], key=by_ts)
    ]
    search_list = [
        {"Date": _ddp_ts(ts), "SearchTerm": term}
        for ts, term in sorted(events["searches"], key=by_ts)
    ]
    login_list = [
        {"Date": _ddp_ts(ts), "IP": ip}
        for ts, ip in sorted(events["logins"], key=by_ts)
    ]
    return {
        "Activity": {
            "Video Browsing History": {"VideoList": video_list},
            "Favorite Videos": {"FavoriteVideoList": fave_list},
            "Search History": {"SearchList": search_list},
            "Login History": {"LoginHistoryList": login_list},
        },
        "Profile": {
            "Profile Information": {
                "ProfileMap": {
                    "userName": "synthetic-demo-donor",
                    "bioDescription": "Synthetic demonstration data — not a real person.",
                }
            }
        },
    }






def build_raw_scrape_frame(items: list[dict], as_of: dt.datetime) -> pd.DataFrame:
    """One raw-named scrape row per item (the shape ``fetch()`` would return).

    Uses the RAW TikTok column names so the real canonicalization path
    (rename -> per-K rates -> plays_per_day -> sv_ stamp) does all derivation.
    ``video_downloaded`` is False: there is no media, the viewer shows its
    honest not-downloaded notice, and the annotation queue builder can never
    pick demo items up for a real (DNF-doomed) Gemini run.
    """
    scrape_ts = pd.Timestamp(as_of)
    rows = []
    for it in items:
        rows.append({
            "item_id": it["item_id"],
            "createTime": pd.Timestamp(it["create_time"].replace(tzinfo=None)),
            "last_modified": scrape_ts,
            "video_duration": it["duration"],
            "desc": it["desc"],
            "author_id": it["author_id"],
            "author_nickname": it["author_name"],
            "author_uniqueId": it["author_handle"],
            "author_signature": "Synthetic demo account",
            "stats_playCount": it["play_count"],
            "stats_diggCount": it["fave_count"],
            "stats_commentCount": it["comment_count"],
            "stats_shareCount": it["share_count"],
            "stats_collectCount": it["save_count"],
            "music_id": str(zlib.crc32(it["music_title"].encode())),
            "music_title": it["music_title"],
            "music_authorName": "Demo audio",
            "music_album": "",
            "music_duration": min(60, it["duration"]),
            "music_original": it["music_title"] in ("trending audio", "meme sound"),
            "challenges": " | ".join(it["hashtags"]),
            "isAd": it["is_ad"],
            "IsAigc": False,
            "video_downloaded": False,
        })
    return pd.DataFrame(rows)






def build_annotation_response(it: dict) -> dict:
    """One structured annotation response conforming to the live contract."""
    niche = it["niche_def"]
    speech_pct = 70 if it["story_type"] in ("Issue-Based", "Event-Based") else 30
    transcript = (
        f"Today we're {it['activity']}. "
        f"{'Let me walk you through it step by step.' if speech_pct > 50 else ''}"
    ).strip()
    return {
        "transcript": transcript,
        "spoken_language": "English",
        "multilingual": "No",
        "objects": list(niche["objects"][:3]),
        "symbols_and_brands": ["DemoBrand"] if it["is_ad"] else [],
        "text_overlays": [it["activity"].capitalize()],
        "faces": [] if it["gender"] == "-" else [{
            "gender": it["gender"] if it["gender"] in ("Female", "Male", "Nonbinary") else "Female",
            "age_estimate": 24 + (int(it["item_id"][-2:]) % 30),
            "ethnicity": it["ethnicity"],
        }],
        "audio_summary": {
            "speech_vs_music": speech_pct,
            "background_music": it["music_title"] if it["music_title"] != "none" else "-",
            "notable_sounds": list(niche["sounds"][:2]),
        },
        "main_activity": it["activity"],
        "video_story": (
            f"A creator is {it['activity']} in a short vertical video themed "
            f"around {niche['key'].replace('_', ' ')}."
        ),
        "type_of_story": it["story_type"],
        "content_category": it["categories"],
        "primary_country": "Australia",
        "tiktok_native": "Yes",
        "trend_technical": "Yes" if it["trend_technical"] else "No",
        "trend_cultural": "Yes" if it["trend_cultural"] else "No",
        "advertising": "Yes" if it["is_ad"] else "No",
        "aigc": "No",
        "main_gender": it["gender"],
        "main_ethnicity": it["ethnicity"],
        "political_score": it["political"],
        "sensitivity_score": it["sensitivity"],
        "call_to_action": "Follow for more" if it["is_ad"] else "-",
    }






def build_annotation_entries(items: list[dict], as_of: dt.datetime,
                             annotation_version: str, model: str = "synthetic-demo") -> dict:
    """Raw machine-annotation entries in the shape ``call_machine`` writes."""
    inference_ts = int(as_of.replace(tzinfo=dt.timezone.utc).timestamp())
    entries = {}
    for idx, it in enumerate(items):
        entries[str(idx)] = {
            "item_id": it["item_id"],
            "source_platform": "tiktok",
            "inference_ts": inference_ts + idx,
            "inference_duration": 0,
            "model": model,
            "prompt_fn": "synthetic_demo_generator",
            "annotation_version": annotation_version,
            "structured": True,
            "usage": {},
            "error": None,
            "finish_reason": "STOP",
            "response": json.dumps(build_annotation_response(it)),
        }
    return entries






def generate(seed: int = DEFAULT_SEED, donors: int = DEFAULT_DONORS,
             days: int = DEFAULT_DAYS, as_of: str = DEFAULT_AS_OF,
             annotation_version: str = "av_unstamped") -> dict:
    """Build all demo artifacts in memory (pure — no storage side effects).

    Returns:
        {"donor_files": {filename: ddp_dict}, "collection_ids": [...],
         "scrape_raw": DataFrame, "annotation_entries": {...},
         "items": [...]}
    """
    rng = random.Random(seed)
    anchor = dt.datetime.strptime(as_of, "%Y-%m-%d")
    items = build_items(rng, anchor)

    donor_files: dict[str, dict] = {}
    collection_ids: list[str] = []
    played_ids: set[str] = set()
    for i, donor in enumerate(DONORS[:donors]):
        events = build_donor_events(rng, donor, i, items, days, anchor)
        donor_files[f"demo_participant_{donor['n']:02d}.json"] = donor_ddp_json(events)
        collection_ids.append(f"DEMO_P{donor['n']:02d}")
        played_ids.update(it["item_id"] for _, it in events["plays"])

    # Only items that actually surface in someone's feed need enrichment rows.
    played_items = [it for it in items if it["item_id"] in played_ids]
    scrape_raw = build_raw_scrape_frame(played_items, anchor)
    annotation_entries = build_annotation_entries(played_items, anchor, annotation_version)

    return {
        "donor_files": donor_files,
        "collection_ids": collection_ids,
        "scrape_raw": scrape_raw,
        "annotation_entries": annotation_entries,
        "items": played_items,
    }






def emit_to_directory(result: dict, outdir: str) -> None:
    """Write every artifact as plain files (inspection / JOSS examples path)."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for fn, doc in result["donor_files"].items():
        (out / fn).write_text(json.dumps(doc, indent=1))
    result["scrape_raw"].to_parquet(out / "demo_scrape_raw.parquet")
    (out / "demo_machine_annotations_raw.json").write_text(
        json.dumps(result["annotation_entries"], indent=1))
    print(f"[demo] Wrote {len(result['donor_files'])} donor files, "
          f"{len(result['scrape_raw'])} scrape rows and "
          f"{len(result['annotation_entries'])} annotation entries to {out}/")






def write_to_store(result: dict, tz: str = "Australia/Brisbane") -> None:
    """Install the artifacts into the configured data store via the real paths.

    Donor JSONs land in ``demo_raw`` with manifest entries (the same state a
    UI upload produces); the scrape batch goes through the genuine
    ``_canonicalize_recode_save``; the annotation JSON lands in
    ``machine_annotations_raw``. The admin then runs the normal Ingest
    refresh + Consolidate & Refresh from the UI.
    """
    import fyp.data_io as data_io
    from fyp.annotation import annotation_versioning
    from fyp.scrape.platform_scraper import get_scraper
    from fyp.scrape.scrape import _canonicalize_recode_save

    # 1. Donor files + ingestion manifest (mirrors the upload route).
    manifest_fn = "ingestion_manifest.json"
    manifest = {}
    if data_io.exists(storage_location="demo_raw", filename=manifest_fn):
        manifest = data_io.load_json(storage_location="demo_raw", filename=manifest_fn) or {}
    for i, (fn, doc) in enumerate(result["donor_files"].items()):
        data_io.save_json(data=doc, storage_location="demo_raw", filename=fn)
        manifest[fn] = {"collection_id": result["collection_ids"][i],
                        "tags": ["demo"], "tz": tz}
    data_io.save_json(data=manifest, storage_location="demo_raw", filename=manifest_fn)
    print(f"[demo] Uploaded {len(result['donor_files'])} donor files to demo_raw.")

    # 2. Scrape batch through the real canonicalize/recode/save path (real sv_).
    fine_ts = "20260630000000000000"
    _canonicalize_recode_save(result["scrape_raw"], get_scraper("tiktok"), fine_ts)
    print(f"[demo] Saved scrape batch scrapes_{fine_ts}.parquet.")

    # 3. Annotation raw file, stamped with the genuinely active av_.
    annotation_versioning.ensure_active_version_registered()
    active = annotation_versioning.active_annotation_version()
    for entry in result["annotation_entries"].values():
        entry["annotation_version"] = active
    from fyp.annotation.machine_annotation import _machine_annotations_label
    ann_fn = f"{_machine_annotations_label()}_20260630000000000001.json"
    data_io.save_json(data=result["annotation_entries"],
                      storage_location="machine_annotations_raw", filename=ann_fn)
    print(f"[demo] Saved {len(result['annotation_entries'])} annotation entries "
          f"to {ann_fn} (version {active}).")
    print("[demo] Next: DM -> Ingestion -> Refresh, then Consolidate & Refresh, "
          "then define the demo study over " + ", ".join(result["collection_ids"]))






def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic demo dataset.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--donors", type=int, default=DEFAULT_DONORS)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF,
                        help="Anchor date (YYYY-MM-DD); fixed default keeps output deterministic.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-only", metavar="OUTDIR",
                      help="Write artifacts as plain files to OUTDIR (no data store).")
    mode.add_argument("--write", action="store_true",
                      help="Install into the configured data store via data_io.")
    args = parser.parse_args()

    result = generate(seed=args.seed, donors=args.donors, days=args.days, as_of=args.as_of)
    if args.emit_only:
        emit_to_directory(result, args.emit_only)
    else:
        write_to_store(result)




if __name__ == "__main__":
    main()
