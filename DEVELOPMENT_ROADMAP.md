# FYP Development Roadmap — H2 2026

_Drafted on return from holiday (2026-06-03). Companion to `POST_HOLIDAY_NOTES.md` (which
holds the deploy-verification checklist and engineering rough edges). This document is the
strategic plan; that one is the operational backlog._

---

## Context — why this plan exists

**The north star:** turn messy, personal *sequence* data from social platforms into meaningful,
defensible findings for HASS (humanities & social science) researchers. Today the tool does this
well for **one** platform (TikTok) and one broad style of analysis (category profiles, PCA,
ANOVA/PERMANOVA, timelines). Two things are missing to reach the vision:

1. **A sequence/temporal lens.** The data is fundamentally an *ordered feed* per participant, but
   almost all current analysis treats it as a bag of categorised videos. The headline new idea —
   _does how long a user lingers on videos in one stretch of feed predict the kind of videos they
   get served next?_ — is a sequence question the platform cannot yet answer.
2. **Platform generality.** "Messy personal sequence data from platforms" is plural; the ingestion
   layer is already abstracted for it, but no second platform has been built.

**Decisions taken (2026-06-03), which set this plan's shape:**

| Decision | Choice |
|---|---|
| Order of the three thrusts | **1) Linger→feed sequence feature → 2) Multi-platform → 3) Reliability/usability** |
| Linger feature approach | **Exploratory/descriptive first, then predictive modelling** |
| Data reality | **Mostly DDP/AIO** — dwell signal (`play_duration`) available across most of the corpus |
| Audience | **My own research now, self-serve for other HASS researchers later** — build depth now, don't paint into a corner |

**Current-state assessment (from a full codebase read):**

- ✅ **Solid:** clean tab pattern (template + JS + blueprint + `data_service`); a genuinely
  platform-agnostic ingestion ABC (`ForYouBaseCollection`); generic analysis stack
  (PCA/ANOVA/PERMANOVA/recoding); a working dual-mode (Cloud Tasks / subprocess) job framework;
  CSS-token design system; good docs in `CLAUDE.md`.
- ⚠️ **Fragile:** zero automated tests / no CI; no type checking (ruff only); Cloud Tasks have
  no retry / dead-letter; PCA cache has no freshness check; heavy frontend files
  (`timelines.js` ~2.5k lines, `collections.js` very large); `UserManager` bulk-loads all users
  on web cold start. (Details and several latent bugs already catalogued in `POST_HOLIDAY_NOTES.md`.)

---

## Phase 0 — Week 1 back: housekeeping (do before new work)

These are blocking/hygiene items, not new features. Most come straight from `POST_HOLIDAY_NOTES.md`.

