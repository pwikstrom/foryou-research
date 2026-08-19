# Extending the Hub

Developer guide to the three extension points of The For You Data Hub:
adding a platform, adding or updating an annotation backend, and adding an
embedding backend. All three follow the same registry design described in
[architecture.md](architecture.md) — an ABC with an `__init_subclass__`
auto-registry, so most of the work is one subclass plus declarative config.
The contract files that drive the schemas are documented in
[contracts.md](contracts.md); configuration keys in
[configuration.md](configuration.md); module conventions in `DEVELOPING.md`.

One rule applies everywhere: cite and import the canonical subpackage paths
(`fyp/scrape/platform_scraper.py`, `fyp/annotation/backends/`, ...), never
the old flat `fyp/<name>.py` alias shims — required in thread-pool bodies,
preferred everywhere else (see the shim-poisoning note in
[architecture.md](architecture.md) §"Package layout").

## Adding a new platform

The registry design means no orchestration edits: per-platform scrape
queues (`to_scrape_<platform>.json`), the derived worker processes
(`web_interface/process_manager.py` builds one `queue_scraper_<platform>`
process per platform in the contract's platform list), media
subdirectories, and the scrape version registry
(`fyp/scrape/scrape_versioning.py` — the platform set is part of the `sv_`
version identity, so a new platform forks a new scrape version
automatically) all derive from the contract and the registries. What does
need touching is longer than the three-line summary in
[pipeline.md](pipeline.md); here is the full checklist.

### 1. Scraper subclass

Create `fyp/scrape/<platform>_dl.py` subclassing `BaseScraper` from
`fyp/scrape/platform_scraper.py`. Use `instagram_dl.py` or `youtube_dl.py`
as templates (all current scrapers are yt-dlp-based, but nothing requires
that). Set the `platform` class attribute to the platform key and implement
the five abstract methods:

- `item_url(item_id)` — canonical web URL for an item id.
- `fetch(item_id, *, save_media, save_path, ...)` — fetch one item's raw
  single-row DataFrame in the platform's native column names and, when
  `save_media`, store its media. Failures return an *empty* frame carrying
  `attrs['error_type']` / `attrs['error_detail']`; a metadata-ok /
  media-failed row instead carries `attrs['media_error_type']` and
  `video_downloaded=False` so the orchestrator can retry the media phase.
- `map_to_canonical(raw)` — rename native columns to the canonical
  contract names. Pure rename, no derivations — by convention a module-level
  `_RAW_TO_CANONICAL` dict fed to `raw.rename(columns=...)`.
- `classify_error(error_type)` — map a fetch error category to a
  `scrape_status`: `"ok"`, `"permanent:<reason>"`, or
  `"transient:<reason>"`. The prefix decides whether the item is pruned
  from the retry queue; the categories in `THROTTLE_CATEGORIES`
  (`rate_limited`, `bot_check`) additionally shrink batch concurrency.
- `repair_counts(df)` — fix platform count quirks (e.g. 32-bit overflow)
  before rates are derived; return `df` unchanged if counts are clean.

Optional overrides, all with working defaults: `throttle_limits`
(per-batch concurrency bounds), `inter_request_delay` (per-worker pacing
for session-level rate limits), `health_check` (pre-batch auth/quota
probe), `media_probe_url` (system-health CDN reachability probe),
`prepare_raw_batch` (raw-frame fix-ups before canonicalization),
`media_duration_cap` / `should_download_media` (media-phase gating), and
the carousel trio `slideshow_image_column` / `fetch_slideshow_audio` /
`image_count` — set `slideshow_image_column` only if the platform has
photo/carousel posts; the orchestrator then assembles slideshow mp4s from
the images your `fetch` saved.

### 2. Register the module

Add the new module to the `_SCRAPER_MODULES` tuple near the top of
`fyp/scrape/platform_scraper.py` (~line 73). This is the one hardcoded
list on the scraping side: `get_scraper()` imports these modules lazily so
subclasses self-register without a circular import — a module not listed
there never registers, and `get_scraper("<platform>")` raises.

### 3. Scrape contract

In `config/scrape_contract.toml`:

- Add the platform key to `[meta].platforms`. This is explicit, not
  derived: a platform whose raw fields all map to generic base names owns
  zero `scope="platform"` fields (Instagram is exactly this) yet still
  needs its queue, worker process, and version-descriptor entry.
- Prefer mapping raw fields onto the generic **base** fields
  (`fave_count`, `comment_count`, `share_count`, `save_count`,
  `author_handle`, ...) via `_RAW_TO_CANONICAL` rather than minting new
  fields. Add `scope = "platform"` `[[fields]]` blocks only for genuinely
  platform-specific data.
- The flat `[perk]` table maps each `*_per_K_play` rate to its count
  field; `BaseScraper.derive_engagement_rates` reads it. Counts the
  platform can't produce simply stay NA, and so do their rates — nothing
  to declare.

### 4. Ingestion subclass

Create `fyp/ingest/<platform>.py` subclassing `ForYouBaseCollection`
(`fyp/ingest/base.py`). Class attributes: `source_platform`, `raw_path`
(registration also self-registers the raw-upload storage location),
`platform_url_template`, and the upload-filter classmethods
`accepted_upload_suffixes` / `zip_member_suffixes`. Implement the two
hooks `load_single_raw(filename)` and `process_single(df)`; the base owns
the load loop, manifest, ledger, dedup, and timestamp finalization
(see [pipeline.md](pipeline.md) §1).

Then import the class in `fyp/ingest/__init__.py`. Unlike the scraper
side this import is eager and its **order is pinned** (tiktok → instagram
→ youtube → yours at the end) so registry order stays byte-identical;
`tests/unit/test_subpackage_shims.py` guards it.

### 5. Activity contract

If the platform's donation export carries activity fields the shared
schema lacks, add platform-scoped fields to
`config/activity_contract.toml`. Most platforms need nothing here.

### 6. UI

`web_interface/templates/tabs/dm/scrape.html` holds the one hardcoded
platform list in the templates: the `platform_display` Jinja map (~line
35) that turns keys into display names ("tiktok" → "TikTok"). Without an
entry the new platform's card on Data Pipeline → Scrape falls back to
`|capitalize`. Every other UI surface derives from the contract and
registries.

### 7. Cookies

Nothing to code: `fyp/scrape/scraper_cookies.py` is fully generic. Each
platform's Netscape-format cookie file lives at
`gs://<bucket>/secrets/<platform>_cookies.txt` on Cloud Run (cached
locally for six hours) and is read from the local Chrome profile in dev.
You only need to *provide* the file — via Admin → Scrapers — if the
platform requires an authenticated session.

### 8. Structure sentinel baselines

The sentinel (`fyp/core/structure_sentinel.py`) learns donation-export
structure per `(source_platform, data_source)` and quarantines drifted
uploads. A new platform starts learn-only (every accepted file trains the
baseline) until enough files accumulate; to seed the baseline from an
existing corpus of accepted files, run
`scripts/bootstrap_structure_baselines.py`.

### 9. Optional config

`[misc] max_duration_for_download_<platform>` in `config/config.toml`
overrides the global `max_duration_for_download` media cap for one
platform (`BaseScraper.media_duration_cap` reads it). Metadata is always
scraped; the cap only gates the media phase.

### 10. Tests

Mirror the existing per-platform suites in `tests/unit/`:

- a scraper test (`test_instagram_scraper.py`, `test_youtube_scraper.py`)
  exercising `map_to_canonical`, `classify_error`, and `repair_counts` on
  representative raw rows, including the failure/attrs contract;
- an ingest test covering `load_single_raw` / `process_single` on a
  fixture export;
- `test_scrape_contract_platforms.py` and
  `test_scraper_process_names.py` pick the new platform up from the
  contract automatically — run them to confirm the wiring;
- if the platform has carousels, cover the slideshow hooks
  (`test_scraper_slideshow_hooks.py` is the pattern).

## Adding or updating an annotation backend

Two very different tasks share this heading; be sure which one you are
doing.

### Swapping or pinning a model: config only

Moving an *existing* backend to a new model version needs no code. Edit
`model` (Gemini) or `model_id` (the others) in the backend's
`[machine.<backend>]` block in `config/config.toml` — or, to keep two
model generations selectable side by side, declare a
`[machine.<backend>.variants.<name>]` block and pick the variant in
Admin → Backends. See [configuration.md](configuration.md) §"Pinning or
A/B-ing annotation model versions" for the variant mechanics. Either way
the changed effective model flows into the version descriptor and forks a
new `av_` annotation version automatically; old rows stay readable as
legacy.

### Authoring a new backend

A backend is one class that produces the production raw-row dict for one
item; everything downstream (threading, flatten, refine, versioning,
queue pruning) is backend-agnostic.

1. **Registry.** `fyp/annotation/backends/__init__.py` needs the new id
   in *both* `BACKEND_IDS` (the closed, settings-visible id set; order =
   UI order) and `_BACKEND_MODULES` (id → module path, imported lazily so
   optional dependencies never load at package import).
2. **Subclass** `AnnotationBackend` from
   `fyp/annotation/backends/base.py`. Class attributes: `name` (must
   equal the registry id — registration keys on it), `max_workers`
   (thread-pool width; a resident local model is effectively 1),
   `supports_batch_mode`, and `cloud_run_capable` (False for anything
   needing the host machine). Implement the two abstract methods:
   - `availability(deep)` — returns a `BackendAvailability` with
     actionable per-check detail; with `deep=False` it must stay a cheap
     local check safe on every page load, `deep=True` may probe the
     network/model.
   - `annotate_one(item_id, platform, ...)` — returns the raw-row dict
     documented in `base.py`'s module docstring. Failures are reported
     in-band (`error` set, `finish_reason` starting `"DNF"`), never
     raised. It runs as a thread-pool body, so any lazy import inside it
     must use canonical `fyp.<subpackage>.<module>` paths.
3. **Version identity hooks.** The overridables `prompt_suffix`,
   `version_extra_params`, `effective_model_id`, and `version_gen_params`
   feed the `av_` annotation-version hash. Get these right or the
   backend's output will be stamped with a version that doesn't reflect
   what produced it — annotations then fail to fork a new version when
   the model or an output-affecting parameter changes. Defaults are
   Gemini-shaped (they read `[machine.gemini]`), so a new backend
   overrides `effective_model_id` and `version_gen_params` at minimum;
   `version_extra_params` carries any additional output-affecting knobs
   (frame counts, sampling rates, ...) and must return `{}` when the
   backend has none.
4. **Config block.** Add `[machine.<id>]` to `config/config.toml` with
   the backend's model id and parameters (backend-agnostic keys like
   `max_duration_for_annotation` stay on the top-level `[machine]`
   table). Optional `pricing = {input = ..., output = ...}` (USD per 1M
   tokens) drives the pre-queue cost display; local backends omit it.
