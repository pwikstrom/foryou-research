# FYP Development Roadmap — H2 2026

_Drafted on return from holiday (2026-06-03). Companion to `POST_HOLIDAY_NOTES.md` (which
holds the deploy-verification checklist and engineering rough edges). This document is the
strategic plan; that one is the operational backlog._

> **2026-07-29:** Thrusts 1–2 are complete and much of Thrust 3 is paid down. The
> current plan is the **[H2 2026 — Phase 2](#h2-2026--phase-2-consolidate-for-a-hosted-multi-user-audience-2026-07-29)**
> section at the end of this document. The original three-thrust plan below is kept
> (with ✅ annotations) because its reasoning stays useful.

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
  CSS-token design system; good docs in `DEVELOPING.md`.
- ⚠️ **Fragile:** zero automated tests / no CI; no type checking (ruff only); Cloud Tasks have
  no retry / dead-letter; PCA cache has no freshness check; heavy frontend files
  (`timelines.js` ~2.5k lines, `collections.js` very large); `UserManager` bulk-loads all users
  on web cold start. (Details and several latent bugs already catalogued in `POST_HOLIDAY_NOTES.md`.)

---

## Phase 0 — Week 1 back: housekeeping (do before new work)

These are blocking/hygiene items, not new features. Most come straight from `POST_HOLIDAY_NOTES.md`.

> **✅ Update (2026-07):** done — the test harness grew into the full `scripts/verify.sh` gate,
> the model switch was resolved (and later superseded by config-declared backend variants), and
> the remaining operational checks are tracked in `POST_HOLIDAY_NOTES.md`.

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

> **✅ Update (2026-07):** all three increments are shipped (`fyp/analysis/sequence_analysis.py`,
> `sequence_model.py`, `run_sequence_refresh.py`, the sequence tab). **The headline research
> question came back null:** dwell in window _k_ does **not** predict next-window content
> (ΔR²≈0, robust across specifications) — recorded so it isn't re-chased. Two adjacent findings
> did land: within-session engagement satiation (robust) and windowed embedding entropy /
> binge-hour profiling. The sequence infrastructure remains in place for other window-level
> questions.

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

> **✅ Update (2026-06-29):** the **scraping/enrichment** half of this thrust is shipped (step 3
> below). The scraper is now `BaseScraper` (ABC, auto-registry, `get_scraper()`) + `TikTokScraper`,
> mirroring the ingestion ABC, driven by a declarative `config/scrape_contract.toml` that owns the
> canonical cross-platform scrape schema (base + per-platform fields). The output is canonicalised
> (`create_time`/`play_count`/`duration`/`author_name`/`*_per_K_play` + `scrape_status`/`storage_link`/
> `scrape_ts`) and deployed to prod. Adding a platform = one subclass (five hooks) + a `[platform]`
> block. Remaining for this thrust: a real second-platform **ingestion** subclass (step 2) and the
> messy-intake UX (step 4).
>
> **✅ Update (2026-07-04):** the **orchestration plumbing** that made "add a platform = one subclass"
> literally true is shipped and deployed. Per-platform scrape queues (`to_scrape_<platform>.json`,
> `fyp/scrape_queues.py`) each drained by their own `queue_scraper_<platform>` worker + enrichment-tab
> UI block; `source_platform` stamped per scraped row (composite activity↔enrichment merge);
> per-platform media layout (`{prefix}/{platform}/{id}.mp4`, `fyp/media_paths.py`) with legacy-flat
> fallback; `ThrottleController`/`health_check` generalised onto `BaseScraper`; annotation kept
> TikTok-only behind a guard. So steps 1–2 (pick the platform + write its `*Collection` and
> `*Scraper` subclasses) are now the *only* remaining work to onboard a second platform end-to-end.
>
> **✅ Update (2026-07-05):** the **second-platform ingestion** (step 2) is shipped for **two**
> platforms at once — `InstagramDDPCollection` and `YouTubeDDPCollection` (`fyp/ingest.py`) parse
> zipped data-donation exports into the platform-agnostic activity schema (IG: viewed reels →
> `play` + liked posts → `fave`, both export schemas; YT/Takeout: watch-history HTML, ads →
> `ad_play`, multi-locale timestamps). The "one class, nothing else" promise is now literally true
> on the ingestion side: `ForYouBaseCollection.__init_subclass__` self-registers the raw-upload
> location at import (no `fyp_config` edit — the "hardcoded raw paths" coupling below is gone),
> `registered_raw_locations()` derives the upload-location list from the registry, and the base
> class owns a `save_enrichment_seed()` + `seed_*` contract that persists donated captions/owners as
> a per-platform enrichment seed (canonical scrape-base schema, `scrape_status="donated"`). An
> optional donor-timezone (`tz`) manifest field (validated by `ingest.parse_donor_timezone`)
> authoritatively resolves ambiguous export tz labels. Parse failures raise and leave the file
> pending for retry rather than being discarded. `organize_datasets.new_merge` now always emits the
> enrichment-status/derived columns (so a pre-scraper study doesn't error), and its id-length filter
> is per-`source_platform`. Annotation stayed TikTok-only until 2026-07-07 (see below).
>
> **✅ Update (2026-07-05, later same day):** the **IG/YT scrapers** (step 3) are shipped and deployed
> (f004e3c; revs 00206-lf4/00128-fqd). `fyp/instagram_dl.py` + `fyp/youtube_dl.py` (both yt-dlp,
> `BaseScraper` subclasses), authenticated via the new `fyp/scraper_cookies.py` (per-platform
> `secrets/{platform}_cookies.txt` on GCS, Chrome cookies locally, generalised `cookie_health`);
> `fyp/tiktok_dl.py` delegates to it. A **generic media-duration cap**
> (`BaseScraper.media_duration_cap`/`should_download_media`, global 300s + optional
> `max_duration_for_download_<platform>` key) gates every platform's media phase — long-form YouTube
> deliberately stays metadata-only; skip-for-length is `ok`+`video_downloaded=False`, not an error.
> The donated **enrichment-seed fallback** is wired into consolidation (`_merge_enrichment_seeds`
> anti-join; donated rows surface in Explore but stay scrape-eligible). Contract gained `ig_*`/`yt_*`
> fields (+`video_downloaded` → base scope) → hash bump → full Consolidate & Refresh required.
> YouTube format extraction needs the EJS n-challenge solver (`yt-dlp-ejs` + deno in the base image);
> metadata is solver-independent. Known limits: IG image posts fail `permanent:no_video` (carousel
> follow-up), IG extractor currently flaky upstream ("empty media response", yt-dlp #17074 — errors
> stay transient/queued).
>
> **Update 2026-07-06 (YouTube media-gap fix):** YouTube's session rate-limit response ("Video
> unavailable … rate-limited for up to an hour") was misclassified as permanent `removed`, so media
> failures were silently saved as scrape-ok metadata-only rows and dequeued forever. Fixed
> generically: rate-limit/apostrophe-safe classification, `attrs['media_error_type']` contract on
> `BaseScraper.fetch` (transient media failures stay queued + feed the throttle), a batch circuit
> breaker that stops Cloud-Task self-chaining, `inter_request_delay()` pacing, a "Retry missing
> media" queue-builder option, and the **bgutil PO-token provider** (pip plugin + Node-built script
> in `Dockerfile.base`, script mode via `_pot_extractor_args()`). Live validation: safeguards all
> engage in prod, but the bot wall persists on the 2026-07-05 cookie file — next step is a fresh
> cookie export from a closed incognito session; fallback is draining the YT queue from a
> residential IP (that fallback is now BUILT: `FYP_FORCE_GCS` local-drain runbook in DEVELOPING.md,
> `e79ba7a`, not yet run).
>
> **✅ Update (2026-07-07):** three more pieces shipped and deployed (`27706f4`, `94e8f57`,
> `18b6251`; revs 00223-l2m/00146-xgv + 00224-mrg):
> - **Slideshow OOM root fix** — the task-runner OOM was `make_slideshow` compositing photo-post
>   slideshows at native 2160×3840 (~15–18 GiB per 20-image post), not yt-dlp. Canvas now capped
>   at 1000px with PIL pre-downscale (~1.6 GiB peak); the interim concurrency/admission-gate
>   workarounds were reverted, the memory valve + download byte-caps kept.
> - **Cookie-race fix** — the "not a Netscape format cookies file" error was yt-dlp's non-atomic
>   cookie write-back racing across concurrent scraper threads on the shared /tmp cache; each call
>   now gets a private validated copy. (So there was never a "malformed IG cookie file" to fix.)
> - **Annotation generalized to ALL platforms** — TikTok-only guards removed, composite
>   `(source_platform, item_id)` keying end-to-end, contract wording platform-neutralized → new
>   version `av_8e04fabdfefd`. Eligibility: scraped_ok + video_downloaded + under duration cap
>   (long-form YT metadata-only items never queue).
> Also shipped 2026-07-06: the **DDP structure sentinel** (`fyp/structure_sentinel.py`) —
> per-(platform,source) learned baselines quarantine silent export-format drift with an
> approve/reject review panel; a big first bite of the messy-intake UX (step 4).
>
> **Remaining (operational, see POST_HOLIDAY_NOTES.md § 2026-07-07): fresh YT cookie export +
> local drain, IG cookies + re-scrape, drain TikTok queue, queue IG/YT annotation + promote the
> new av_, Consolidate & Refresh; then the rest of the messy-intake UX (step 4).**

**Where the TikTok coupling actually lives (the work):**
- **Scraping/enrichment** (`fyp/tiktok_dl.py`): yt-dlp wrapper, TikTok cookie handling, TikTok error
  codes, TikTok page-JSON extraction, TikTok URL template. This is the biggest platform-specific
  surface — but it's *orthogonal* to ingestion (separate queue) and yt-dlp already supports many
  platforms.
- ~~**Hardcoded raw paths** in `fyp_config.py` (`zeeschuimer_raw`, `ddp_raw`, `aio_raw`).~~ ✅ Gone —
  classes self-register their raw location at import via `__init_subclass__` (2026-07-05).
- **Activity-type semantics** `("play","observe")` in `organize_datasets.py` — semantic, easily
  extended.

**Plan:**
1. **Pick the second platform** (open decision — see below). Candidate exports: Instagram, YouTube
   (watch history), Facebook. Choose by (a) does it carry a *sequence + a dwell/engagement proxy*?
   and (b) which platform do your researchers actually need? A platform with no dwell signal still
   benefits the timelines/PCA/correlations analyses even if it can't join the linger feature.
2. ✅ **Done (2026-07-05):** two new `*Collection` subclasses (`InstagramDDPCollection`,
   `YouTubeDDPCollection`) implementing `load_single_raw` + `process_single` for the platforms'
   zipped export formats; the raw-path entry is now auto-registered by the base class. Real exports
   ingest end-to-end into a study *without touching the analysis layer*, validating the "messy data →
   schema" promise. Donor-timezone override + parse-failure-stays-pending robustness landed with it.
3. ✅ **Done (2026-07-05):** IG/YT `*Scraper` subclasses shipped + deployed (see update above).
   Operational follow-ups tracked there (cookie uploads, Consolidate & Refresh, live batch
   validation); deeper follow-ups: IG carousel→slideshow support, IG retry-count demotion for the
   ambiguous rate-limit error (PO-token provider for YT shipped 2026-07-06). ✅ Annotation
   generalized to all platforms 2026-07-07 (see update above).
4. **Harden the "messy intake" UX** — the audience promise is *messy* data. Improve the Data
   Management ingestion path to report what was parsed/dropped/malformed per file (builds on the
   existing pending-uploads panel), so a researcher uploading an unfamiliar export sees why rows
   didn't land. _Partially shipped 2026-07-06:_ the DDP structure sentinel quarantines
   silently-drifted export formats with a per-file review panel; remaining is the per-file
   parsed/dropped/malformed reporting for *accepted* files.

---

## Thrust 3 (Months 4–6) — Reliability & self-serve foundations

> **✅ Update (2026-07-23):** much of this thrust has been paid down ahead of schedule.
> Stream A: the professionalization plan (complete 2026-07-12) delivered the pytest suite +
> golden annotation safety net, the `scripts/verify.sh` gate, CI, packaging, lazy config, and
> the `fyp/` subpackage split. Stream B (self-serve): the public-install UX shipped 2026-07-15
> (setup wizard, local-first defaults, LICENSE/CITATION, public mini-site), and annotation +
> embedding are now **pluggable backends** (hosted `qwen_api` passed production acceptance
> 2026-07-23 at ~3× lower cost than Gemini; local `qwen_local`/`minicpm_local` for fully
> offline installs; config-declared backend *variants* pin model versions). A JOSS paper is
> drafted on an unmerged working branch. Remaining:
> Cloud Tasks retry/dead-letter, study export + methods/provenance note.

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

---

# H2 2026 — Phase 2: consolidate for a hosted, multi-user audience (2026-07-29)

_Drafted 2026-07-29, after Correlations Phases 0–3 deployed (prod == main == `199cf91`).
Supersedes the three-thrust plan above as the active plan; the history and reasoning
above stay authoritative for what shipped and why._

## Strategic frame

**Goal:** an intuitive, powerful, reliable, robust tool to ingest, scrape, annotate,
and analyse sequenced user data from short-video platforms, for HASS researchers at
all levels plus students doing assignments on recommender systems and cultural taste.

**Three decisions taken 2026-07-29 that shape this phase:**

| Decision | Choice |
|---|---|
| Delivery model | **Hosted instance** — invite researchers/students onto the Cloud Run deployment; self-install/JOSS is a medium-term capstone, not a near-term driver |
| Audience weighting | **Researchers and students, equal weight** — teaching enablers are first-class plan items |
| Posture | **Consolidate first** — reliability/self-serve (Thrust 3) finishes before new analysis lenses |

## Short term (next ~6–8 weeks)

### S1. Clear the operational backlog — make "reliable" true, not just built (weeks 1–2; admin ops, not code)

**Status 2026-07-29: substantively closed.** A full read-only verification against
prod found most carried-forward items already done: the prod **PCA/Correlations
refresh** ran 2026-07-29 (`{study}_corr_stats.json` × 7 + `annotation_reliability.json`
in the `cache` location — note the reliability artifact is empty until a repeat-run
ab_eval or n≥10 human-eval coding exists, so "Correct for noise" stays a no-op for
now); the **YouTube residential-IP drain** completed early July (all 1,218 items
scraped); the **IG `no_video` re-queue** is done (84/87 scraped, 83 annotated);
**IG/YT annotation + `av_8e04fabdfefd` promotion** are done; `MAIL_PASSWORD` is set
on both services; the `share_annotations` opt-in migration was applied 2026-07-14
with redeploys since. The **`GEMINI_API_KEY` was rotated 2026-07-29**: the key
leaked in git history was deleted (GCP soft-delete) after every local copy was
swapped to a restricted replacement key — the historical string is now inert.

Remaining, deliberately deprioritized (tracked in the pending-ops memory hub, not
blocking S2): enable **GCS bucket versioning** on `fyp_bucket_01` (+ a
noncurrent-version lifecycle cap), the 949-item TikTok queue drain, the `sv_` /
`av_28a765642412` promotions, the viz/timeline "Platform" checkboxes, one plain
Consolidate & Refresh after any new scrapes, and the local `v0_legacy` decision.

_Why first:_ every later item (reliability metrics, demos, onboarding) reads from
artifacts these ops produce, and inviting users onto a stale pipeline undermines the
"reliable, robust" goal directly.

### S2. Hosted multi-user hardening (weeks 2–5; code)

- **Cloud Tasks retry / dead-letter + failure audit trail** (queue is
  `max-attempts=1` today) — the biggest remaining Thrust-3 robustness gap.
- **Cost guardrails for invited users:** per-user/role quotas or approval gates on
  expensive ops (annotation runs, re-scrapes, refreshes) — today an invited student
  could trigger a four-figure Gemini bill. Build on `web_interface/permissions.py`
  and the activity log.
- **Permission sweep** of the remaining API surfaces (process start, queue builders,
  exports) — the Correlations Phase-0 audit found tab endpoints readable by any
  logged-in user; repeat that audit everywhere.
- **UserManager lazy-loading** + remaining catalogued rough edges, pulled
  opportunistically.

> **✅ Update (2026-07-29):** S2 is **shipped and deployed** — merged to main as
> `98b3272` (four commits, one per phase), revisions `fyp-data-hub-00309-dmw` +
> `fyp-task-runner-00213-gwd`, queue `fyp-background-tasks` moved to
> `max-attempts=4` (60s–600s backoff) *after* both services went live.
>
> Two of the four bullets above were written from stale assumptions, and the
> endpoint audit found worse than "readable by any logged-in user":
>
> - **Two genuinely unauthenticated exposures**, not just missing tab gates.
>   `/api/video/<study>/<item_id>` had **no auth decorator at all** and ignored
>   its `study` segment — with the hub public (`invoker-iam-disabled=true`),
>   anyone who knew or guessed an `item_id` could download participant media.
>   `/internal/run-task/<name>` was registered on the **public** hub, CSRF-exempt,
>   gated only by an `Authorization: Bearer` *prefix* check (the OIDC token was
>   never verified) — arbitrary task execution. It now registers only on the
>   task-runner, where platform IAM restricts the invoker. Verified on prod:
>   401 / 401 / 404-on-hub / 403-on-runner.
> - **Permission + study-access gates** added to the four Explore endpoints,
>   `/api/video_analysis/ids` and the three Timelines endpoints, via a shared
>   `web_interface/routes/_access.py`. `/api/logs/<name>` is admin-only;
>   `/api/status` redacts `task_args`/`last_run_study` from plain viewers.
> - **UserManager lazy-loading was already done** (commit `b6ec1f0`,
>   2026-07-06) — no work needed. Likewise `/api/start` was already
>   admin-only, so the cost exposure was never "a student starts a worker": it
>   was the **queue builders**, which built unbounded queues. Those now carry
>   admin-configurable per-request caps (admins bypass), a dry-run + confirm
>   step showing item count and estimated spend, and activity-log entries.
> - **Retry is app-controlled, per task** — the queue could not simply be turned
>   up, because the handler returned HTTP 200 even on failure. Only the 11
>   verified-idempotent refreshes (`process_routes.QUEUE_RETRY_SAFE`) return 503
>   and get retried; scrapers/annotators/consolidate/`collection_delete` stay
>   single-attempt by design (queue-prune and batch-job claims make a blind
>   retry mean double spend or a lost batch). `web_interface/task_failures.py`
>   is the dead-letter record — Cloud Tasks HTTP queues have no native one —
>   surfaced on Admin → System info with acknowledge actions.
> - **Rough edges cleared:** `meta_refresh_groups` now honours `--studies` (it
>   was refreshing every study on every pipeline run), `new_merge` releases its
>   join inputs (~3× final-frame peak RSS), and the intermittent timelines
>   MultiIndex failure from 2026-04-21 is fixed at the `data_io` layer.
>   **DNF retriability is deliberately deferred to M2** — it needs a
>   raw-results-layer change, not the cheap fix this phase was scoped for.

### S3. First-session UX: onboarding + honest intake (weeks 3–6; code)

- **Finish the messy-intake UX** (Thrust 2 step 4 remainder): per-file
  parsed/dropped/malformed reporting for *accepted* uploads, alongside the
  structure-sentinel review panel.
- **Guided first-run** for a newly invited user (what a study is, what the tabs
  answer, what to upload) — extend the public mini-site guide into the logged-in
  Home tab rather than building a wizard.
- **Per-study methods/provenance note** (Thrust 3B): auto-generated summary of
  filters, sample sizes, annotation contract/model versions, refresh dates — the
  inputs already exist in the registries and study metadata.

> **✅ Update (2026-07-30):** S3 is **shipped and deployed** — main `690f49e`,
> revisions `fyp-data-hub-00310-xsz` + `fyp-task-runner-00214-ksf`. As with S2,
> the verification pass found the bullets partly stale — several building blocks
> already existed and the work was largely *surfacing* them:
>
> - **Messy-intake UX:** per-file `raw_rows`/`kept_rows` already lived in the
>   ingestion ledger and a per-file results table already rendered — but only
>   from the live poll of one run (gone on reload), with no ledger read
>   endpoint, no drop *reasons*, and `raw_rows=0` recorded for too-small files.
>   Now: the base ingest loop counts per-file drops generically
>   (`not_parseable` around `process_single` — NOT in
>   `_finalize_activity_frame`, which TikTok bypasses — and
>   `missing_required` in the `_standardize` hard-drop gate), the ledger
>   carries `processed_rows`/`deduped_rows`/`dropped`, and the Ingestion page
>   gains `GET /api/manage/ingestion/ledger` + a persistent "Ingestion history"
>   panel with plain-language drop labels. Pre-DataFrame record skips (IG/YT
>   parser level) are deliberately uncounted in v1 — copy says "rows we could
>   read".
> - **Guided first-run:** a dismissible, permission-keyed "Getting started"
>   panel on the Home tab (dismissal via the user-settings merge); `/guide`
>   reachable again when logged in (it wasn't — every link was
>   anonymous-gated); honest zero-study empty states (the study picker was
>   silently *hidden*, Explore showed "Loading filters..." forever, My Studies
>   rendered a bare header). Bonus fix: a stale `#my_studies` selector let
>   non-admins open the *editable* study modal from My Studies.
> - **Methods note — open question 3 decided: sidecar** (`{study}_methods.json`
>   in `cache`), built by `web_interface/services/methods_note.py` and written
>   by BOTH refresh workers on **every** refresh *including short-circuits* —
>   a preferred-version promotion must reach the note without a rebuild.
>   Surfaced via `GET /api/studies/<study>/methods` + a "Methods" modal on
>   Explore (plain language + JSON download); the schema carries `*_label`
>   plain-language siblings so the M1 export README is a templating job.
> - **Deferred:** the `USER_ACCESS` empty-means-all (analysis tabs) vs
>   empty-means-none (My Studies) inconsistency needs a product decision on
>   default sharing — honest messaging shipped instead.
> - **Post-deploy:** methods notes generate on the next study/pipeline refresh;
>   run one Recode & Refresh to backfill all studies.

### S4. Teaching enabler #1: demo study + safe role (weeks 5–8)

- A **synthetic/consented demo dataset** ingested as a permanent sandbox study —
  doubling as the JOSS reviewer demo path (one artifact, two uses).
- A **read-only "student/explorer" role** on the existing role/permission system:
  full analysis tabs on shared studies, no uploads, no expensive ops. With S2's
  quotas this is the minimum to put a class in front of the tool.

> **✅ Update (2026-07-31):** S4 is **shipped and deployed** — merged to main as
> `6b59a7c` (commit `a07b576`), revisions `fyp-data-hub-00311-htj` +
> `fyp-task-runner-00215-ldt`; boot migrations verified in the prod logs
> (roles.json gained the votes key + the student role; the USER_ACCESS
> backfill found nothing to do — every prod study already carried an explicit
> list). As with S2/S3, verification reshaped the bullets:
>
> - **Built-in `student` role** seeded at boot beside admin/viewer: the four
>   analysis tabs + personal My-stuff pages. Deliberately excluded:
>   `tab.semantic_space` (the embedding map is corpus-global — any holder sees
>   the *real* corpus, grant per-installation if acceptable) and the new
>   `feature.annotation_votes` key. Three write endpoints turned out to be
>   reachable with **zero** permissions (video tags save/delete, video vote,
>   plus `/api/user/settings` accepting arbitrary keys) — all now gated;
>   both vote endpoints sit behind `feature.annotation_votes` (grant-alled to
>   existing roles at boot, skip-listed for student per the product decision
>   that students don't feed the annotation demand signal).
> - **The deferred USER_ACCESS decision landed as a global flip**: empty/missing
>   means shared-with-nobody on every surface (it meant "everyone" on the
>   analysis tabs and "nobody" on My Studies). A boot-time migration
>   (`fyp.studies.migrate_user_access_defaults`, hub/local-server only — never
>   plain imports) backfilled a snapshot of existing role names into every
>   unshared study, so nobody lost access while future roles start excluded.
>   My Studies also now honours per-username grants, Correlations shares the
>   `_access.py` helper, and `save_study` rejects empty `SELECTED_COLLECTIONS`
>   server-side (an empty list silently selected EVERY collection at recode).
> - **Demo dataset is TikTok-only synthetic** (IG/YT export shapes deferred to
>   M4): `scripts/generate_demo_dataset.py` deterministically emits 5 donor
>   personas × 45 days over ~790 items in 7 niches with planted group
>   differences, plus a contract-conformant scrape parquet (real `sv_`) and
>   raw structured-annotation JSON (real `av_`) consumed by the genuine
>   refine/consolidate/recode/PCA pipeline. Note TikTok DDP ingests `.json`,
>   not zips — the roadmap's "export zips" assumption was stale.
> - **Isolation is layered**: a `TikTokDemoCollection` (`data_source="demo"`)
>   keeps the armed `tiktok_ddp` structure-sentinel baseline unpolluted; all
>   demo item ids start `9900…` (`utils.DEMO_ITEM_ID_PREFIX`), which excludes
>   them from the embeddings/semantic-map backlog and from every queue build
>   (`_apply_queue_cap`); demo scrape rows carry `video_downloaded=False` so a
>   real annotation run can never DNF-clobber the fabricated annotations; and
>   studies exclude demo collections by construction (explicit id lists).
> - **Honest provenance everywhere**: methods-note schema v2 adds a
>   `data_provenance` block driven by a `SYNTHETIC: true` study-def key, the
>   Explore methods modal shows a synthetic banner, Home/`/guide` copy names
>   "Demo study (synthetic data)", and the study picker auto-selects the first
>   *non*-synthetic study so the demo never hijacks a researcher's default.
> - **Post-deploy ops owed (one-click since the `demo_dataset` worker landed):**
>   Admin → DM → Ingestion → "Generate demo dataset" (installs donor files +
>   enrichment + the study definition on the task-runner; also available as
>   `scripts/generate_demo_dataset.py --write`), then Process New Collections,
>   then Consolidate & Refresh (+ collection-metadata refresh). Verify with a
>   throwaway student account and optionally set
>   `default_new_user_role = student`.

## Medium term (~2–6 months)

### M1. Data export + reproducibility package

Per-study export: recoded parquet/CSV + PCA/timeline/sequence/correlation artifacts
+ the S3 methods note as a bundled README. The remaining Thrust-3B core item and the
single highest-leverage "powerful" feature for the audience.

### M2. Reliability layer, completed

- **Correlations Phase 4** extensions (per the approved 5-phase plan).
- Decide — and if funded, execute — the **250k legacy re-annotation** (~$3–5k;
  newer contract fields are null on legacy items), or explicitly scope studies to
  post-contract data and surface that in the methods note.
- **Arm the structure sentinel for IG/YT** once enough accepted files exist
  (currently learn-only).

### M3. Teaching enabler #2: assignment-ready workflows

Cohort onboarding (bulk invite) plus 2–3 written **assignment templates** ("compare
two collections' category profiles", "does engagement satiate within sessions?" —
leaning on shipped findings) and guide pages. Deliberately lightweight — content and
small UI affordances, not a "classroom mode" build.

### M4. Public release / JOSS capstone

The drafted package (on an unmerged working branch) becomes
cheap to finish once S1 (key rotation), S4 (demo path), and S3 (provenance) land:
fill placeholders, sanitization audit, merge, Zenodo + tag + submit. Positioning:
credibility and citability for the hosted instance, not a pivot to self-install
support.

### M5. New analytical capability (gated behind M1–M2)

First candidates by audience fit: **cross-platform comparative views** (the same
participant/cohort across TikTok/IG/YT — multi-platform ingest exists, but no tab
makes the comparison first-class) and **cross-study/cohort comparison** in
Explore/Correlations. Sequence-lens extensions only when a concrete research
question demands them (respect the recorded nulls: linger→feed is null, "feed
diversification" was retracted).

## Sequencing logic

S1 before everything (artifacts + trust) → S2 before inviting anyone new (cost +
access safety) → S3/S4 make "intuitive" real for both audiences and feed M3/M4 →
M1 is the top researcher ask → M2 makes "powerful" defensible → M4 rides on S-phase
byproducts → M5 last, per "consolidate first".

## Verification approach

- **S1:** each pending-ops item verified closed (artifacts present in GCS, queues
  drained, versions promoted); pending-ops hub updated.
- **S2:** ✅ done 2026-07-29. Endpoint sweep is covered by
  `tests/unit/test_endpoint_gates.py` and re-verified against prod after deploy
  (401 on the media/item endpoints, 404 for `/internal/run-task` on the hub,
  403 on the task-runner); caps and the 503/200 retry matrix are covered by
  `tests/unit/test_cost_guardrails.py` and `tests/unit/test_task_failures.py`.
  `docs/routes.md` regenerated. **Still outstanding:** the retry + dead-letter
  path has not yet fired on a *real* prod failure — the first genuine task
  failure will exercise it and should appear on Admin → System info.
  Note for future spot-checks: curling a gated POST returns 400 from CSRF
  *before* the permission decorator runs, so test a GET endpoint instead.
- **S3:** ✅ shipped 2026-07-30 (main `690f49e`). Covered by
  `tests/unit/test_methods_note.py`, `test_home_getting_started.py`,
  `test_ingest_drop_stats.py`, `test_build_per_file_summary.py`,
  `test_ingestion_ledger_endpoint.py`; url-map snapshot + `docs/routes.md`
  regenerated (2 new routes). Prod spot-checks: `/guide` 200 anonymously, the
  two new endpoints 401 unauthenticated. **Still outstanding:** one Recode &
  Refresh run to generate methods notes for existing studies, the
  malformed-export end-to-end check on real prod data, and a fresh-viewer
  manual walkthrough of the Home panel + empty states.
- **S4:** ✅ code shipped 2026-07-30. Covered by `tests/unit/test_study_access.py`,
  `test_demo_generator.py` (determinism, contract conformance, dedup-safety,
  PCA floor, real ingest parse), and extensions to `test_endpoint_gates.py` /
  `test_role_permission_migration.py` / `test_cost_guardrails.py` /
  `test_methods_note.py`; no route changes so the url-map snapshot is
  untouched. **Still outstanding:** the prod data ops (generator run, ingest,
  demo-study creation) and the manual student-account walkthrough — log in as
  the student role → read-only surfaces only, demo study visible.

## Status addendum (2026-08-10)

With S1–S4 code-closed, August work went to research-facing depth and
corpus-scale robustness rather than starting the M-track:

- **Correlations tab overhaul** (deployed through 2026-08-06): statistical-rigor
  pass (two-panel Group differences, corr_stats v2), then an explanations layer
  (key-findings report, sample-bound Gemini interpretation, per-view explainer
  texts). See `docs/correlations-tab-guide.md`.
- **Sessions tab** (new; four deploys 2026-08-06 → 2026-08-10): session-quality
  explorer with binge episodes and low-entropy sequences, admin-editable list
  floors, per-binge creators, permutation-based directedness, within-binge trend
  scan, and a binge-detector rewrite (off-theme skip tolerance + rewind). One
  prod `sessions_refresh` rebuild is owed to pick up the new segmentation.
- **Corpus-scale robustness** (deployed 2026-08-09): the `pca_refresh` OOM fixed
  exactly (survivors-only crosstab — no published number moves), the sessions
  build rewritten O(batch) over a dense embedding sidecar
  (`fyp/analysis/embedding_store.py`), streaming `data_io` primitives, a shared
  memory probe (`fyp/core/memory.py`), and dispatch-deadline/dead-letter fixes
  for self-chaining workers. Known next cliff: `stats.family_permanova` is
  O(groups²).
- **Ops/UX**: durable per-process run logs (last 10 runs, launch attribution),
  per-variable filter value search, Video Analysis timestamp/identity/navigation
  fixes.

The M-track (M1 study export bundle first) remains the next planned thrust.

## Status addendum (2026-08-16)

The week after the 2026-08-10 addendum continued the same consolidate-and-deepen
posture; the M-track has still not started.

- **Sessions tab, hardened and integrated** (multiple deploys 2026-08-11 →
  2026-08-16): a performance overhaul (locks + caches, a `sessions_plays`
  artifact with baked-in play texts, fingerprint caches — detail views drop
  from ~30s to sub-second warm); usability round (context deltas, strip cursor,
  off-theme videos visible when stepping a binge, per-session variable step
  plot); the refresh made **study-window-scoped and incremental**
  (`run_sessions_refresh.py` `stale_only` mode, per-collection provenance,
  chained automatically after every study save) with coarse global enrichment
  invalidators (embedding-store / annotation-corpus fingerprints force a full
  rebuild — fixing a bug where new annotations never marked anything stale);
  and the sessions worker wired into the refresh-pipeline UI and pipeline
  membership.
- **Reliability**: a thread-pool **alias-shim import race** (bit timelines on
  chain links) fixed and then audited repo-wide with a guard test
  (`tests/unit/test_pool_import_race.py`); the Cloud Tasks **dispatch-deadline
  trap** closed on the pipeline side (every pipeline dispatch now carries the
  deadline); embedding-store **dedupe + a single-flight lease** on
  `embeddings_refresh` after a duplicate-shard incident; chain-spanning
  `last_run_duration` + unified status-light colours; a refresh-pipeline step
  list and card info tooltips; Consolidate button restructure.
- **Scraper resilience**: a **transient-storm guard** (companion to the
  permanent-storm guard, born of the TikTok bot-wall incident); failed-scrapes
  records now carry per-item failure categories (plus a repair of the corrupted
  prod record); cached local Chrome cookie extraction.
- **Study management**: **Duplicate** and **Rename** actions in the study modal
  (rename moves artifacts without a rebuild), and per-endpoint **study
  date-window control** (independent start/end with steppers and chart edge
  handles).
- **Annotation ops**: coverage-based annotation targeting — queue items by
  collection-day scrape coverage, cheapest-day-first
  (`scripts/adhoc/queue_high_coverage_days.py`; applied to prod).

M1 (study export + reproducibility package) is still the next planned M-track
item.
