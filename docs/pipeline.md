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
- **Structure sentinel** (`structure_sentinel.py`): learns each platform's
  export structure and per-file sanity stats; a drifted upload is quarantined
  for admin review (Data Management → Ingestion) instead of silently
  mis-ingested. Parse failures stay pending and are retried next refresh.
- **Donor timezone**: uploads can carry an authoritative IANA zone / fixed
  offset per file, validated at upload time.

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

## 3. Annotation (`machine_annotation.py`, `annotation_*.py`)

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

## 5. Analysis & studies

Study definitions (`studies.py`) filter the recoded corpus into datasets.
On top of them: PCA + distance metrics (`pca.py`), ANOVA/PERMANOVA
(`stats.py`), timeline metrics (`timeline_analysis.py`), within-session
profiling (`session_profile.py`), sequence windowing/modelling
(`sequence_analysis.py`, `sequence_model.py`), dense semantic embeddings +
niche detection + 2D map (`embeddings.py`, `niche_detection.py`,
`video_map.py`; embeddings are backend-dispatched too — Gemini default,
hosted or local Qwen alternatives, model-scoped shard store).

Refresh dependencies (each a background job, chained from the UI):

```
consolidate → embeddings → video_map (niches) → study definitions → { meta ‖ pca ‖ timelines }
```

## Adding a platform — checklist

1. One ingestion subclass of `ForYouBaseCollection` (registers itself + its
   upload location automatically).
2. One scraper subclass of `BaseScraper`, registered in `_SCRAPER_MODULES`.
3. A `scope = "platform"` block in `config/scrape_contract.toml` and the
   platform added to `[meta].platforms`.

Queues, worker processes, UI blocks, and media subdirectories all derive
automatically. Use the Instagram/YouTube implementations as templates.
