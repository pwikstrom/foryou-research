# Low-entropy viewing sequences in TikTok watch histories — research design note

*Draft for discussion. Standalone research; nothing here ships to production.*

## 1. Phenomenon & motivation

Do algorithmic feeds produce "rabbit holes" — stretches where a user is served
semantically homogeneous content? We operationalise homogeneity in the **dense
embedding space** (1536-d `gemini-embedding-001`), not topic labels, so we keep
the within-topic geometry that discrete niches throw away. A pilot on a 20-donor
panel established that focused 60-minute windows **do** occur beyond chance for
~25% of donors (BH-FDR on cosine distance), but they are **short tight runs**
(~5–6 back-to-back videos), not homogeneous hours, and the median hour stays
diverse. This note turns that pilot into a question programme and pins down the
definitional choices everything downstream depends on.

## 2. Data & unit of analysis

- **Plays**: `collections_recoded.parquet`, one row per play, 4.78M rows, 100%
  timestamped. Fields: `collection_id` (≈ one donor's watch history),
  `item_id`, `local_timestamp` / `utc_timestamp`, `play_duration` (capped 600 s),
  `session_id` (persistent 900 s-gap session), `activity_type`
  (`play`/`fave`/`following`/`comment`/`search`/`observe`/…), `data_source`.
- **Per-video** (join on `item_id`): `embedding` (1536-d; ~260k = the
  annotated corpus), `niche`/`niche_name`, `content_category`, annotation
  scalars (`political_score`, `sensitivity_score`, `advertising`, `aigc`,
  `trend`, `tiktok_native`, `speech_vs_music`, `faces_age_estimate`,
  `main_gender`/`main_ethnicity`, `australian_relevance`), `video_story`, and
  **author identity** (deliberately excluded from the embeddings, but available
  for analysis — see RQ6).
- **Collection-level**: `collections_metadata.parquet`, `collections_tags.json`,
  `studies.json`.
- **Population**: *all donated personal watch histories* — both named donations
  and UUID-id'd collections are real donors; the only exclusion
  is observe-only / no-watch-time **baseline** collections (a different data-
  generating process). Eligibility is set on **play volume and day-span**, not
  naming (floors set empirically from the inventory, §10).
- **Unit**: *episodes nested within donors*. Primary inference is **within-donor**
  (each donor is their own control via the time-shuffle null); prevalence and
  correlates are secondary, across-donor.

## 3. Core constructs (the load-bearing definitions)

| construct | definition | operationalisation |
|---|---|---|
| **Semantic focus** | how alike a set of videos is | mean pairwise cosine distance on corpus-mean-centred, L2-normalised embeddings (low = focused). Spectral/von-Neumann entropy secondary (size-sensitive). |
| **Window vs sequence** | unordered set vs ordered run | window-focus ignores order; sequence measures use order |
| **Binge episode** | a delimited maximal run of focus | segmentation, §4 — the discrete object everything counts/characterises |
| **Stationary binge vs directed drift** | cluster vs travelling run | within-episode mean consecutive-step cosine (local roughness) + diameter / net displacement; *straightness = net ÷ path* |
| **Relative vs absolute focus** | non-random vs experiential | relative: vs per-donor time-shuffle null; absolute: cosine below a fixed cut (e.g. 0.4–0.5). Report both. |
| **Coverage** | embedded fraction of a window/episode | `n_embedded / n_plays` — the dominant validity lever; report per episode |

## 4. Defining a binge episode (the step-zero decision)

Everything after RQ1 presupposes a discrete episode with a start, end, length and
topic. Candidate segmenters:

- **(a) Threshold-and-merge** — flag rolling windows (trailing *K* videos or *T*
  minutes) below a focus cut, merge contiguous flags into episodes. Simple, transparent.
- **(b) Change-point / 2-state HMM** on the embedding stream (focused vs diffuse).
  Cleaner statistically, heavier; defer to v2.
- **(c) Greedy run** — open an episode when the next video is within *d* of the
  running centroid, extend while it stays, close on a session gap or exit.

**Recommend (a) or (c) for v1** with explicit parameters — focus cut, min length
(videos *and* minutes), max intra-episode gap (a session break ends an episode),
dedupe — then a **specification curve** over them. Each episode emits:
`start/end`, length (videos / minutes / distinct), focus score, diameter,
straightness, dominant niche(s), `n_authors`, coverage, engagement (mean
completion & dwell), scalar profile, session position, and the immediately
preceding action. This **episode table** is the spine of every later analysis.

## 5. The question programme

*Their five questions are RQ1, RQ3–4, RQ5/7, RQ8, RQ11; the rest are additions.*

**A — Existence & shape**
- **RQ1 — Do low-entropy sequences occur beyond chance?** Per-donor time-shuffle
  null on episode focus; BH-FDR across donors; report relative *and* absolute.
  *Status: pilot yes (~25%), coverage-limited.*
- **RQ2 — Binge vs drift.** Are focused runs stationary clusters or directed
  drifts (the "led deeper" rabbit hole)? Distribution of episode straightness;
  classify episodes. *Probably the highest-value addition.*

**B — Prevalence**
- **RQ3 — How common across users?** % donors with ≥1 episode (FDR and absolute),
  coverage-stratified.
- **RQ4 — How common within users?** Share of watch-time / windows spent in
  episodes; is it concentrated in a few binge-prone donors or spread thinly?

**C — Character**
- **RQ5 — What are episodes about, and how long?** Length distribution; niche /
  category mix; which niches are *over-represented* vs base rate ("bingeability").
- **RQ6 — User- or algorithm-initiated?** Same-author vs cross-author within an
  episode (clean test — author was excluded from the embeddings); does an episode
  follow a `search`/`fave`/`following` or a long-dwell seed vs passive scrolling?
- **RQ7 — Engagement & valence during episodes.** Completion / dwell vs the
  donor's baseline (absorption vs satiation — ties to the robust satiation
  finding); political / sensitive / commercial profile vs baseline (doomscroll
  vs entertainment — the pilot binges were beauty/dance).

