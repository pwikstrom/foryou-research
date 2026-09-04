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

**Incremental consolidation.** With the *Incremental consolidation* admin
setting on, `consolidate_enrichment` folds only the NEW batch files into the
consolidated frames (scrape lane: `_fold_scrape_batch`; annotation lane:
`_fold_annotation_batch`, sourcing touched keys' history from the
all-versions archive) and patches `enrichment_status.parquet` for the
touched item_ids (`patch_enrichment_status`) instead of rebuilding
everything from all files — O(batch) compute instead of O(corpus). The fold
reuses the full rebuild's normalize/dedupe/seed/preferred-view transforms
verbatim, and declines to the unchanged full path whenever equality cannot
be proven: a forced run, a scrape-contract bump, a value-column-set change,
an annotation-version promotion since the last run (recorded in the
ledger), or a missing archive/marker. Donated seed rows carry an
`is_enrichment_seed` provenance column so the fold can evict and re-derive
them against the current seed files. A weekly shadow verification
(`consolidate_enrichment` with `verify_consolidation`, self-scheduled from
the tail of a normal run) dry-runs the full rebuild and compares all three
artifacts per item; a mismatch is recorded in the task-failure ledger and
auto-promotes the full rebuild. Golden equality tests:
`tests/golden/test_incremental_consolidation.py`.

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

