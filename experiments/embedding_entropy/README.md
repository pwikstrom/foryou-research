# Embedding-space windowed entropy

**Question.** Does any collection contain a window of *X* minutes (start 60) during
which the videos watched are semantically *homogeneous* — a focused binge —
where homogeneity is a property of the dense 1536-d `gemini-embedding-001`
vectors, **not** the discrete `niche` labels or the 2D t-SNE map?

These are standalone research scripts. `experiments/` is excluded from the
Docker/gcloud build; nothing here ships to production.

## Files
- `entropy_metrics.py` — the embedding-entropy measures (the core idea).
- `data_access.py` — local-parquet loaders (corpus mean, embeddings, plays, labels).
- `select_donors.py` — ranks collections by play volume × embedding coverage,
  writes a donor shortlist to `tmp/embedding_entropy_donors.txt`.
- `run_window_entropy.py` — fixed 60-min **tumbling** clock-bins; per-window
  metrics + permutation null → `tmp/`.
- `run_sliding_entropy.py` — event-anchored **sliding** `[t, t+W)` windows
  (most sensitive to tight bursts); fast incremental scan + timestamp-shuffle null.
- `aggregate.py` — pools a `*_summary.json` into a panel view with BH-FDR
  correction across donors.
- `profile_population.py` — collection inventory + unembedded-metadata ceiling
  (sets the population floors; see RESEARCH_DESIGN.md §10).
- `build_episodes.py` — **Phase 0**: segments binge *episodes* per donor session
  and writes the episode table (`tmp/episodes_*.parquet`) + per-donor summary,
  with binge-vs-drift geometry (`entropy_metrics.trajectory_geometry`) and
  content/author attributes. The spine for RQ1–RQ11.
- `describe_episodes.py` — descriptive readout of the episode table (RQ1–7 first
  answers).
- `run_episode_null.py` — **RQ1**: per-donor time-shuffle null on the episode
  count (videos scrambled across the donor's history, timestamps/session slots
  fixed, identical segmenter; BH-FDR across donors).
- `run_spec_curve.py` — re-segments the population under a grid of cut/mem/
  min-length specs and tracks every headline claim across the grid.
- `sample_episodes_for_review.py` — stratified ~20-episode sample rendered as
  `tmp/episode_review_sample.md` for human calibration of the focus cut.
- `test_base_rates.py` — formal enrichment tests: niche over-representation,
  author concentration, and valence vs matched random draws from each donor's
  own diet (RQ5/6/7).
- `phase2_analyses.py` — donor correlates of binge intensity (RQ8), niche
  recurrence/habit (RQ9), and session-retention after a binge (RQ10).
- `phase3_prediction.py` — episode-onset prediction with a momentum-vs-precursor
  ablation, per-donor time-ordered split, rare-event metrics (RQ11).
- `make_figures.py` — dark-theme 16:9 presentation PNGs →
  `tmp/figs_presentation/`.
- `build_deck.js` — assembles the figures into the talk deck
  (`tmp/figs_presentation/six_minutes_of_beauty_reviews.pptx`; full-bleed
  slides + speaker notes + closing limits slide). Run:
  `NODE_PATH=$(npm root -g) node experiments/embedding_entropy/build_deck.js`.

Documents, by audience:
- **`PAPER_DRAFT.md`** — plain-language, paper-shaped draft (intro → questions
  → data → methods → findings → discussion → limitations → appendices). The
  backbone for a journal submission and the readable version for non-experts.
- **`FINDINGS_v1.md`** — the condensed technical results companion (all
  numbers, tests, caveats).
- **`RESEARCH_DESIGN.md`** — the study design: question programme,
  operationalisations, threat model, phased plan, locked decisions.

## Run
```bash
source .fypenv314/bin/activate
python experiments/embedding_entropy/run_window_entropy.py            # 3 seed collections, 60-min, 21 days
python experiments/embedding_entropy/run_window_entropy.py --window-minutes 30 --min-emb 8
python experiments/embedding_entropy/run_window_entropy.py --no-dedupe   # let rewatches count
```
Outputs: `tmp/embedding_window_entropy_<tag>.parquet` (one row per window) and
`..._summary.json` (one row per collection).

## Method
1. **Windows.** Per collection take the densest contiguous *N*-day span (by
   embedded-play count), tile it into *X*-minute tumbling clock-bins, keep bins
   with ≥ `min_emb` **distinct** embedded videos.