**D — Correlates**
- **RQ8 — Do binge-prone collections differ?** Model binge-propensity on
  collection properties **with coverage and volume as nuisance covariates** (or
  matched). Guard circularity: don't use overall diet-narrowness as both the
  predictor and the thing explained.

**E — Dynamics & prediction**
- **RQ9 — Recurrence / habit.** Do donors return to the same niche across days
  (stable obsession) vs one-off situational binges? Does a binge in niche X
  predict future binges in X?
- **RQ10 — Consequence / retention.** Does an episode predict session
  continuation / length vs drop-off (the time-well-spent angle)?
- **RQ11 — Predictability from the preceding stretch.** Within-donor time series:
  features of the preceding *K* windows → does an episode start? **Separate
  momentum** (entropy already declining — near-tautological) **from a distinct
  precursor signature** in the otherwise-diverse run before it. Rare-event
  evaluation (PR-AUC, time-ordered split). *Prior: the linger→feed null and
  session-stability findings temper expectations — the feed barely responds to
  behaviour — but the predictors here (trajectory momentum, a seed action) are
  different from dwell.*

**Validity / robustness (cross-cutting)**
- **RQ-V1 — Format vs topic.** Is a "semantic" binge sometimes just a sound /
  hashtag-trend cluster (the same audio challenge), not a topic?
- **RQ-V2 — Specification sensitivity.** Do answers survive the multiverse of
  segmentation parameters + relative/absolute + window size?
- **RQ-V3 — Coverage validation.** Do conclusions hold on the highest-coverage
  donors, and/or with a cheap fallback embedding (hashtags + music + sound) to
  lift coverage?

## 6. Threats to validity

- **Coverage (dominant).** Unembedded videos (the majority for most donors) break
  sequences and dilute/hide episodes. Treat coverage as a **moderator
  everywhere**: stratify by it, validate on high-coverage donors, report the
  per-episode unmeasured fraction, and consider a fallback embedding to raise it.
- **Proxy / counterfactual.** We observe *realised exposure* (served ∩ watched),
  not the algorithm's intent or what it would have served. Causal claims about
  "the algorithm" are out of reach; frame findings as descriptions of realised
  viewing. (Same caveat as the linger→feed work.)
- **Selection.** DDP donors are not a representative sample of users.
- **Detection power ∝ coverage × volume** → mechanically confounds prevalence
  (RQ3/4) and correlates (RQ8). Control explicitly.
- **Circularity** in RQ8 between "binge-prone" and "narrow overall diet."
- **Multiple comparisons** (donors × questions) → BH-FDR; pre-specify primary
  outcomes.
