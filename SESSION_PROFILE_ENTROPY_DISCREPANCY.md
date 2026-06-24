# Session Profile — Entropy Reproduction Discrepancy

**Date:** 2026-06-04 (afternoon session, resuming the Session Profile tab build)
**For:** the session that produced the ground-truth `paper_three_session_metrics.parquet`
**Status:** BLOCKING the worker build — need to know how the entropy column was actually produced.

---

## TL;DR

When rebuilding the begin→end finding from scratch with the **committed** modules
(`fyp.niche_detection.detect_niches` → `fyp.session_profile.build_session_metrics`
→ `compute_profile`), **every headline number reproduces exactly except niche
entropy**, which collapses from the established **+0.044 (97% of participants up)**
to **+0.004 (~55% up)** — i.e. a near-null. I ran **11 separate experiments** to
recover it and none reach the established magnitude. The assembly is provably
correct (sessions, videos, and segment sizes are byte-for-byte identical to the
ground truth). The signal that's missing is specifically **late-segment niche
diversity**.

I need the establishing session to confirm *exactly* what was fed to
`detect_niches` and whether `build_session_metrics` (segment / `feed_position`
definition) was changed between writing the ground-truth file and committing.

---

## What reproduces perfectly

Ground truth = `cache/paper_three_session_metrics.parquet` (mtime **2026-06-04 10:12:16**).
Commit `70b3d13` (adds `fyp/session_profile.py`) is **2026-06-04 10:17:18** — i.e. the
ground-truth file was written **~5 minutes before** the module was committed.

| metric | my reproduction | ground truth | verdict |
|---|---|---|---|
| completion Δ (late − early) | −0.127 | −0.124 | ✅ |
| dwell Δ | −3.20 | −3.24 | ✅ |
| % sessions narrowing | 24% | 24.7% | ✅ |
| n_sessions in band | 14,783 | 14,802 | ✅ (99.9%) |
| n_participants | 64 | 64 | ✅ |
| **n_annot / session** | mean 27.67 | mean 27.74, **corr = 1.000** | ✅ identical |
| completion_early (per-session corr) | — | **corr = 1.000** | ✅ identical segments |
| **entropy Δ** | **+0.004 (~55% up)** | **+0.044 (96.9% up)** | ❌ |
| entropy_early | 1.997 | 2.003 (corr 0.978) | ≈ |
| entropy_late | 2.001 | **2.048** (corr 0.964) | ❌ |
| top_share Δ | −0.001 (flat) | −0.0065 (76% down) | ❌ |

**Interpretation:** `n_annot` corr = 1.000 and `completion_early` corr = 1.000 prove
the band, the session membership, the per-video data, and the *early* segment are
identical to the ground truth. So the assembly recipe is right. The discrepancy is
isolated to **niche diversity in the late segment**: ground truth says late videos
spread across ≈7.76 effective niches (exp 2.048); mine says ≈7.40 (exp 2.001), the
same as early.

