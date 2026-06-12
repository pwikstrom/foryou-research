# Low-entropy viewing sequences in TikTok watch histories — v1 findings

*Working draft, 2026-06-09. Companion to `RESEARCH_DESIGN.md` (design,
definitions, threat model) and `README.md` (scripts). All analyses are
standalone research code under `experiments/`; nothing ships to production.*

## Abstract

We ask whether TikTok users experience *low-entropy sequences* — stretches of
viewing in which the content is semantically homogeneous — measuring homogeneity
directly on dense 1536-d embeddings of each video (`gemini-embedding-001` over
annotation + scrape text), not on topic labels or a 2D projection. Across 73
donated watch histories we find that focused "binge" episodes are **real**
(78% of donors have at least one episode beyond their own time-shuffled chance
baseline; the chance baseline is ~0.02 episodes per donor), **near-universal but
marginal** (they occupy a median ~0.06% of plays / ~0.2% of watch-time),
**short** (median 5 distinct videos over ~6 minutes), and **benign** in
content. What gets binged is a property of the content itself, not just of the
diet: how-to/review/serial formats are 2–6.5× over-represented in episodes
relative to the same donors' overall diets (beauty reviews 6.2×, Christian
sermons 6.5×, recipes 5.2×), while **political content is the one tested niche
that is *not* disproportionately binged** (1.29×, n.s.), and episodes are if
anything *less* political than the same person's diet (paired p≈0.049). Nearly
half of episodes are dominated by a single creator — vastly above the ~0.1%
expected by chance — suggesting creator loyalty, not algorithmic topic
amplification, drives much of the phenomenon. Across all ten segmentation
specifications we find **zero "directed drift" episodes**: focused viewing means
sitting in a topical cluster, never being led along a semantic path. The
classic rabbit-hole narrative — algorithm leads user progressively deeper into
ever-narrower, politically charged content — is absent from these data at the
minutes-to-hours timescale; what exists instead is a brief, recurrent,
commercially flavoured *topical dwell*. Completing the picture: bingeing is a
**habit** (the same person re-binges the same niche at ~10-day intervals, 6×
chance), belongs to slow, deliberate, narrower-taste viewers rather than heavy
scrollers, is **absorbing but terminal** (dwell rises during an episode, yet
sessions end sooner after one — a finale, not a trap), and gives only a
**faint warning** before it starts (16.7× lift at a 0.05% base rate; the
strongest precursors are the user's own recent fave/follow/search actions).

## 1. Background and question

Prior work in this programme found that within a sitting, engagement satiates
while feed *composition* stays stable (no diversification, no narrowing), and
that dwell does not predict next-window content. Those results used discrete
niche labels or session-level aggregates. The present study asks the
finer-grained question: **do focused, low-entropy stretches exist at all — and
if so, how common are they, what are they like, and what drives them?**
Homogeneity is computed on the raw embedding geometry so that within-topic
structure is retained.

## 2. Data

- **Activity**: `collections_recoded.parquet`; 4.78M rows, 133 collections;
  per-play `local_timestamp` (100% coverage), `play_duration` (capped 600 s),
  persistent 900 s-gap `session_id`, `activity_type`.
- **Embeddings**: 260,196 annotated videos, `gemini-embedding-001` @1536
  (Matryoshka), embedded from labelled annotation+scrape documents (author
  identity deliberately excluded). Corpus-mean-centred, L2-normalised before
  any distance is computed (the model is anisotropic).
- **Per-video features** (for characterisation only, never for segmentation):
  embedding-derived niche (150 KMeans clusters, Gemini-named), human
  content-category, annotation scalars (political, sensitivity, advertising,
  AIGC), and scrape `author_uniqueId`.
- **Population**: all donated personal watch histories (named and anonymous;
  both are real donors). Excluded: 8 observe-only baseline collections.
  **Analyzable: n = 73** (coverage ≥ 0.15, ≥ 5,000 plays, ≥ 30 days, ≥ 1,000
  embedded plays). **High-coverage stratum: n = 13** (coverage ≥ 0.40 within
  the analyzable set). Median embedding coverage of an analyzable donor's plays
  is ~25%; coverage is a moderator throughout and all prevalence statements are
  **conditional on observability**.

## 3. Methods

**Focus measure.** Mean pairwise cosine distance among a set of videos'
directional embeddings (low = homogeneous). Spectral (von Neumann) entropy of
the window Gram spectrum is reported as a secondary measure; raw entropy bits
are bounded by log₂(n) and are never used for ranking.

**Episode segmentation** (`build_episodes.py`). Within each session, in time
order over distinct embedded videos: grow the current episode while the next
video's cosine distance to the centroid of the last 6 members is ≤ **0.5**;
close otherwise. Keep episodes with ≥ 4 distinct videos spanning ≥ 3 minutes.
Repeat plays extend an episode's span but are not new members (a rewatch loop
cannot fake a binge). Session boundaries hard-break episodes.