- **Definition-dependence** → specification curve (RQ-V2).
- **Known artifacts** (rewatch duplicates, entropy's size confound) → keep the
  dedupe + cosine-distance ranking + per-donor null guardrails from the pilot.

## 7. Analysis & statistics

- **Effect measure**: cosine distance (focus) primary; spectral entropy secondary.
- **Inference**: within-donor time-shuffle permutation null (preserves the diet
  and window occupancy, scrambles only timing) → across-donor BH-FDR.
  Mixed-effects models (donor random intercept) for episode-level outcomes nested
  in donors; coverage and volume as covariates throughout.
- **Prevalence**: hierarchical — user-level *and* exposure-level — coverage-
  stratified, donor-bootstrap CIs.
- **Prediction**: within-donor, time-ordered train/test (no leakage), PR-AUC +
  calibration, ablation isolating precursor vs momentum.
- **Multiverse**: specification curve over segmentation params + relative/absolute
  + window width.

## 8. Phased build plan

- **Phase 0 (unblocker)** — binge-episode **segmenter** + episode table + the
  binge/drift (straightness) metric. One parquet, one row per episode. Spine for
  all of the above.
- **Phase 1** — prevalence (RQ1/3/4) + character (RQ5/6/7). Fastest high-yield,
  most publishable.
- **Phase 2** — correlates (RQ8) + recurrence/consequence (RQ9/10).
- **Phase 3** — prediction (RQ11).
- **Cross-cutting** — validity battery (RQ-V1/2/3) run alongside.

## 9. Resolved decisions (defaults)

1. **Population** — all donated personal watch histories (named **and** UUID-id'd;
   both are real donors), `play` activity only; **exclude observe-only /
   no-watch-time baselines**. Floors fixed from the inventory (§10): **analyzable
   n = 73** (coverage ≥ 0.15, n_play ≥ 5,000, n_days ≥ 30, n_emb_play ≥ 1,000) =
   the prevalence denominator; **high-coverage validation n = 29** (coverage ≥
   0.40); coverage enters every analysis as a continuous moderator. Prevalence is
   stated conditional on observability, never as an unqualified population rate.
2. **Binge definition** — the **absolute cut delimits the episode** (primary
   cos-distance < 0.5; specification curve over {0.4, 0.5, 0.6}); the **per-donor
   time-shuffle null is the separate significance test** (RQ1). Min **4 distinct
   embedded videos** and **≥3 min**, dedupe on, and **`session_id` boundaries
   hard-break episodes**. **Calibration DONE (2026-06-09): PW reviewed the
   20-episode stratified sample (`tmp/episode_review_sample.md`) and judged the
   near-cut stratum genuinely borderline, not noise → the 0.5 cut is locked as
   the headline spec.** Spec-curve result: prevalence is the only cut-sensitive
   headline (59–92% across 0.4–0.6; 82% at 0.5); short/rare/benign/zero-drift
   hold across the whole grid, and `mem` is irrelevant.
3. **Unit** — **within-donor primary** (each donor their own control via the null);
   prevalence/correlates secondary, carrying the coverage/volume caveats.
4. **Coverage** — **stratify-and-caveat** for v1 + validate conclusions on the
   high-coverage donors. A metadata fallback embedding (hashtags/music/desc) is a
   **RQ-V3 robustness check only**, contingent on the unembedded-metadata ceiling
   (§10), and **never mixed into the primary gemini space**. Annotating the
   missing videos (the deferred 250k re-annotation) is out of scope here.
5. **v1 scope** — first writeup runs **RQ1 → RQ3/4 → RQ5 → RQ6**, with **RQ2
   (binge vs drift) riding along** (nearly free once the episode table exists).
   RQ7 (engagement/valence) is the immediate follow-up.

## 10. Profiling passes — results & locked floors

Run via `profile_population.py` (writes `tmp/collection_inventory.parquet`).

**Inventory (133 collections):** 8 observe-only/low-play baselines excluded →
**125 donor histories**. Coverage spread, not the feared bimodal: 22 donors
<0.15 (feeds largely outside the annotated corpus — *unobservable*, not
non-bingers), 103 ≥0.15, 29 ≥0.40. DDP exports cap at ~183 days (p50 span 182 d,
p50 21k plays).

**Locked floors:**
- **Analyzable population — n = 73** donors: `coverage ≥ 0.15 & n_play ≥ 5,000 &
  n_days ≥ 30 & n_emb_play ≥ 1,000`. Coverage enters every analysis as a
  continuous **moderator**.
- **High-coverage validation stratum — n = 29** donors: `coverage ≥ 0.40`.
- Prevalence is therefore stated **conditional on observability** ("among donors
  whose viewing is substantially within the annotated corpus"), never as an
  unqualified population rate.

**Unembedded-metadata ceiling:** 80.9% of plays are unembedded; of distinct
unembedded videos only **33%** carry *any* scrape record (the rest are bare
item_ids from the DDP — unrecoverable). A caption-based fallback could recover
~**38% of unembedded play volume**, lifting overall coverage from ~19% toward
~50% — meaningful but partial, and in a **separate, weaker text space**. Verdict:
keep the fallback as the RQ-V3 robustness check (Phase 2), **do not** gate v1 on
it; stratify-and-caveat + the n=29 high-coverage validation remains primary.