- [ ] **Production verification sweep** — work through the unchecked boxes in
  `POST_HOLIDAY_NOTES.md` ("Things to verify end-to-end after deploy" + "Things to scan quickly on
  return"). The Cloud Tasks pipeline (`consolidate → recode → meta → pca → timelines`) is
  code-complete but never exercised on real Cloud Run data. Confirm it before building on top of it.
- [ ] **Resolve the in-flight config change.** `config/config.toml` currently has an uncommitted
  switch from `gemini-2.5-flash` → `gemini-3-flash-preview`, with a probe script in
  `tests/test_model_availability.py`. Run the probe, confirm the preview model is available and
  cost/quality acceptable, then either commit it deliberately or revert. Don't leave a preview model
  half-staged in prod config.
- [ ] **Scan Cloud Run logs** for failed-task volume and the recurring `503` dispatch mystery
  (diagnostics are now in `_dispatch_cloud_task`); confirm the RA's "Fetch from AWS" runs landed in
  GCS during the holiday.
- [ ] **Stand up a minimal test harness first** (small, ~half a day): add `pytest` and convert 2–3
  of the existing `tests/unit/` scripts to real tests. This is the seed for Thrust 3 and gives the
  sequence feature somewhere to put regression tests as it's built.

---

## Thrust 1 (Months 1–2) — Linger → Feed Sequence Analysis

**Research question.** For windows of a participant's feed, does dwell behaviour
(`play_duration`) in window _k_ predict the *kind* of content (content category / political /
sensitivity scores) served in window _k+h_, short-term (_h_=1) and medium-term (_h_≈2–4)?

**Feasibility (verified in code).** `play_duration` exists on DDP/AIO rows
([ingest.py:1335–1414](fyp/ingest.py:1335)) as a forward-delta dwell proxy capped at 600s; the feed
is already chronologically sorted; `completion_rate = play_duration/video_duration` is already
computed in `new_merge` ([organize_datasets.py:1264](fyp/organize_datasets.py)); prediction targets
(22 `content_category` labels, `political_score`, `sensitivity_score`) come from Gemini annotation.
The **gap**: no persisted `feed_position`/`session_id` (the 180s-gap `session_id` is computed then
dropped at [ingest.py:1332](fyp/ingest.py:1332)), and no windowing layer.

### Key design decision: derive sequence indices in the analysis layer, NOT in ingest

`feed_position`, `session_id`, and inter-event gap are cheap, deterministic functions of
`(collection_id, utc_timestamp)` — recompute them per-study. **Do not** add them to
`REQUIRED_COLUMNS`: that forces a full re-ingest and a stored-schema migration for a feature whose
window/session definition will change repeatedly during exploration. Keeping the window definition a
*tunable parameter* (not a frozen column) is exactly what an exploratory research tool needs.

### Window model

- **Window** = N consecutive viewing events (`play`/`observe`) within one session
  (default N=10, session gap 180s, both exposed as parameters). Numbered within `(collection_id, session_id)`.
- **Horizon _h_:** short-term = next window (_h_=1); medium-term = _h_∈2–4. Cross-session horizon is
  configurable, off by default (conservative).
- **Dwell binning per participant** (e.g. own tertiles → Short/Med/Long), never global cutoffs —
  absolute dwell scale varies by participant/device/timezone, and per-participant bins are
  leakage-safe.

### Increment 1 — Data + computation core (no UI)

- **New `fyp/sequence_analysis.py`** mirroring `fyp/timeline_analysis.py` (pure, dataframe-in /
  artifact-out, constants at top, no Flask/IO): `add_sequence_index()`, `build_windows()`,
  `compute_transition_lift()`. **Lift** (`P(next cat | dwell bin) / P(next cat)`) is the headline
  descriptive quantity because it controls for base rates.
- **New worker `web_interface/run_sequence_refresh.py`** mirroring `run_pca_refresh.py`: loads
  `{study}_recoded.parquet`, filters to viewing events, gates participants on dwell-coverage and
  min-windows, writes a per-study **`{study}_sequence.parquet`** (horizon-agnostic window frame) +
  **`{study}_sequence_summary.json`** (precomputed lift/transition grid + participant eligibility).
- **Pipeline registration (3 edits, same as timelines/pca):** add to `CLOUD_TASK_ELIGIBLE` and
  `processes` in `process_manager.py`; add lazy import + `TASK_FUNCTIONS` entry in
  `routes/process_routes.py`; optionally chain after PCA in `run_study_refresh.py` behind a flag.
- **Read cache:** `get_sequence_df()` / `get_sequence_summary()` in `data_service.py`, mirroring
  `get_pca_df()` with the mtime-check pattern.
- **Tests:** windowing, session boundaries, per-participant binning, leakage-safety.
- _Value:_ the artifact is inspectable/exportable even before any UI — immediately useful to you.

### Increment 2 — Exploratory tab (Stage A)

New `templates/tabs/sequence.html` + `static/sequence.js` + `routes/api_sequence_routes.py`, wired
into `fyp_data_hub.py`, `index.html`, `study_state.js` (`_GATED_TABS` + `has_sequence` flag set in
`get_accessible_studies`), and the permissions registry — all following the **Correlations** tab as
the closest precedent.

Visualisations (Plotly, already loaded):
1. **Dwell-bin → next-window category heatmap of _lift_** (the headline).
2. **Raw transition matrix** (toggle, like the correlations scatter/heatmap toggle).
3. **Per-participant spread** strip/small-multiples — shows whether the aggregate is driven by a few
   heavy donors (aggregate = *mean of per-participant lifts*, with `n_participants` shown, to avoid
   Simpson's paradox).

Controls: window size, horizon, target variable, participant filter (incl. "dwell-eligible only"),
session-gap (advanced). _Value:_ answers "is the signal real?" in-app — the gate to Stage B.

### Increment 3 — Predictive modelling (Stage B)

Only after Stage A shows descriptive signal. Lightweight, interpretable sklearn (already pinned):
multinomial `LogisticRegression` for category, `Ridge` for ordinal scores, optional shallow
decision tree for human-readable rules. Output `{study}_sequence_model.json` behind a `run_model`
task flag; a Stage B UI panel (baseline-vs-augmented bars with CIs, odds-ratio forest plot).

**The honest claim** = *incremental* skill of a dwell-augmented model over a baseline that already
knows current-window category + time-of-day + position-in-session (i.e. beating the "feed just keeps
serving the same thing" null). Report ΔAUC/Δlog-loss with bootstrap-over-participants CIs and a
within-participant permutation test.

### Methodological guardrails (must be enforced in code + surfaced in UI copy)

- **No leakage:** `GroupKFold(groups=collection_id)` — never row-level CV (windows autocorrelate);
  dwell bins fit on train folds only; never use ≥_k+h_ features.
- **Dwell only on DDP/AIO:** per-participant dwell-coverage gate + `has_sequence` flag; NA
  `play_duration` is never imputed to 0 (that would fabricate fast-skips).
- **Lift, not raw probability,** as the headline (base-rate control).
- **Multiple comparisons:** BH-FDR across the category × score × horizon × window grid.
- **Caveat surfaced in UI:** on DDP, `play_duration` *is* the inter-event delta — "dwell" conflates
  watch time with pauses/multitasking; this is observational data with confounds (time-of-day,
  stickiness, base rates, donor heterogeneity).

---

## Thrust 2 (Months 2–4) — Multi-platform generalisation

**Why it's tractable.** Ingestion is already an ABC: `ForYouBaseCollection` defines a
platform-agnostic `REQUIRED_COLUMNS` schema ([ingest.py:215](fyp/ingest.py:215)) and two abstract
methods; subclasses auto-register via `__init_subclass__`; the three existing classes
(`TikTokDDPCollection`, `TikTokAIOCollection`, `TikTokZeeschuimerCollection`) are the template. The
analysis stack (PCA/stats/recode) and the study abstraction are fully generic — a study can already
mix collections across platforms.

**Where the TikTok coupling actually lives (the work):**
- **Scraping/enrichment** (`fyp/tiktok_dl.py`): yt-dlp wrapper, TikTok cookie handling, TikTok error
  codes, TikTok page-JSON extraction, TikTok URL template. This is the biggest platform-specific
  surface — but it's *orthogonal* to ingestion (separate queue) and yt-dlp already supports many
  platforms.
- **Hardcoded raw paths** in `fyp_config.py` (`zeeschuimer_raw`, `ddp_raw`, `aio_raw`).
- **Activity-type semantics** `("play","observe")` in `organize_datasets.py` — semantic, easily
  extended.

**Plan:**
1. **Pick the second platform** (open decision — see below). Candidate exports: Instagram, YouTube
   (watch history), Facebook. Choose by (a) does it carry a *sequence + a dwell/engagement proxy*?
   and (b) which platform do your researchers actually need? A platform with no dwell signal still
   benefits the timelines/PCA/correlations analyses even if it can't join the linger feature.
2. **Spike: one new `*Collection` subclass** implementing `load_single_raw` + `process_single` for
   that platform's export format, plus a raw-path entry. Target: ingest a real export end-to-end into
   a study, *without touching the analysis layer*. This validates the "messy data → schema" promise.
3. **Generalise enrichment** only if needed: factor the scraper toward a small strategy interface
   (`platform_dl` with a TikTok and a second implementation) rather than forking `tiktok_dl.py`.
4. **Harden the "messy intake" UX** — the audience promise is *messy* data. Improve the Data
   Management ingestion path to report what was parsed/dropped/malformed per file (builds on the
   existing pending-uploads panel), so a researcher uploading an unfamiliar export sees why rows
   didn't land.

---

## Thrust 3 (Months 4–6) — Reliability & self-serve foundations

This is where "self-serve for other HASS researchers later" gets paid down without committing to a
full multi-tenant rebuild now. Two streams:

**A. Reliability / quality (de-risks everything above):**
- **Automated tests + CI.** Grow the Phase-0 pytest seed into a real suite (ingestion schema,
  windowing, recode, the known-fragile `_repair_stringified_multiindex` from `POST_HOLIDAY_NOTES.md`)
  and a GitHub Actions workflow that runs pytest + ruff on push.
- **Background-job robustness.** Add Cloud Tasks retry (the queue supports it; `max-attempts=1`
  today) and a dead-letter/audit trail; the framework is otherwise solid.
- **Cache freshness.** Give `pca_df_cache` (and the new sequence cache) the mtime-checked
  invalidation that `StudyCache` already uses, so workers refreshing data don't leave the UI stale.
- **Work the catalogued rough edges** in `POST_HOLIDAY_NOTES.md` (DNF retriability, `meta_refresh`
  ignoring the affected-studies list, the task-runner memory headroom on big merges, `UserManager`
  lazy-loading). These are pre-scoped with effort estimates — pull them in opportunistically.

**B. Self-serve enablers (architecture choices, not a full build):**
- Keep **per-study data isolation** clean (it largely is — studies are independent artifacts), so
  multi-tenant scoping later is a matter of access rules, not a data re-layout.
- **Export.** Researchers need to get findings out — a "download this study's sequence/PCA/timeline
  data + a methods note" export. Cheap to add, high value, and a prerequisite for outside users.
- **A methods/provenance note** auto-generated per study (what was filtered, sampled, annotated,
  which model) — both good science and a self-serve trust requirement.
- Defer true multi-tenancy (org/data isolation, quotas, onboarding) until there's a concrete second
  user; just don't make choices now that block it.

---

## Verification approach

- **Sequence feature:** unit tests for windowing/leakage in `tests/`; then run
  `run_sequence_refresh.py` locally against a real study, inspect `{study}_sequence.parquet`; then
  drive the tab via the `preview_*` tools (load study, toggle window/horizon, confirm lift heatmap
  renders and per-participant spread is sane). Sanity check: the dwell→next-category lift for an
  obviously sticky category should exceed 1.0; baseline-vs-augmented Δ should be small and honest.
- **Multi-platform:** ingest a real second-platform export end-to-end into a throwaway study;
  confirm it appears in Explore/Correlations with no analysis-layer changes.
- **Hardening:** CI green on push; induce a Cloud Task failure and confirm retry + dead-letter;
  refresh a study mid-session and confirm the UI cache invalidates.

---

## Open decisions to resolve as we go

1. **Which second platform** (Thrust 2) — driven by your researchers' needs and whether that
   platform's export carries a sequence + dwell proxy.
2. **Window definition defaults** (N, session gap, horizon range) — set initial defaults, then let
   Stage A exploration tell us what's stable.
3. **Where the methods/provenance note lives** (per-study sidecar vs. export-only) — decide when
   building export in Thrust 3.