**Inference.**
1. *Existence (RQ1)*: per-donor permutation null — scramble which video
   occupies each play slot across the donor's history (timestamps and session
   structure fixed), re-run the identical segmenter, 200 permutations;
   BH-FDR across donors.
2. *Character (RQ5–7)*: matched-draw enrichment — for every episode, draw the
   same number of distinct videos from the same donor's distinct embedded diet
   (500 draws); compare observed niche counts, dominant-author share and
   valence against the draw distributions; Wilcoxon paired tests across donors
   for valence.
3. *Robustness*: a 10-point specification grid (cut 0.4/0.5/0.6 × centroid
   memory 3/6/12 + a stricter min-length variant), human calibration of the
   cut on a stratified 20-episode sample, a high-coverage-stratum replication,
   and an earlier window-based pilot (fixed tumbling and event-anchored sliding
   windows, 15–120 min) that the episode results must and do agree with.

**Two artifact guardrails** (both changed results during development and are
retained): dedupe of repeated plays (a rewatch loop collapsed the spectrum and
produced a spurious p = 0.005 for one donor, p = 0.47 after dedupe), and
never ranking on absolute entropy bits (which surfaces the smallest windows,
not the most focused).

## 4. Results

### 4.1 Low-entropy sequences exist (RQ1)

60/73 donors (82%) have ≥ 1 episode; **57/73 (78%) are significant against
their own time-shuffled baseline after BH-FDR**. The chance baseline is
near-zero — random orderings of the same diets produce a mean of **0.02
episodes per donor** (max 0.18) — so the segmenter's false-positive rate under
temporal randomness is effectively nil and detected episodes are genuine
temporal clustering. (Equivalently: the question "beyond chance?" is settled
trivially at this cut; the informative axes are intensity and character.)

An earlier window-based pilot agrees: in a 20-donor panel, 5/20 donors showed
60-min windows more focused than their own shuffled null (BH-FDR); a robust
core of 3 donors replicated under both tumbling and event-anchored sliding
windows and at every window width from 15 to 120 minutes; and marginal hits
that failed cross-method replication were correctly discarded.

### 4.2 Near-universal but marginal (RQ3, RQ4)

Prevalence across donors is **definition-dependent in a bounded way**: 59% /
82% / 92% of donors have ≥ 1 episode at cut 0.4 / 0.5 / 0.6. At the calibrated
0.5 cut: 82%, with 45% having ≥ 3 episodes and 16% having ≥ 10 (max 75).

Within donors, episodes are a sliver of viewing: median **0.06% of plays**
(0.19% of embedded plays) and **~0.2% of watch-time** among donors with
episodes; the 90th-percentile donor reaches only ~1.3% of watch-time. In the
high-coverage stratum the exposure estimate roughly triples (0.17% of plays,
0.52% of watch-time) — consistent with low coverage censoring episodes — but
remains well under 1%. **Bingeing is something nearly everyone does and almost
no one does much of.**

Episode *intensity* varies enormously across donors (1–75 episodes), and
high-coverage donors yield ~2.6× more episodes per embedded play (1.13 vs 0.44
per 1,000), so the true rates are likely underestimates throughout.

