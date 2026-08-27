# Data pipeline

The path from a participant's donation zip to an analyzable study dataset.
Module references are to `fyp/` unless noted.

## 1. Ingestion (`fyp/ingest/`)

`ForYouBaseCollection` is an ABC with an `__init_subclass__` auto-registry.
Each platform subclass declares `source_platform` + `raw_path` and implements
two hooks:

- `load_single_raw(filename)` — read one raw donation into a per-file DataFrame
- `process_single(df)` — produce `utc_timestamp` and finalize

The base class owns everything generic: the activity schema (from
`config/activity_contract.toml`), the load loop (manifest, per-file donor
timezone, ledger, dedup), timestamp finalization, and the enrichment seed.
Current subclasses: three TikTok variants (DDP export, AIO capture,
Zeeschuimer), `InstagramDDPCollection`, `YouTubeDDPCollection`.

Notable behaviors:

- **Engagement→play linking**: likes/comments/shares are folded into the
  matching play row's `extra_data` (adjacency first, nearest-play fallback) —
  this folded token is the only engagement signal that survives into studies.
- **Enrichment seed**: donated item metadata (caption, author) is persisted
  per platform in the canonical scrape schema with `scrape_status="donated"`,
  and used at consolidation as a lowest-precedence fallback for items that
  can't be scraped.
- **Structure sentinel** (`fyp/core/structure_sentinel.py`): learns each
  platform's export structure and per-file sanity stats; a drifted upload is
  quarantined for admin review (Data Pipeline → Ingest Collections) instead
  of silently mis-ingested. Parse failures stay pending and are retried next refresh.
- **Per-file intake report**: the load loop records each file's true raw row
  count (including too-small discards) and a per-file drop-reason breakdown —
  rows that couldn't be interpreted (`not_parseable`), rows missing
  required-core fields (`missing_required`), rows deduplicated against the
  archive. Persisted in the ingestion ledger (`ingestion_ledger.json`) and
  surfaced on Data Pipeline → Ingest Collections: the live "Last run results" table
  plus a permanent "Ingestion history" panel
  (`GET /api/manage/ingestion/ledger`) with plain-language labels, so an
  uploader can always see why rows didn't land.
- **Donor timezone**: uploads can carry an authoritative IANA zone / fixed
  offset per file, validated at upload time.
- **Browser-side review** (participant uploads): the export is parsed
  entirely client-side (`static/js/donation_review.js` — no network requests
  happen during review), sections and individual rows can be pruned, and the
  pruned file is rebuilt from the kept rows before upload. For TikTok,
  non-whitelisted sections (DMs, settings, ads data, profile) are stripped in
  the browser by default via the review manifest's `unmapped_policy: "strip"`
  (`fyp/ingest/tiktok.py`), and login-history/IP rows are surfaced as a
  reviewable section. Reviewed uploads are flagged `client_reviewed`, and the
  structure sentinel evaluates them against a separate `__reviewed` baseline
  instead of quarantining them as drift from the verbatim-export baseline
  (`fyp/core/structure_sentinel.py`).

## 2. Scraping (`fyp/scrape/`)

`fyp/scrape/scrape.py` is platform-agnostic orchestration: per-platform queues
(`to_scrape_<platform>.json`, `scrape_queues.py`), batching, threading, a
throttle controller, media-phase retry, a circuit breaker for
rate-limit/bot-wall storms, and consolidation of scrape parquets.

`BaseScraper` (`platform_scraper.py`) is the per-platform ABC (auto-registry
+ `get_scraper(platform)` factory). A platform implements five hooks —
`item_url`, `fetch`, `map_to_canonical`, `classify_error`, `repair_counts` —
plus optional overrides (throttle limits, health check, slideshow hooks).
All three current scrapers (TikTok, Instagram, YouTube) are yt-dlp-based;
cookies are managed per-platform by `scraper_cookies.py`.

The canonical cross-platform scrape schema lives in
`config/scrape_contract.toml`: base fields every platform emits (including
generic popularity counts `fave_count`/`comment_count`/... and per-K
engagement rates) plus genuinely platform-specific fields. Each scraper
translates its raw field names to canonical at scrape time.

Photo/carousel posts are rendered into slideshow mp4s (with the post's audio
when fetchable) so everything downstream is uniformly video.

## 3. Annotation (`fyp/annotation/`)

Downloaded media is queued (`to_annotate.json`) and sent to the active
annotation backend (`fyp/annotation/backends/` — Google Gemini by default,
with hosted-Qwen `qwen_api` and local `qwen_local`/`minicpm_local`
alternatives, plus config-declared variants for model-version pinning; see
`docs/configuration.md` and `docs/installation.md`), driven by a prompt +
structured response schema generated from
`config/annotation_contract.toml` (`annotation_schema.py`). Eligibility:
scraped OK, media downloaded, under the duration cap. Output rows are keyed
`(source_platform, item_id)` and stamped with the annotation version
(`annotation_versioning.py`, `av_` hash) so superseded fields remain
readable as "legacy". Annotation runs as a self-chaining Cloud Task
(`web_interface/run_queue_annotator.py`), one batch per task.