**Automatic per-collection enrichment (Process A + B).** An armed
collection is enriched unattended by a *supervisor* loop
(`web_interface/run_enrichment_supervisor.py` +
`web_interface/services/collection_enrichment.py`): each short tick either
starts a queue worker, consolidates, hands newly scraped items to the
annotation queue, or cuts the next slice into the scrape queue — one action
per tick, so the loop unrolls to
`plan → scrape → consolidate(light) → annotate → consolidate(full refresh)`.
A plan's goal is an **annotation target** — keep going until this many of
the collection's unique videos are annotated. The target is a *state*, not
a spend meter: annotation done by any other means counts toward it, nothing
is ever paid for twice, and reopening a finished plan is just raising the
number. `plan_cycle` clamps every cycle to `target − annotated`, the
handoff clamps what may enter annotation the same way, and a target of 0
means no goal — the plan does nothing rather than run to 100%. The handoff
always sweeps the collection's **scraped-but-unannotated backlog first**,
bounded by the target — the cheapest step toward it — and because the
handoff outranks the plan step in the tick, new scraping starts only once
that backlog is clear. Slices then interleave two processes that both buy
**whole collection-days** (the unit every analysis floors on): Process B
("deep dive") takes consecutive recent days uncapped (what Sessions needs),
Process A ("spread") samples up to `a_days_per_month` whole days per month
backwards through history, capped at `a_day_cap` per day (the
Timelines/Correlations long arc), with `sample_share` splitting each
cycle's items between them. With any deep-dive share above zero the plan
can eventually reach everything processable; only at 100% spread (or under
an `earliest_date` floor) do the spread limits cap the final coverage — the
panel warns when the chosen target sits above that line. Plans, cursors and
targets live in `cache/collection_enrichment.json`; they are armed from the
Edit Collections modal, the site-wide switch is the
`auto_enrichment_enabled` admin setting (ships **off**), and the triggers
are worker completions, the end of each consolidation, and an hourly Cloud
Scheduler heartbeat on `/internal/run-task/enrichment_supervisor`. The
loop's consolidations run with the downstream refresh **deferred**
(`auto_refresh=False`, tagged `plan_deferred`); the impact accumulates in the
ledger's `deferred_impact` entry with `from_plan` set, and the supervisor's
finalize spends it once per cycle when the loop goes quiet, or after a 24 h
backstop. **That is the only deferred debt the supervisor may spend.** An
operator's own consolidate-without-refresh writes the same entry without
`from_plan`, and the finalize leaves it alone — it waits on the Dataset
Assembly page for "Refresh All Affected". A plan deferral landing on top of
an operator's makes the merged entry the loop's (the flag is sticky), since
the plan's cycle needs that refresh and it covers the operator's items too. The
annotate-side eligibility predicate is shared with the manual queue builder
(`collection_enrichment.annotation_eligible`) so the "never annotate-queue
unscraped items" rule has exactly one implementation; the plan's
`in_flight` ledger list records queued scrapes for stall detection only.
Three facts about the loop's relationship with the queues, learned the hard
way on 2026-09-04: (1) **the drain step serves the platform queue whoever
filled it** — `_drain` filters by platform, not by who queued the items, so
arming a plan adopts a colleague's *Build scrape queue* and runs it first;
the Edit Collections panel therefore asks (`queue_preview`) before *Arm* /
*Resume* / *Run a cycle now* when a queue holds videos that are not the
collection's own, offering to drain them first or empty them. (2) **The
stall counter is reset by every productive handoff**: `_plan` reloads the
plan entry immediately before its read-modify-write, because the handoff
earlier in the same boundary tick resets `stall_count` and prunes
`in_flight`, and the snapshotted entry would put the stale values back (a
healthy plan was parked on its fourth cycle that way). (3) **A job the loop
started owes a consolidation** (`__meta__.settle_owed`, set when the loop
starts a scraper or annotator, cleared when it consolidates): a plan parked
or finished while its job still runs leaves results that no tick would
otherwise fold in, so the no-plans path settles that debt before the quiet
finalize. Every one of these decisions is written to the **enrichment
history** (`services/enrichment_journal.py`, `cache/enrichment_journal.json`,
a bounded ring): plans armed/paused/parked, queues built/emptied/drained
(with the split between the armed plans' own slices and everything else),
slices, handoffs, and every scraper, annotator, consolidation and refresh
run with its totals — written by the supervisor, the queue endpoints,
`start_process` (hand-started workers) and the workers' terminal exits.
Dataset Assembly shows the whole history; a collection's Edit Collections
panel shows its slice.

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
as a batch-and-chained `sessions_refresh` worker. Within each link the
per-session segmentation — pure Python, and nearly all of a rebuild's wall
time — runs on a forked process pool over (collection, session-chunk) work
units (`[sessions] workers`, default one per core less one; serial where
`fork` is unavailable), and every link logs a `[TIMING] sessions_link` line
splitting its time into load / vectors / segment. The worker count never
changes the rows. On a hosted deployment the links also cache the dense
embedding parts whole on the task-runner instance (`[sessions]
vector_cache`, memory-backed `/tmp`, keyed by store fingerprint) — a batch's
scattered rows otherwise cost a near-whole read of the store per link.

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

Refresh dependencies (each a background job). The graph is declared once, in
`web_interface/services/refresh_pipeline.py`:

```
consolidate → embeddings → video_map (niches) → study definitions → { meta ‖ pca }
                                    └────────────────────────────→ { timelines ‖ sessions }
```

Timelines and sessions both read the niche map — timelines joins the niche
columns through `new_merge`, sessions reads the map's trend columns — so a map
rebuild invalidates them even when no study changed. They are scheduled as fork
leaves regardless, which keeps the dispatch an out-tree with a single fan-out
and no join to build; the multi-parent dependency lives in their predicates.

**Any** step can start a run, not only a consolidation: starting one from its
Dataset Assembly card plans the same cascade of dependents. What actually gets
dispatched is decided one completion at a time from what each finished step
reports — `map_niche_changed` / `map_cold_start` from the map, `studies_changed`
from the study refresh, `embeddings_embedded_run` from the embeddings worker.
A warm-started map rebuild that moves no video between niches therefore runs
nothing downstream at all. Only a positive "nothing changed" prunes; an absent
signal is unknown, and unknown always runs.

The niche map's fingerprint for study-cache freshness is a hash over its
`(item_id, niche)` pairs, not a file stat: `build_niche_map` rewrites the
parquet on every run (fresh 2D coordinates, a new `built_at`), so a stat would
report a change after a rebuild that moved nothing and force every study to
rebuild anyway.

`sessions_refresh` runs in `stale_only` mode: it re-segments only the
collections whose coverage windows or in-window play/annotated counts moved,
and returns immediately when none did — a second line of defence behind the
planner's own decision. `skip_if_busy` keeps it off the toes of a sessions run
already in flight (one is also chained after every study save, which is a plain
chain rather than a run). It can still be run on its own from Data Pipeline →
Dataset Assembly, where "Force full rebuild" re-segments every covered
collection.

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