### 4.3 Short topical dwells, not directed descents (RQ2)

Episodes are short: median **5 distinct videos over ~6 minutes** (p90: 11
videos, ~15 min), with repeat_rate ~1.0 (binges are about *adjacent similar
content*, not rewatching). Geometrically, episode straightness (net semantic
displacement ÷ path length) has median 0.24, and **no episode in any of the
ten specifications classifies as a directed drift** (large diameter traversed
in a directionally consistent path). The "led progressively deeper" rabbit
hole does not appear; what exists is *dwelling* in a tight semantic
neighbourhood, then leaving.

### 4.4 Bingeability is a content property (RQ5)

Against matched draws from each donor's own diet, 14 of the 15 most frequent
episode niches are significantly over-represented (BH-FDR), with large ratios:

| niche | obs | exp | ratio |
|---|---|---|---|
| Christian Sermons | 43 | 6.6 | **6.5×** |
| Beauty Product Reviews | 235 | 38.1 | **6.2×** |
| Product Reviews | 57 | 10.2 | **5.6×** |
| Recipe Tutorials | 191 | 36.8 | **5.2×** |
| Travel Spotlights | 55 | 14.5 | 3.8× |
| Personal Care Tutorials | 131 | 35.0 | 3.7× |
| Financial Strategies | 58 | 16.4 | 3.6× |
| … | | | |
| **Trump Era Politics** | 39 | 30.1 | **1.29× (n.s., p = 0.07)** |

The enriched niches share a *format*: instructional, review-like, serial —
content where one video naturally invites the next. The single tested niche
that is **not** enriched is the political one: donors watch politics, but they
do not binge it beyond what its diet share predicts.

### 4.5 Nearly half of binges are creator loyalty (RQ6)

48.3% of episodes are dominated (≥ 50% of members) by a single creator;
matched draws produce author-dominated sets **0.1%** of the time (p = 0.002;
median dominant-author share 0.4 vs 0.2 expected). Author identity was
deliberately excluded from the embedding documents, so this is not an artifact
of the representation — though it is partly mechanical (one creator's videos
are genuinely similar, and the segmenter selects on similarity), so we read it
as an upper bound with a large margin: much of what presents as topical
bingeing is **following a creator**, a user-anchored mechanism, rather than
cross-creator topic amplification. Stationary (tight) episodes skew
single-creator (median author share 0.60) while looser wandering episodes are
cross-author (0.29).

### 4.6 Binges are, if anything, less political than the person's own diet (RQ7)

Paired within donors, episode content is *less* political than diet content
(median episode 0.016 vs diet 0.067; paired Δ −0.04; Wilcoxon p = 0.049 —
borderline, reported as suggestive) and indistinguishable on sensitivity
(p = 0.56). Together with §4.4, two independent tests point the same way:
**no doomscroll signature**. The observed binge is overwhelmingly
lifestyle/commercial.

## 5. Robustness

- **Specification curve** (10 specs): *short*, *marginal exposure*,
  *apolitical*, *zero directed drift* and *~half creator-loyalty* hold across
  the entire grid. Centroid memory is irrelevant (3 ≈ 6 ≈ 12). Only headline
  that moves materially: prevalence (59–92%), reported as a range.
- **Human calibration**: a stratified 20-episode sample (tightest / typical /
  near-cut) was reviewed by the PI; near-cut episodes were judged genuinely
  borderline rather than noise, licensing 0.5 as the headline cut.
- **High-coverage replication** (n = 13, coverage ≥ 0.40): 77% ≥ 1 episode,
  69% FDR-significant, median 6 videos / 6 min, 48% author-dominated, median
  political 0.0 — the full result pattern reproduces where observability is
  best.
- **Cross-method agreement**: the window pilot's robust donors are the episode
  table's most prolific bingers with matching topics (e.g. the beauty-review
  donor: 75 episodes, 29 of them beauty); its non-replicating marginal hits
  produce zero episodes.

## 6. Limitations