5. **Variant key surface.** Add a branch for the new id in
   `_known_override_keys()` in `fyp/annotation/backends/variants.py`.
   Unknown variant override keys only warn, but without the branch
   *every* key of a declared variant logs a typo warning.
6. **Optional dependencies.** Heavy deps go in a pyproject extra, not
   the base requirements — the pattern is `local_qwen` / `local_minicpm`
   in `pyproject.toml`, one extra per backend so installs stay minimal.
7. **UI.** The backend appears in Admin → Backends
   (`web_interface/templates/tabs/admin/backends.html`) with its
   availability checks; extend that page if the backend needs its own
   requirement panel. The runtime selector is the admin setting
   `annotation_backend` — deliberately the *only* runtime switch. Model
   ids and generation parameters are config, not admin settings, because
   they are part of the version identity.

Constraints to know about:

- **Batch mode is Gemini-only.** `fyp/annotation/machine_annotation_batch.py`
  is written against the Gemini Batch API and reads `[machine.gemini]`
  directly; `supports_batch_mode = True` on another backend won't make it
  work there. New backends run the per-item threaded path.
- **Cloud Run refusal.** `web_interface/process_manager.py` refuses to
  dispatch `queue_annotator` / `queue_annotator_batch` as a Cloud Task
  when the active backend has `cloud_run_capable = False`, with a message
  telling the admin to switch backends or run locally.