2. **Geometry.** Corpus-mean-centre (remove the model's anisotropy) → L2-normalise.
3. **Entropy on embeddings** (three companion measures per window):
   - **Spectral / von Neumann entropy** `H = −Σ pᵢ log₂ pᵢ` over the normalised
     eigenvalues of the window's Gram matrix — the effective number of semantic
     *directions* the hour spans. The principled "entropy on the embeddings".
   - **Normalised entropy** `H / log₂(n)` — size-robust (absolute bits are
     bounded by `log₂(n)`, so raw bits just track window size — do not rank on them).
   - **Mean pairwise cosine distance** — the headline-interpretable "how unalike
     were the videos"; rank focus on this (low = homogeneous).
4. **Permutation null.** Hold bin sizes fixed, scramble which video lands in
   which bin, recompute. Tests whether similar content genuinely *clusters in
   time* (real bursts) vs. the donor merely being narrow overall.

### Two guardrails that mattered
- **Size confound.** Absolute spectral entropy ≤ `log₂(n)`, so sorting by it
  surfaces the *smallest* windows, not the most focused. Rank on cosine distance
  / normalised entropy; use raw bits only inside the size-matched null.
- **Rewatch artifact.** Repeated plays of the *same* video within a window
  collapse the spectrum (eff_rank→1) and deflate cosine distance, so a rewatch
  loop masquerades as a semantic binge. Default **dedupes** to distinct videos
  per window and reports `repeat_rate` separately. (This flipped cb8b3260 from a
  spurious `p=0.005` to `p=0.47`.)

## Result (20-donor panel, densest 21-day span, 60-min tumbling windows)
A first pass on 3 donors found ~nothing — but they were among the *least*
focused. Broadening to a 20-donor panel (coverage 0.15–0.75) flips the picture:

- **7/20 donors raw p<0.05, 5/20 survive BH-FDR (q<0.05)** on min cosine distance.
- Spectral-entropy min is less sensitive (2/20 raw, 0 FDR) — **cosine distance is
  the better focus detector**; rank/report on it.
- Top hits are genuine topical binges (dedupe on, repeat_rate 1.0): `edcfa1f1`
  cos_dist 0.29 = 6/8 Beauty Product Reviews; `4ca790d2` 0.48 = 8/8 dance;
  a named donation (cov 0.75) 0.51 = Swahili / African lip-sync + comedy.

**Focused hours are the exception but clearly real.** The median donor's tightest
hour is cos_dist ≈ 0.79 and the median *hour* ≈ 0.96 (near the orthogonal
baseline = diverse), so most viewing stays diverse — but a detectable minority
binge. This tempers, not overturns, the "composition is stable / persistence"
theme. Higher coverage ⇒ more detection power, so the rate is likely an
underestimate.

## Sliding windows + timescale sweep
Event-anchored `[t, t+W)` (run_sliding_entropy.py) replicates the tumbling
result and adds a robustness filter:
- Robust core **edcfa1f1 / the named donation / 633c9569** is FDR-significant
  under *both* windowing schemes and at *all* window sizes (15/30/60/120 min).
- Marginal tumbling hits (e.g. f10e0f10 ads-lean, p 0.02→0.70) do **not**
  replicate under sliding — clock-alignment quirks, not real bursts.
- FDR-significant donor count by window: **6 / 4 / 3 / 5** at 15/30/60/120 min →
  **15 min is the most sensitive single scale**; `cb8b3260` is significant only at
  15 min (a brief burst diluted by the hour).
- The binges are **tight contiguous runs of ~5–6 videos** (the sliding minimum
  sits at the floor `k≈5`, and cosine distance is flat across window widths) — a
  short focused run inside an otherwise-diverse hour, not a uniformly homogeneous
  60 min. (edcfa1f1 is also hour-scale: 8 distinct beauty videos at cos 0.29.)

## Caveats / next steps
- **Coverage 15–39 %** — out-of-corpus (mostly un-annotated) videos are invisible;
  a binge in an un-annotated niche would be missed. Biggest threat to the null.
- Only the densest 21-day span per donor; only 3 donors.
- **Clock-aligned tumbling** windows dilute binges straddling `:00`. The natural
  sensitivity upgrade is **event-anchored sliding** windows `[t, t+X)`.
- Try `--window-minutes` sweeps (15/30/120), `--weight` (watch-time), and a
  wider/poorer-coverage donor set.