1. **Coverage / observability.** Only 15–77% (median ~25%) of an analyzable
   donor's plays are in the embedded corpus; un-annotated (often non-English)
   binges are invisible, runs are broken by unembedded interleaves (51% of
   episodes have more interleaved unembedded plays than members), and
   detection scales with coverage. All rates are lower bounds, conditional on
   observability.
2. **Realised exposure only.** We observe what was served *and* watched — not
   the algorithm's candidate pool or intent. No causal claims about "the
   algorithm" are made; the creator-loyalty result in particular cannot
   separate user choice (following, profile visits) from algorithmic
   same-creator sequencing.
3. **Mechanical component in RQ6** (similarity selection favours same-creator
   runs), uniform (not play-weighted) draw urns in RQ5/6, and a borderline
   p = 0.049 in RQ7 — each stated where used.
4. **Sample.** 73 donated histories (Australian-skewed DDP exports capped at
   ~183 days) are not a representative user population.
5. **Timescale.** Episodes are defined within sessions at the minutes scale;
   slow cross-day narrowing would not appear here (prior work in this
   programme found day-scale composition stable, but that is a separate
   analysis).

## 7. Phase 2 — who binges, whether it's a habit, and what a binge does to the session

### 7.1 The binger profile: slow, deliberate, narrower-taste — not the heavy scroller (RQ8)

Donor-level correlates of binge intensity (episodes per 1,000 embedded plays;
n = 73; Spearman, coverage-partialled): the strongest correlate is **baseline
dwell** (mean seconds per video; partial r = +0.32, p = 0.006) — people who
watch each video longer binge more. **Diet diversity** is negative
(r = −0.25, p = 0.04): narrower overall taste, more binges. And **volume is
negative** (r = −0.25, p = 0.03): heavier users binge *less* per play.
Active-engagement propensity (faves/follows/comments/searches per play) is
null. The picture is consistent: bingeing belongs to the slow, absorbed,
somewhat narrow viewer — not to the high-volume scroller. (Exploratory; modest
effect sizes; n = 73.)

### 7.2 Bingeing is a habit (RQ9)

Among the 47 donors with ≥ 2 episodes, **13.8% of a donor's episode pairs
share the same dominant niche, versus 2.2% expected** when episode niches are
permuted across donors (6.3×, p = 0.001). The median gap between same-niche
episodes is **~10 days**, and 45% of multi-episode donors have a single modal
niche covering at least half their episodes. The binge is not a one-off
collision with the feed; it is a *recurring appointment* with a personal topic
— which fits the creator-loyalty mechanism of §4.5.

### 7.3 Binges are finales, not traps (RQ10)

Two paired contrasts within donors:

- **During** an episode, dwell is *higher* than in the same session outside it
  (median 32.8 s vs 27.2 s per video; Wilcoxon p < 10⁻⁵): binges are genuinely
  absorbing, not zombie scrolling.
- **After** an episode, the session has *less* time left than at the same
  elapsed point in the donor's binge-free sessions (median 10.5 vs 17.8
  minutes; paired Δ = −6.6 min; p = 0.001; eligibility matched so longer
  sessions cannot mechanically explain it).

So the binge looks like a **session finale**: a stretch of absorbed viewing
after which the user winds down — the opposite of the "time-trap" narrative.
Causal direction is, however, not identified: the same numbers are consistent
with binges *causing* satiated exits and with binges *clustering* near natural
session ends (e.g. settled, end-of-evening viewing). Either way, sessions that
contain a binge do not run longer after it.

## 8. Phase 3 — how much warning does a binge give? (RQ11)

For every embedded play with ≥ 5 embedded predecessors in its session
(501,131 candidates; 254 onsets; base rate 0.05%), logistic models predicted
"an episode starts at this play" from the preceding window only, with a
per-donor time-ordered 70/30 split. Two feature families: **momentum** (the
semantic state of the last five videos — is the stream already narrowing?) and
**precursors** (dwell level/trend, position in session, hour, same-creator
streak, fave/follow/search in the last 10 minutes).