- **The legacy inline Gemini path.** `fyp/annotation/machine_annotation.py`
  (~line 556) dispatches per-backend but keeps the historical Gemini path
  inline (`backend is None` for the plain `gemini` selection). Gemini
  *variants* ride the generic backend branch while still hitting the
  Gemini API — worth knowing when reading dispatch code, though a new
  backend only ever sees the generic branch.

Worked config example — backend-agnostic keys on `[machine]`, one backend
block, one variant:

```toml
[machine]
max_duration_for_annotation = 300      # backend-agnostic: never on a backend block

[machine.qwen_api]
model_id = "qwen3.5-omni-flash"
temperature = 0.0
max_workers = 4
pricing = {input = 0.10, output = 0.80}

[machine.qwen_api.variants.qwen_next]
label = "Qwen next-gen (pinned)"       # metadata — never part of the version hash
model_id = "qwen4-omni"                # override keys = the parent block's keys
pricing = {input = 0.20, output = 1.60}
```

Testing expectations: extend the registry/dispatch/variant suites
(`tests/unit/test_backend_registry.py`, `test_backend_dispatch.py`,
`test_backend_variants.py`) and add a per-backend test on the pattern of
`test_qwen_api_backend.py` / `test_minicpm_local_backend.py` — mocked
model calls, real raw-row-dict shape checks. Note that the golden
regression suite (`tests/golden/`) replays saved raw *Gemini* responses
through the parse/flatten/repair pipeline; a new backend deserves an
equivalent committed fixture set so its response shape is pinned the same
way.