Regression protection: `tests/golden/` replays saved raw Gemini responses
through the whole parse/flatten/repair pipeline — run it whenever you touch
annotation code.

## 4. Consolidation & recoding (`scrape.py`, `organize_datasets.py`, `recode_variables.py`)

"Consolidate & Refresh" (web UI) folds new scrape/annotation parquets into
the enrichment store, merges enrichment seeds, migrates legacy columns to
canonical names, and detects value changes from re-scrapes (so affected
studies auto-refresh). `organize_datasets.new_merge` joins activity with
enrichment on `(source_platform, item_id)`; `recode_variables.py` derives
analysis variables per the var_schema (type-driven generic recoder).

**Scrape → annotate handoff for participant first batches.** A
participant's prioritised first batch is queued to the *scrape* queue only,
at ingest; the items reach the annotate queue at **consolidation** — the
moment scrape results become visible — once they show as
scraped-but-unannotated, exactly once per ledger entry
(`web_interface/services/participant_enrichment.py`, invoked from
`run_consolidate_enrichment.py`). The correctness rule behind the ordering:
an unscraped item in the annotate queue fails ("DNF - file not found") and
is pruned as failed — never annotate-queue unscraped items. Note that
first-batch auto-enqueueing ships **disabled**
(`AUTO_ENQUEUE_ENABLED = False` in `participant_enrichment.py`): the
ledger/handoff/notification machinery stays wired but is a no-op until an
operator enables it.

## 5. Analysis & studies

Study definitions (`studies.py`) filter the recoded corpus into datasets.
Composed system studies (the participant "Everyone & Me" study) are the
exception: they store no artifacts of their own and are assembled at read
time from the default study plus the user's data, and SYSTEM study
definitions are excluded from all-studies sweeps.
On top of them: PCA + distance metrics (`pca.py`), ANOVA/PERMANOVA
(`stats.py`), timeline metrics (`timeline_analysis.py`), within-session
profiling (`session_profile.py`), sequence windowing/modelling
(`sequence_analysis.py`, `sequence_model.py`), dense semantic embeddings +
niche detection + 2D map (`embeddings.py`, `niche_detection.py`,
`video_map.py`; embeddings are backend-dispatched too — Gemini default,
hosted or local Qwen alternatives, model-scoped shard store). The Sessions
tab's artifacts (session index, binge episodes, low-entropy windows) are
built by `fyp/analysis/session_explorer.py` + `entropy_metrics.py` over a
dense random-access embedding sidecar (`fyp/analysis/embedding_store.py`),
as a batch-and-chained `sessions_refresh` worker.

Every study refresh also writes a **methods/provenance note**
(`{study}_methods.json`, built by `web_interface/services/methods_note.py`):
a plain-language, export-ready record of the study's filters, sample sizes,
the annotation/scrape/activity contract versions present in the rows, the
embedding model behind any niche columns, and refresh dates. Both refresh
workers write it on every refresh — including short-circuited ones, so a
newly *preferred* annotation version reaches the note without a rebuild.
Surfaced as the "Methods" panel on each study's row under My stuff → My Studies
(`GET /api/studies/<study>/methods`); it becomes the bundled README in the
planned per-study export.

Refresh dependencies (each a background job, chained from the UI):

```
consolidate → embeddings → video_map (niches) → study definitions → { meta ‖ pca ‖ timelines ‖ sessions }
```

`sessions_refresh` joins the fan-out as a fourth terminal leaf, in
`stale_only` mode: it re-segments only the collections whose coverage windows
or in-window play/annotated counts moved, and returns immediately when none
did. `skip_if_busy` keeps it off the toes of a sessions run already in flight
(one is also chained after every study save). It can still be run on its own
from Data Pipeline → Dataset Assembly, where "Force full rebuild" re-segments
every covered collection.

## Adding a platform — checklist

1. One ingestion subclass of `ForYouBaseCollection` (registers itself + its
   upload location automatically).
2. One scraper subclass of `BaseScraper`, registered in `_SCRAPER_MODULES`.
3. A `scope = "platform"` block in `config/scrape_contract.toml` and the
   platform added to `[meta].platforms`.

Queues, worker processes, UI blocks, and media subdirectories all derive
automatically. Use the Instagram/YouTube implementations as templates.

Those three steps are the core; a handful of supporting steps (structure-
sentinel baselines, per-platform cookies, one hardcoded display-name map,
tests) are easy to miss — the complete checklist is in
[extending.md](extending.md).