| features | AUROC | lift @ top 1% | PR-AUC ÷ base |
|---|---|---|---|
| momentum only | 0.74 | 13.7× | 6.7× |
| precursors only | 0.65 | 9.8× | 4.3× |
| both | **0.78** | **16.7×** | **10.0×** |

The honest reading is double-edged. **Relatively**, the signal is real and
substantially better than chance — and prior results in this programme
(dwell does not predict next-window content) made the precursor signal in
particular *not* a foregone conclusion. **Absolutely**, it is useless as an
alarm: at a 0.05% base rate, even 16.7× lift means ~99% of flagged moments are
false positives. The coefficient pattern tells a coherent story aligned with
everything above: onsets are preceded by an *already-narrowing* stream
(momentum), *longer dwell* (the absorbed-viewer state), a *same-creator
streak*, and a *recent deliberate action* (fave/follow/search) — the
user-initiated signature again, not an algorithmic ambush.

Caveats: 135 of 389 episodes (35%) begin within the first five embedded plays
of their session and are excluded for lack of a lookback window — binges
disproportionately start *early* in sessions, and predicting those is
out of scope here; momentum is partially self-announcing by construction
(measured, which is why the ablation separates it); linear models only.

## 9. Interpretation

These results extend the programme's *persistence* theme to the finest
timescale yet — and Phase 2/3 complete the character sketch. The feed's
composition is stable across a sitting, dwell does not steer the next window,
and even where genuine low-entropy runs exist they are brief, rare,
creator-anchored, concentrated in instructional/commercial formats — and
**habitual** (the same person returns to the same niche on a ~10-day cadence),
**absorbing but terminal** (higher dwell during, sessions end sooner after),
and **faintly self-announcing** (a real but practically unusable warning
signature: an already-narrowing stream, longer dwell, a creator streak, a
recent deliberate fave/follow/search).

The policy-salient rabbit-hole narrative — progressive, directed,
algorithm-driven descent into extreme or political content that traps the
user — finds no support at this timescale in these data, failing on each of
its components separately: not directed (zero drift episodes), not political
(the one non-enriched niche; binges less political than the person's own
diet), not a trap (sessions wind down after binges), and not an ambush (the
strongest precursors are the user's own deliberate actions). The binge that
actually occurs looks like *ten minutes of beauty reviews from a creator you
follow, ending the evening's scroll*. The open questions v2 should chase:
why are some users 75× more binge-prone than others beyond the profile
correlates found here, what the un-annotated (out-of-corpus) binges look like,
and whether the same anatomy holds on other platforms.

## 10. Reproducibility

All code in `experiments/embedding_entropy/` (build-excluded), all outputs in
`tmp/`. Pipeline: `profile_population.py` → `build_episodes.py` →
`describe_episodes.py` / `run_episode_null.py` / `run_spec_curve.py` /
`sample_episodes_for_review.py` / `test_base_rates.py` →
`phase2_analyses.py` (RQ8/9/10) → `phase3_prediction.py` (RQ11) →
`make_figures.py` (presentation PNGs). Window pilot: `run_window_entropy.py`,
`run_sliding_entropy.py`, `aggregate.py`, `select_donors.py`. Key outputs:
`episodes_v1.parquet`, `episode_donor_summary_v1.parquet`,
`episode_null_v1.json`, `spec_curve_v1.parquet`, `base_rates_v1.json`,
`phase2_v1.json`, `phase3_v1.json`, `collection_inventory.parquet`,
`episode_review_sample.md` (PI-annotated), and nine dark-theme 16:9 figures in
`tmp/figs_presentation/` (fig0 headline stat card; fig1 rarity; fig2 anatomy
of one real binge; fig3 niche enrichment; fig4 creator loyalty; fig5
beyond-chance; fig6 no-rabbit-hole geometry; fig7 habit + finale; fig8
predictability). Segmentation defaults: cut 0.5, mem 6, ≥ 4 distinct videos,
≥ 3 min, dedupe on, session hard-breaks; seeds fixed in-script.