## Adding an embedding backend

The embedding side (`fyp/analysis/embedding_backends/`) mirrors the
annotation registry but is simpler: no variants layer, no batch mode, no
version hooks — the model id itself scopes the store.

1. **Registry.** `fyp/analysis/embedding_backends/__init__.py`: add the
   id to `BACKEND_IDS` and the module to `_BACKEND_MODULES` (lazy import,
   same rationale — `qwen_local` needs torch, installed only via the
   `local_embeddings` extra).
2. **Subclass** `EmbeddingBackend` from
   `fyp/analysis/embedding_backends/base.py`. Class attributes: `name`
   and `cloud_run_capable`. Four abstract methods:
   - `model_id()` — the embedding model id, stamped per-row into the
     shard store's `model` column;
   - `dim()` — output dimensionality (stamped as `dim`);
   - `availability(deep)` — same contract as the annotation side (it
     reuses the same `BackendAvailability` type);
   - `embed_texts(texts, reporter)` — return an `(n, dim)` float32
     matrix. Rows whose embedding failed must come back as
     **zero-vectors, never raise per-row**: the caller drops all-zero
     rows before writing the shard so those items retry next run.
3. **Config block** `[embedding.<id>]` in `config/config.toml`; the
   runtime selector is the admin setting `embedding_backend`
   (Admin → Backends). The same Cloud Run guard applies: a
   `cloud_run_capable = False` backend blocks `embeddings_refresh`
   dispatch on Cloud Run.

There is no invalidation step when a model changes, because the store is
**model-scoped**: readers only see rows whose `model` column matches the
active backend's `model_id()`. Changing the model (by config edit or by
switching backends) simply starts re-embedding the corpus under the new
id, leaving old vectors intact — switch back and they are all still
there. This is also why `model_id()` must be honest: two configurations
that produce different vectors must report different ids.

Tests: `tests/unit/test_embedding_backend_registry.py` for the registry,
plus a per-backend suite on the pattern of `test_embedding_qwen_api.py`
(mocked API, zero-vector failure contract, dtype/shape checks); the
store-scoping behavior is pinned by `test_embeddings_store_scoping.py`.