**Suspicious clue:** `completion_late` per-session corr is **0.973**, not 1.000,
even though `completion_early` is exactly 1.000. `completion` is niche-independent
(it's `play_duration/video_duration`), so a < 1.0 correlation in the *late* segment
hints that **late-segment video membership differs slightly** between the two runs —
pointing at the `feed_position` / segment-boundary definition, not the niches.
(Could also be variance noise — late completions are lower and noisier — but worth
checking.)

---

## My assembly recipe (the one being validated)

Per study, on the FULL intact data (NOT the sampled `cache/{study}_recoded.parquet`):

1. Load `recoded/collections_recoded.parquet`, filter to the study's
   `SELECTED_COLLECTIONS`, keep `activity_type ∈ {play, observe}`.
   (4.26M `play` rows; only 13k `observe` — essentially DDP.)
2. Merge `recoded/scrapes_recoded.parquet` on `item_id` →
   `video_duration, author_id, stats_playCount` **+ text fields
   `desc_hashtags, desc_not_hashtags, music_title`** (these live in scrape, not
   annotation — easy to miss).
3. Merge `recoded/machine_annotations_recoded.parquet` on `item_id`, filtered to
   `annotated_ok == True` → content features + niche text fields
   (`video_story, main_activity, objects, text_overlays, symbols_and_brands,
   transcript_no_repetitions`).
4. `dwell = play_duration`; `completion = play_duration / video_duration`;
   `log_playcount = log1p(stats_playCount)`.
5. `feed_position = cumcount()` per `collection_id`, sorted by
   `(collection_id, utc_timestamp)` (mergesort).
6. `detect_niches(annotated_rows, n_niches=150)` (fits on unique `item_id`,
   assigns per impression), mapped back by `item_id`. `session_id` = the
   persistent 900s id already backfilled into `collections_recoded.parquet`.
7. `build_session_metrics(df)` (band 12–80 annotated/session; early/late thirds).

The `annotated_ok == True` filter is what aligned n_sessions to 14,783 (≈14,802)
and made completion/dwell match to 3 decimals — strong evidence this recipe is
the intended one.

---

## Experiments run to recover entropy +0.044 (all failed)

All on `paper_three`, entropy Δ reported (ground truth = **+0.044, 97% up**):

| # | variant | entropy Δ | % up | notes |
|---|---|---|---|---|
| 1 | per-study K=150, no `annotated_ok`, no scrape text fields | −0.000 | 53% | 15,357 sessions |
| 2 | global niches K=150 + `annotated_ok` | +0.002 | 53% | 14,783 sessions |
| 3 | per-study K=150 + `annotated_ok` + scrape text fields | +0.002 | 53% | 14,783 sessions |
| 4 | global niches K=150 (full corpus, proper text) | +0.002 | — | entropy_late 1.992 vs gt 2.048 |
| 5 | per-study K=300 | +0.002 | — | early entropy rises, Δ does not |
| 6 | per-study K=400 | +0.002 | — | early 2.039 (overshoots gt 2.003) |
| 7 | per-study K=500 | +0.002 | — | — |
| 8 | impression-level fit (no dedup) | −0.000 | 55% | — |
| 9 | random_state ∈ {0,1,2,3,4} | +0.0005…+0.0033 | 53–58% | partition is NOT the cause |
| 10 | first-hashtag niches (41,765 niches) | +0.012 | 72% | but only 9,723 sessions (wrong) |
| 11 | hashtag-set niches (108,568 niches) | +0.004 | 75% | only 9,723 sessions (wrong) |
| 12 | rich fields (+content_category, type_of_story, scene_sentiments, notable_sounds) | +0.000 | 50% | 14,783 sessions |

**Conclusion:** entropy Δ is robustly ≈ +0.004 across *every* niche definition,
granularity, scope, seed, and field set tested. The established +0.044 is **not
achievable** via `detect_niches` on the current data with the committed
`build_session_metrics`. Because the ground-truth file was written 5 min *before*
the commit, the most likely explanation is that the **code that wrote it differs
from the committed code** (segment definition, `feed_position` derivation, or the
niche source).

### Segment structure (per-session means, my per-study K=150)

```
n_imp        early=9.222  late=9.562  (+0.340)   <- late slightly larger (segment rounding)
n_uniq_item  early=9.197  late=9.542  (+0.345)
n_uniq_niche early=8.482  late=8.815  (+0.333)   <- rises in proportion to n_imp → per-video diversity FLAT
repeat ratio early=1.0034 late=1.0027            <- essentially no replays; rewatch is long dwell, not dup rows
```

So in my pipeline, late videos are *no more diverse per video* than early. In the
ground truth they clearly are.

---

## What the establishing session needs to confirm

1. **What was fed to `detect_niches`?** Exact corpus (per-study? global? which
   collections?), the field set / weights, whether `annotated_ok` was applied,
   the row **order**, and whether niches were freshly detected or loaded from a
   cached artifact. (Is there a saved `*_niche*.parquet` / item→niche map from
   that run? I found none in `cache/` or `recoded/`.)

2. **How was `feed_position` derived?** Activity filter (play-only? play+observe?
   all activity?), per-`collection_id` vs per-`session_id`, tie-breaking on equal
   `utc_timestamp`, and the **denominator** in the segment fraction. The committed
   `_segment_label` uses `rank / (n-1)`; if the ground-truth code used `rank / n`
   or a different `SEGMENT_FRACTION`, the *late* boundary shifts more than the
   early one — which would explain `completion_late` corr 0.973 vs
   `completion_early` corr 1.000.

3. **Was `build_session_metrics` changed between 10:12 and 10:17?** Especially the
   entropy computation and the early/late segment assignment. The committed
   version computes Shannon entropy over `value_counts(normalize=True)` of the
   segment's impressions. Did the ground-truth code compute entropy over **unique
   videos** instead of impressions, or use a different niche column, or a
   bias-corrected estimator?

4. **Was the niche assigned per-impression or per-unique-video, and from which
   text?** Specifically, did late-session videos get *finer* niche resolution
   somehow (e.g. a two-stage niche model, or sub-clustering)?

5. **Does dmrc_summer_mini still reproduce?** The memory says dmrc gave +0.045 at
   100%. If the establishing session can re-run dmrc with its original code and
   still gets +0.045, but my committed pipeline gives ≈+0.004 there too, that
   pins the gap to the code path, not the data.

---

## Reproduction harness (committed to `tests/`)

All scripts assume `source .fypenv314/bin/activate`. They cache intermediates in
`cache/_repro_*` to make iteration fast (delete those when done).

- `tests/repro_session_profile.py [study] [--global]` — end-to-end repro + sanity print.
- `tests/repro_assemble_cache.py [study]` — assemble full annotated df once → `cache/_repro_assembled_{study}.parquet`.
- `tests/build_global_niche_map.py [K]` — fit niches on the full annotated corpus → `cache/_repro_global_niche_K{K}.parquet`.
- `tests/repro_diff.py [perstudy|global|content_category|allrows] [K]` — per-session diff vs ground truth (the corr table above).
- `tests/repro_diff2.py [impressions|unsorted]` — impression-fit / shuffled-order variants.
- `tests/repro_seedsweep.py` — entropy Δ across random_state 0–4.
- `tests/repro_hashtag.py [first_hashtag|hashtag_set]` — hashtag-based niches.
- `tests/repro_richfields.py` — expanded niche-document field set.
- `tests/repro_diag.py` — per-session segment structure (impressions / unique items / unique niches, early vs late).

Quickest diff to see the gap (after building the two caches):

```bash
python tests/repro_assemble_cache.py paper_three      # ~25s after the big loads
python tests/repro_diff.py perstudy 150               # prints the corr table
```

Environment: Python 3.14 (`.fypenv314`), scikit-learn **1.8.0**, same data files
as the establishing run (annotation/scrape/collections unchanged — `n_annot`
corr = 1.000 confirms the per-session annotated video sets are identical).

---

## Bottom line for the tab build

The **satiation** finding (completion/dwell ↓) and the **rabbit-hole-session**
finding (~25% of sessions narrow) reproduce exactly and are safe to feature. The
**feed-diversification** (entropy ↑) finding does **not** reproduce with the
committed code. Pending the establishing session's answer, the tab will be built
on the committed pipeline (honest numbers) with diversification presented as the
weak/near-null effect it currently computes to — and revised if the establishing
session identifies the missing step.
