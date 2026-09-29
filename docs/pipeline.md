# Data pipeline

The path from a participant's donation zip to an analyzable study dataset.
Module references are to `fyp/` unless noted. Where a rule has a history,
the dated record of it is in the [decision log](decisions/README.md).

## 1. Ingestion (`fyp/ingest/`)

`ForYouBaseCollection` (`fyp/ingest/base.py`) is an ABC with an
`__init_subclass__` auto-registry. Each platform subclass declares
`source_platform` + `raw_path` and implements two hooks:

- `load_single_raw(filename)` — read one raw donation into a per-file DataFrame
- `process_single(df)` — produce `utc_timestamp` and finalize

The base class owns everything generic: the activity schema
(`REQUIRED_COLUMNS`, from `config/activity_contract.toml`), the load loop
(manifest, per-file donor timezone, ledger, dedup),
`_finalize_activity_frame()` (drops unparsed timestamps, sets `tz_offset`,
sorts chronologically), and `save_enrichment_seed()`. Current subclasses:
three TikTok variants (`TikTokDDPCollection` for the data-download export,
`TikTokAIOCollection` for the AIO capture, `TikTokZeeschuimerCollection` for
browser captures), `InstagramDDPCollection`, `YouTubeDDPCollection`.

At class definition (import time) `__init_subclass__` registers the class in
`ForYouBaseCollection._registry` **and** self-registers its raw-upload
location (`activity_data/{source_platform}/{raw_path}`) via
`data_io.register_location()`, so a new platform needs no `fyp_config` edit
and the upload routes see the location before any collection is
instantiated. `registered_raw_locations()` derives the whole upload-location
list from the registry, for code that must probe every location (e.g.
collection deletion). Raw uploads therefore land in one folder per subclass
under `activity_data/`; the TikTok folders predate that convention and are
named differently — see [configuration.md](configuration.md#storage-locations).

### Upload identity

`fyp/ingest/raw_names.py` generates a raw upload's stored filename and its
collection id (platform, source, upload time, random suffix); the browser's
filename is provenance only, kept on the manifest entry and as the default
**display ID**. Browser filenames collide — every TikTok export is called
`user_data_tiktok.json` — so they never decide where a donation is stored or
which collection it joins (see
[decision 0016](decisions/0016-generated-raw-upload-identities.md)).

- Raw locations and the archive are append-only at the storage layer
  (`data_io.move`/rename into them raise instead of overwriting), and the
  ingester reports a pending entry whose name is already taken rather than
  skipping it.
- An explicit collection id may append to an existing collection only under
  the same account.
- Display IDs name one collection each: `unique_display_label` suffixes
  ` (2)` at upload, and a rename onto a name another collection answers to
  (its display ID or its collection id, case and whitespace ignored) is
  refused with a 409. Only a rename is checked — the bulk edit and the
  modal's autosave resend the stored name on every tag tick.
- Pre-existing duplicates are flagged (`duplicate_display_ids`, an
  ops-report check and a *duplicate* pill in Edit Collections and the study
  picker) rather than renamed behind the operator's back. The pill is
  computed over the tags file, not the listing (the listing endpoint sends
  each row its `displayIdTwins`), because a twin can be a tags entry with no
  metadata row; the ops report marks such an id *(no data)* and names
  unowned ones under *Leftover collection entries*.

### Instagram and YouTube exports

The Instagram and YouTube ingesters parse **zipped** data-donation exports
into the platform-agnostic activity schema. They read zip members with
`utils.read_zip_members()` (over a `data_io.local_copy()` of the upload) and
repair double-encoded captions with `utils.repair_mojibake()`.

- **`InstagramDDPCollection`** (`source_platform="instagram"`,
  `raw_path="instagram_raw"`) reads
  `your_instagram_activity/story_interactions/stories_viewed.json`,
  `ads_information/ads_and_topics/videos_watched.json` and
  `ads_and_topics/posts_viewed.json` (viewed reels / feed videos / feed posts
  → `activity_type="play"`), plus `likes/liked_posts.json` (→ `fave`),
  `saved/saved_posts.json` (→ `save`) and the donor's own comments,
  `comments/post_comments_1.json` / `comments/reels_comments.json`
  (→ `comment`, text in `extra_data`, media owner as `seed_author_id`).
  Instagram names no media for a comment, so comment rows have no `item_id`
  and never fold. The two feed-impression streams are what give a liked or
  saved item a play row to fold onto. It supports **both** the current
  `label_values` record schema and the classic `string_list_data` /
  `string_map_data` schema. `item_id` is the reel/post shortcode parsed from
  the URL (nullable — classic story views carry no URL). Caption and owner
  are captured as enrichment-seed columns.
- **`YouTubeDDPCollection`** (`source_platform="youtube"`,
  `raw_path="youtube_raw"`) parses Google Takeout
  `history/watch-history.html`. `item_id` is the 11-character video id;
  organic watches → `play`, served ad impressions ("From Google Ads",
  detected from the details/caption cell) → `activity_type="ad_play"`, and
  non-video events (Shorts creation, community-post views) are dropped.
  Timestamps are parsed from the account's display locale — day-first *and*
  US month-first, 12-hour AM/PM (including narrow/no-break spaces), with an
  abbreviated zone or an explicit `GMT±HH:MM` offset. Engagement comes from
  the optional Takeout CSVs: `comments/comments.csv` → `comment` rows (text
  in `extra_data`), `playlists/Liked videos.csv` → `fave`,
  `playlists/Favorites videos.csv` → `save` — all exact-timestamped and
  video-id-keyed.

### Activity vocabulary and engagement linking

`fyp/core/utils.py` owns one engagement vocabulary across platforms (see
[decision 0022](decisions/0022-engagement-vocabulary-and-inferred-links.md)):
`VIEWING_ACTIVITY_TYPES` (`play`, `observe`, `ad_play`), `ENGAGEMENT_TYPES`
(`fave` = a like, `save` = a bookmark, `comment`, `share` including
reposts), `STANDALONE_ACTIVITY_TYPES` (`follow`, `followed_by`, `search`,
`login`, `post`), their union `KNOWN_ACTIVITY_TYPES`, and the UI label map
`ENGAGEMENT_LABELS` (Like / Save / Comment / Share / Follow). Every ingester
declares the subset it emits in `emitted_activity_types`;
`tests/unit/test_ingest_activity_vocabulary.py` checks the declaration
against the class's section maps, and `process()` writes a ledger note on any
file whose rows fall outside it.

What each platform's export calls these: TikTok `ItemFavoriteList` → `fave`,
`FavoriteVideoList` → `save`, `ShareHistoryList` (`share:<method>`) and
`RepostList` (`share:repost`) → `share`, `Following` → `follow`; Instagram
liked / saved posts → `fave` / `save`, comments → `comment`; YouTube Liked
videos / Favorites videos → `fave` / `save`, comments → `comment`.
`scripts/migrate_engagement_vocabulary.py` retags stored data written under
the older vocabulary, in which TikTok bookmarks were `fave`. The rules a new
platform follows are in [extending.md](extending.md#activity-types-and-engagement).

**Engagement→play linking (`extra_data`).** `derive_play_duration()`
(shared, platform-agnostic) folds engagement activities (fave/save/comment/
share — never follow, which names no item) into a play row's `extra_data` as
comma-separated `"<atype>[:context]"` tokens: first via chronological
same-item adjacency runs (which also attribute dwell), then via a
**same-item nearest-play fallback** for engagement not adjacent to any play
(e.g. an Instagram like whose only logged view of that item is days earlier —
Instagram's impression streams log a view once per item). Only `extra_data`
is affected by the fallback; `play_duration` stays strictly adjacency-based.
This folded token is the **only** engagement signal that survives into
studies, which filter to play/observe rows (see `services/explorer_backend.py`,
`parse_extra_data_tokens` from `fyp/core/utils.py`). A follow is counted
from its own row on the participant's My Collections page only.

**Every inferred link is named on its row** (`link_method`, a base
activity-contract column): a lead play carries `adjacent`, `nearest_play` or
`adjacent,nearest_play`; a TikTok comment whose video id came from the 180 s
forward fill carries `ffill_180s`; everything else is null. The contract
description documents the inference; the column lets an analysis exclude it.

### Enrichment seed

A subclass populates `seed_*` scratch columns (`seed_desc` /
`seed_author_id` / `seed_author_name` / `seed_create_time`) in
`load_single_raw`. `process()`'s column filter drops them from the activity
rows, but `save_enrichment_seed()` persists them separately as a per-platform
`{source_platform}_{data_source}_enrichment_seed.parquet` in the **canonical
scrape-base schema** (`config/scrape_contract.toml`), keyed on
`(source_platform, item_id)` with `scrape_status="donated"` and a
`scrape_contract_version` stamp. It merges across ingest runs (existing rows
survive; a captioned row wins a key collision over a caption-less duplicate),
and is a no-op for platforms that populate no seed columns (e.g. TikTok).

Consolidation merges the seeds as a **lowest-precedence fallback**:
`fyp.scrape._merge_enrichment_seeds` anti-joins donated rows against real
scrape rows on `(source_platform, item_id)` and appends the rest with
`scraped_ok=False` / `video_downloaded=False`, so unscraped (or permanently
unfetchable) items surface their donated caption and author in Explore while
staying scrape-eligible. A later real scrape supersedes the donated row on
the next consolidation, and seed-file row counts take part in consolidation
change detection. Donated-seed rows are never annotation-eligible until
really scraped.

### Donor time zone

The ingestion manifest accepts an optional `tz` per file — an IANA zone name
(`Asia/Kolkata`, preferred) or a fixed offset (`+05:30`, `-8`) — collected
in the upload modal and validated at upload time by
`fyp.ingest.parse_donor_timezone` (an unrecognised value is rejected with
HTTP 400, not silently dropped). When present it is the **authoritative**
source for local-time conversion on every platform, overriding the
(sometimes ambiguous, e.g. IST) timezone label in the export; YouTube needs
it to interpret local wall-clock times unambiguously. All three DDP parsers
reach it through `_finalize_activity_frame()` (see
[decision 0021](decisions/0021-donor-time-zone.md)).

`tz_offset` is **fractional hours** (`double[pyarrow]`: `+05:30` → 5.5,
Adelaide 9.5), per row from a supplied zone (daylight saving applied) or once
per file when inferred. What a parser had to resolve without a supplied zone
— YouTube's ambiguous IST/CST/BST/EST labels, an unrecognised label — is
written to the file's ledger `notes` (`ForYouBaseCollection.note_file`), not
only logged.

### Structure sentinel

`fyp/core/structure_sentinel.py` quarantines silent format drift instead of
ingesting it. It learns each (platform, data_source)'s upload structure —
zip members, typed JSON key paths (direct-message partner names and record
ids used as keys collapse to `chat history with *` and `<id>`), HTML markers
— plus per-file sanity stats (rows/MB; kept ratio = rows kept over the
records the parser should have read, so sections outside its whitelist never
count as drift; null-`item_id` fraction; seed fill rates) into
`structure_baselines.json` (location `recoded`).

- During an ingest refresh (`run_ingest_refresh` injects a per-run
  `StructureSentinel` into every sub-collection) each new file is checked
  twice: Phase A (raw structure, inside `load_raw` right after
  `load_single_raw`) and Phase B (processed-stat drift, before
  `migrate_sub_collections`).
- A deviating file's rows are withheld and its ledger outcome set to
  `quarantined_structure` (a `LEDGER_SKIP_OUTCOMES` member), with the verdict
  and findings persisted to `structure_verdicts.json` for the Data Pipeline →
  Ingest Collections "Structure review" panel (approve = learn structure +
  un-ledger → the next refresh ingests; reject = `manually_excluded`).
- Missing core structure, changed types and hard stat outliers quarantine;
  purely additive changes only warn (tunable via `ADDITIVE_QUARANTINES`).
  Baselines under 3 accepted files are learn-only (never quarantine); stat
  checks need 5. **A `warn` verdict ingests**: the file's rows go in and the
  verdict sits in the review panel for acknowledgement; only `quarantined`
  withholds rows.
- **Parse-rate floor (no baseline needed):** a file whose parser kept under
  `PARSE_RATE_FLOOR` (10 %) of its ingestible rows (raw rows minus the file's
  `outside_whitelist` count) quarantines with code `parse_rate_floor` even
  while the baseline is learning, because a young baseline cannot see a
  near-total loss and a warn verdict is easy to approve. The review modal
  shows "parser kept X of Y rows" before Approve (see
  [decision 0017](decisions/0017-ingest-review-sticky-approvals-parse-rate-floor.md)).
- An admin's approval **sticks**: the sentinel reads the stored verdicts at
  the start of a run and does not re-quarantine an approved file for findings
  of a kind the approval covered (one learned file cannot move a 20-file
  baseline), and `save_ledger` merges the run's changes into the stored
  ledger instead of overwriting it, so a review recorded while an ingest run
  held the ledger in memory is never erased.
- Sections a donor leaves out are never drift: the sentinel notes them as
  `withheld_sections` and ingests what was donated. Browser-reviewed uploads
  are evaluated against a separate `__reviewed` baseline (see *Browser-side
  review* below).
- Bootstrap from already-uploaded history with
  `python scripts/bootstrap_structure_baselines.py`. Sentinel failures never
  block ingestion (log-and-ingest), and `sub.sentinel = None` (e.g. ad-hoc
  scripts) disables it.

### Intake report, drop reasons and donor merge

The load loop records each file's true raw row count (including too-small
discards) and a per-file drop-reason breakdown (`file_stats_this_run`),
persisted in the ingestion ledger (`ingestion_ledger.json`,
`processed_rows` / `deduped_rows` / `dropped`):

- `outside_whitelist` — records in sections the parser never ingests, by
  design. A platform that excludes records on purpose records them itself
  (`self._record_file_drops(counts, "outside_whitelist")`, as the TikTok
  parser does for sections outside its whitelist) and the base subtracts
  them, so they are never conflated with parse failures;
- `not_parseable` — the residue of rows `process_single` could not read;
- `missing_required` — rows missing required-core fields, counted at the
  integrity gate;
- deduplicated rows — rows already in the archive (key: collection, item,
  timestamp, type — never `tz_offset`, so a re-donation with a corrected zone
  deduplicates and the newest offset wins).

The ledger is surfaced on Data Pipeline → Ingest Collections: the live "Last
run results" table plus a permanent "Ingestion history" panel
(`GET /api/manage/ingestion/ledger`) with plain-language labels, so an
uploader can always see why rows didn't land.

**Donor merge.** `identify_similar_file_content` clusters raw files whose
per-second timestamp sets overlap by more than 20 % of the smaller set, and
never on fewer than three shared seconds (`min_shared_seconds`). It runs once
over the **whole** table in `migrate_sub_collections`, across routes and
platforms, so a browser capture and an export are compared too; the
three-second floor keeps a few-event capture that coincides with one second
of a large export from merging with it.

### Parse failures stay pending

A structural failure in `load_single_raw` (unreadable zip, missing member,
invalid JSON, unsupported timestamp locale) **raises**; the load loop logs it
and leaves the file pending — it is *not* added to `discarded_raw_files` and
gets no ledger skip-outcome, so it is retried on the next refresh (e.g. after
a parser fix for a new export-format variant). This is distinct from a
legitimately-too-small donation, which *is* ledger-recorded as discarded.

### Browser-side review

For participant uploads the export is parsed entirely client-side
(`static/js/donation_review.js` — no network requests happen during review),
sections and individual rows can be pruned, and the pruned file is rebuilt
from the kept rows before upload. For TikTok, non-whitelisted sections (DMs,
settings, ads data, profile) are stripped in the browser by default via the
review manifest's `unmapped_policy: "strip"` (`fyp/ingest/tiktok.py`), and
login-history/IP rows are surfaced as a reviewable section. Reviewed uploads
are flagged `client_reviewed`, and the structure sentinel evaluates them
against a separate `__reviewed` baseline whose stat distributions fit pruned
files. Sections a donor leaves out are never drift on either baseline.

### Intake statistics and corpus replay

Two scripts describe the ingestion corpus for write-ups. Both run only
against a **downloaded snapshot** of `recoded/`, wired in through a
throwaway config directory under `--out` that `FYP_CONFIG_PATH` names (so the
checkout's `config.local.toml` and `.env` are never read), and refuse if
storage would resolve to GCS.

- `python scripts/intake_report.py --snapshot <dir> --out <dir>` computes
  attrition per route, the outcome distribution, the sentinel's denominators,
  false-positive split and time in quarantine (with a quarantined-vs-accepted
  comparison by route and donor region), time-zone resolution levels with a
  calibration of the inference against supplied zones, and the sensitivity of
  the session gap, comment-link window and donor-merge threshold.
  `--skip-parquet` gives the ledger and verdict parts in seconds;
  `--classification` reads the filled-in `quarantine_worksheet.csv` back.
  Time in quarantine starts at the ledger's `uploaded_at`, because a
  verdict's `ts_evaluated` is overwritten on every run and re-stamped after
  an approval.
- `python scripts/replay_ingestion.py --snapshot <dir> --out <dir>`
  re-ingests the snapshot's raw exports (TikTok `activity_data/ddp/ddp_raw`
  and `activity_data/aio/aio_raw`, plus `instagram/instagram_raw` and
  `youtube/youtube_raw`) one file at a time in donation order, leaving out
  byte-identical repeats of an earlier file unless `--keep-copies`, and
  finishing with `add_local_time_features` + `add_session_ids` as the refresh
  does. It runs through `load_raw` → `process` → `migrate_sub_collections` as
  `run_ingest_refresh` does, but without the sentinel or the post-save side
  effects, into a scratch store under `--out` (the snapshot is only read).
  Its `platform_mapping` report checks each platform's rows against the
  activity contract (every column present with its declared Arrow type,
  required values, activity types against the module's
  `emitted_activity_types`). It runs the code in the checkout, so it
  describes what today's pipeline does with the donation history, not what
  happened at the time. Each file's mtime is set to its donation time,
  because `ts_added_to_dataset` — and so the newest-wins dedup and the
  canonical collection id — comes from it. When a re-donation supersedes
  every row of an older file, the older file leaves no row to be a sibling,
  so `build_per_file_summary` reports the new file as `added_as_new`; the
  replay records the true merge from `last_cid_remap`.

## 2. Scraping (`fyp/scrape/`)

`fyp/scrape/scrape.py` is platform-agnostic orchestration: per-platform queues
(`to_scrape_<platform>.json`, `scrape_queues.py`), batching, threading, a
throttle controller, media-phase retry, the batch guards below, and
consolidation of scrape parquets. It calls the active scraper through the
base interface.

`BaseScraper` (`platform_scraper.py`) is the per-platform ABC (auto-registry
+ `get_scraper(platform)` factory), plus the shared, platform-agnostic
derivations (per-K engagement rates, `plays_per_day`, column
standardization). A platform implements five hooks — `item_url`, `fetch`,
`map_to_canonical`, `classify_error`, `repair_counts` — plus optional
overrides (throttle limits, pacing, batch size, health check, slideshow
hooks). All three current scrapers are yt-dlp-based:
`tiktok_dl.TikTokScraper` (the older PykTok-fork backend is retired, though
the row schema it defined is kept), `instagram_dl.InstagramScraper` and
`youtube_dl.YouTubeScraper`. The checklist for adding one is in
[extending.md](extending.md).

**Cookies** are managed per platform by `scraper_cookies.py`:
`secrets/{platform}_cookies.txt` on GCS with a 6 h `/tmp` cache on Cloud
Run, Chrome-profile cookies in local dev, and a
`cookie_health(platform, session_cookie=...)` probe that degrades to
file-age status when the session cookie has no expiry row (e.g. YouTube's
`__Secure-3PSID`).

### Where each scraper runs

Instagram and YouTube do not scrape from datacenter IPs in practice — both
wall Cloud Run off whatever cookies or PO tokens are attached — so their
queues are drained by a local install on a residential IP (runbook:
[DEVELOPING.md](../DEVELOPING.md#local-scrape-queue-drain-against-prod-gcs-residential-ip)).
That is a property of the scraper (`BaseScraper.residential_ip_only`): on
Cloud Run the queue worker refuses to start and the enrichment supervisor
leaves those queues alone, instead of burning them against the wall and
tripping guards whose state, shared through the bucket, would then hold the
local install off too.

### Authentication

TikTok and YouTube scrape signed in (locally, from the operator's own Chrome
profile). Instagram goes anonymous first and retries with the session
cookies only for a post it hides from logged-out viewers: each path alone
has broken, and anonymous-first keeps the account's footprint to the posts
that need it. The logged-in session is spent sparingly, because Instagram
logs it out when it is not: logged-in requests are spaced
`[misc] scraper_instagram_auth_interval` seconds apart across threads
(default 20), and the media leg downloads from the info dict the metadata
leg already extracted, so a gated post costs one logged-in call. A
logged-out session shows up as Instagram's login page where the API's JSON
should be; the scraper then classifies `session_expired`, makes no further
logged-in request that run, and the orchestrator stops the run without
charging any retry budget and raises a scraper alert asking for a fresh
login in Chrome (see [decision 0025](decisions/0025-instagram-authentication.md)).
Follow-gated Instagram posts stay permanently `private`.

### Media duration cap and platform specifics

`BaseScraper.media_duration_cap()` reads the optional
`[misc] max_duration_for_download_<platform>` config key, falling back to the
global `max_duration_for_download` (300 s); each `fetch()` calls
`should_download_media(duration)` between its metadata and media phases.
Skipping for length is not an error — the metadata row is saved with
`scrape_status="ok"` and `video_downloaded=False`. Most YouTube watch-history
items are long-form and deliberately stay metadata-only; Shorts and clips get
media (720p-capped DASH merge).

- **YouTube** format extraction needs the n-challenge solver: `yt-dlp-ejs`
  (requirements.txt) plus a JS runtime (deno in the Docker base image; node
  works locally); metadata extraction is solver-independent via
  `ignore_no_formats_error`. YouTube's bot wall is a distinct `bot_check`
  category in `THROTTLE_CATEGORIES` (shrinks concurrency). Media streams from
  datacenter IPs additionally require proof-of-origin tokens: the **bgutil
  PO-token provider** is integrated in script mode
  (`bgutil-ytdlp-pot-provider` pip plugin in requirements.txt + the matching
  provider script built with Node 22 in `Dockerfile.base` at
  `/opt/bgutil-ytdlp-pot-provider/server`, env `BGUTIL_POT_SERVER_HOME`,
  wired via `youtube_dl._pot_extractor_args()`; a no-op locally where the
  script is absent). Even with PO tokens a flagged or rotated cookie session
  can still hit the bot wall — re-export cookies from a closed incognito
  session if downloads stall.
- **Instagram** image-only posts (single photos and carousels) are fetched as
  images and become silent slideshows (see *Photo and carousel posts*); a
  carousel with no image segments fails `permanent:no_video`. Instagram's
  ambiguous "rate-limit reached or login required" is kept transient so
  throttled items stay queued.
- **TikTok** caps concurrency at 6 and reports cookie health via the
  `health_check` hook.

### Failure verdicts and batch guards

Each scraper classifies a failure into its own taxonomy, and
`classify_error` maps it to `permanent:<reason>` (pruned from the queue,
recorded in the failed-scrapes ledger) or `transient:<reason>` (kept for a
later run). One broken session can make every item read as permanently gone,
so three guards sit on top — each aborts the batch, stops Cloud Task
self-chaining, and raises a scraper alert for a human (see
[decision 0003](decisions/0003-scraper-storm-guards.md)):

| Guard | Trips on | The items |
|---|---|---|
| circuit breaker | 15 consecutive throttle verdicts | stay queued |
| permanent-storm guard | 15 consecutive identical *permanent* verdicts | demoted to transient, stay queued |
| transient-storm guard | 25 consecutive identical *transient* verdicts | already transient; chaining stops |

- **Circuit breaker** (`scrape.CIRCUIT_BREAKER_THRESHOLD`): consecutive
  `rate_limited` / `bot_check` outcomes across the fetch and media phases.
  YouTube rate-limits the whole session for up to an hour, and phrases its
  rate-limit response as "Video unavailable…", so YouTube's classifier checks
  rate-limit keywords **before** removal keywords.
- **Permanent-storm guard** (`scrape._permanent_storm_threshold`, default 15,
  `[misc] scraper_permanent_storm_threshold`) catches a broken session that
  fails every item with the same permanent classification (a flagged session
  reporting live posts as removed): it demotes those ids to transient (kept
  queued, excluded from the failed-scrapes record) and sets the
  `permanent_storm_tripped` / `permanent_storm_category` attrs. Trade-off: a
  genuinely dead, homogeneous queue run stays queued and stops the worker —
  recoverable, unlike false pruning. The guard counts media-leg verdicts too
  (metadata scraped, media failed); those rows are *results*, not failures,
  so the demotion loop never sees them — they stay queued through the
  media-retry rule below — and the guard's log line reports both
  populations.
- **Transient-storm guard** (`scrape._transient_storm_threshold`, default 25,
  `[misc] scraper_transient_storm_threshold`) covers the retryable side (a
  bot wall failing every item with one transient category, invisible to the
  other two guards) and raises the same persistent alert
  (`KIND_TRANSIENT_STORM`); no demotion is needed.
- **Network outage gate** (`fyp/scrape/connectivity.py`,
  `ConnectivityGate`): a local drain runs on a laptop whose network drops,
  and an outage must not read as a storm (see
  [decision 0027](decisions/0027-network-outages-are-waited-out.md)). Any
  failed item, on either leg, first asks the gate: a TCP probe to port 443 of
  the platform host and `[misc] connectivity_probe_host`. Online → the
  failure stands and feeds the guards as before. Offline → one worker waits
  (the rest queue on the gate's lock), then every item that failed during the
  outage is re-run (≤ `_OFFLINE_RERUNS` per item); an item whose failure
  meets an online probe while the outage is still being waited out, or that
  started before the outage ended, counts as the outage's. Outage failures
  never reach the throttle, breaker, storm guards, retry budgets or alert
  file. Past `[misc] scraper_offline_max_wait_seconds` (1800 s local, 120 s
  Cloud Run) the gate gives up: the batch aborts with an `offline` attr, the
  loop / chain stops, nothing is charged and no alert is raised or cleared.
  Before its bucket writes a batch that saw an outage holds (unbounded, local
  only) until the network is back, so its rows are not lost to a write
  timeout. `tests/unit/conftest.py` pins the probe online for every unit
  test.
- **Batch deadline** (`download_video_threads`): waves are counted at the
  throttle *ceiling* (`throttle_limits`' maximum), not the oversized thread
  pool — the pool is deliberately larger than real concurrency. The 1800 s
  clamp applies only on Cloud Run (the Cloud Tasks request deadline); a local
  drain is bounded by `[misc] scraper_local_batch_deadline_seconds` (default
  4 h). On timeout the batch sets `abort_event` so un-started workers return
  `batch_aborted` (transient, stay queued) and gives in-flight downloads one
  per-item ceiling to land — their rows are **kept** (`batch_deadline_hit`
  attr), never written off (see
  [decision 0023](decisions/0023-media-leg-failures-batch-deadline-youtube-pacing.md)).
- **YouTube pacing** (`YouTubeScraper`): every request rides one signed-in
  session (locally the operator's own Chrome cookies on a residential IP)
  and YouTube throttles the *session*, so the scraper caps concurrency at 2,
  sleeps 5 s per item (`BaseScraper.inter_request_delay()`, held inside the
  throttle slot) and caps a batch at 250 (`BaseScraper.max_batch_size`,
  honoured by both `scraper_loop_from_list` and `run_queue_scraper`);
  `[misc] scraper_youtube_max_concurrency` / `_inter_request_delay` /
  `_max_batch_size` override. Calibration: 2–4 concurrent with a 1.5 s delay
  soft-blocked after ~700 media pulls in 34 minutes.

### Media-leg failures

When `fetch()` succeeds on metadata but the media download fails, the
returned row carries `df.attrs['media_error_type']` / `media_error_detail`
(all three scrapers implement this; the contract is documented on
`BaseScraper.fetch`). The orchestrator saves the metadata row either way and
keeps the item in the scrape queue for a media retry (excluded from the queue
prune) **whatever the media error's category**: a permanent verdict on the
media leg is not trusted on its own, because a rate-limited session answers
a bare "Video unavailable" for live videos (see
[decision 0023](decisions/0023-media-leg-failures-batch-deadline-youtube-pacing.md)).
The ids ride out of `download_video_threads` in
`results.attrs['media_retry_ids']`. The media category also feeds the
throttle controller and the storm guards.

### Corroborated verdicts

The guards assume a healthy queue produces heterogeneous outcomes — which a
queue of nothing but retries never does, since retrying only the failures
distils it down to items that fail. A scraper may therefore mark a permanent
verdict *corroborated* (`attrs['verdict_corroborated']`, see
`BaseScraper.fetch`) when it rests on per-item evidence independent of the
error text: such a verdict neither extends nor resets a storm run, and is
pruned even when the guard trips (see
[decision 0024](decisions/0024-corroborated-permanent-verdicts.md)). YouTube
corroborates two cases, both read from the metadata leg, which adds the tv
player client — the only one that states *why* a video will not play — and
captures the reason yt-dlp otherwise swallows under
`ignore_no_formats_error`:

* the platform has no record of the video (no channel, no view count, no
  duration) **and** the stated reason is itself a removal. A video with no
  record is a failure, never an empty placeholder row;
* the record is intact but YouTube refuses to play it here, naming a region
  whitelist or a rights claim. Its metadata is scraped, the media leg is
  skipped, and the id leaves the queue with its metadata-only row standing.

A bare "Video unavailable" with the record intact is never corroborated: that
is exactly what a throttled session returns.

### Retry budgets

Retry budgets bound everything that stays queued, in per-platform sidecars
(`scrape_queues.py`). An item transiently failing through
`MAX_ZERO_PROGRESS_STRIKES` runs in which the queue as a whole made no
progress is given up on and recorded as failed. An item whose metadata
scraped but whose media did not is retried for `MAX_MEDIA_RETRY_STRIKES` (3)
healthy runs (`scrape_queues.charge_media_retry`, sidecar
`scrape_media_retry_strikes_<platform>.json`) and then pruned with its
metadata-only row standing — no failed-scrapes entry, the metadata did
scrape. An aborted batch never charges either budget — the verdicts
implicate the session, not the items — but ids that left the queue always
drop their strikes, and a batch that makes progress clears only the strikes
of the ids it pruned (one item's success is no evidence for another's).

### Scraper alerts and the failed-scrapes record

A tripped guard raises a **persistent scraper alert**
(`fyp/scrape/scraper_alerts.py`, `cache/scraper_alerts.json`, CAS via
`data_io.update_json`). A platform keeps one alert (within a batch a logout
outranks the storms, the storms the breaker). The Scrape page shows a red
banner and a failing health chip on that platform's scraper card, and the
Admin → System Information health panel shows a banner. It clears on the
next batch that produces results with none of these, or when an admin
dismisses it on the Scrape page (`POST /api/manage/enrichment/scraper_alert/dismiss`).
While it stands the enrichment supervisor does not start that platform's
scraper: it blocks the platform's armed plans instead, and they wait to be
armed again. The hold reads the alert, not the worker's task status, because
a local drain never writes that status file and a dismissal never clears it.

The failed-scrapes record stores each item's failure category so storms are
diagnosable after the fact. It stores retryable failures too (a timeout, a
storm-aborted batch); only an item whose **latest** record is final
(`permanent:*`, or a legacy bare id) counts as failed. `load_failed_scrapes()`
applies that filter, and it is what `scrape_fail` in
`enrichment_status.parquet`, the enrichment plan's skip and the coverage
bar's "failed for good" read, so a retryable failure never excludes an item
for good (see [decision 0026](decisions/0026-only-final-scrape-failures-count.md)).

The Scrape page's **"Retry missing media"** checkbox re-queues items that are
`scraped_ok` but `video_downloaded=False` and within the platform's duration
cap (unknown durations pass).

### Per-platform queues, workers and media layout

Each platform has its own scrape queue `to_scrape_<platform>.json` (owned by
`fyp/scrape/scrape_queues.py`; the legacy single `to_scrape.json`
auto-migrates into the default platform's queue on first read), drained by its
own `queue_scraper_<platform>` process. The platform rides in `task_args` and
is carried through self-chaining; `process_manager.SCRAPER_PROCESS_NAMES`
derives the process set from the contract's registered platforms. Every
scraped row is stamped with `source_platform` (a `scope="base"` contract field
with no var_schema metadata — the **activity** contract owns that var_schema
row); it is backfilled to the default platform for pre-column history at
consolidation, and the activity↔enrichment merge is composite on
`(source_platform, item_id)`.

New downloads write to `{gcs_media_prefix}/{platform}/{item_id}.mp4`;
readers (viewer streaming, annotation upload) resolve via
`fyp/core/media_paths.py` `resolve_media()` — the row's `storage_link`
first, then the platform subpath, then the legacy flat `{item_id}.mp4` path.
Existing flat TikTok media is **not** migrated; it keeps working via the
fallback. `ThrottleController` lives on `platform_scraper` (generic).

### Photo and carousel posts

Still-image posts are stored as slideshow mp4s and treated as videos
downstream. Division of labor (documented on `BaseScraper`): the platform's
`fetch()` downloads the source images as `{item_id}_{NN:02}.jpeg` and fails
with a retryable `carousel` category when a photo post's images can't be
extracted or downloaded; the orchestrator (`download_single_video`) detects
image posts via the `image_count` hook, assembles `{item_id}.mp4` with
`make_slideshow()` at `SLIDESHOW_SECONDS_PER_IMAGE` (2 s) per image, muxes the
post's audio track (music or TTS voiceover, fetched via the
`fetch_slideshow_audio` hook — yt-dlp `bestaudio` for TikTok; failure degrades
to a silent slideshow), uploads, and deletes the source jpegs.
`prepare_raw_batch` converts the raw image-URL list to a count and overrides
`duration = image_count × 2`. Instagram slideshows are silent (the post's
music is not fetched) and skip video segments inside mixed carousels.
Slideshows built before July 2026 are silent; historical media is not
regenerated.

### The scrape schema

`config/scrape_contract.toml` is the single declarative source for the
canonical, cross-platform scrape schema — the scraper's analogue of
`annotation_contract.toml` — loaded and validated by
`fyp/scrape/scrape_contract.py` (authoring keys:
[contracts.md](contracts.md)).

- It defines the **base** fields every platform emits (`scrape_status`,
  `storage_link`, `scrape_ts`, `source_platform`, `desc`, `create_time`,
  `author_id`, `duration`, `author_name`, `author_handle`, `play_count`, the
  generic absolute counts `fave_count` / `comment_count` / `share_count` /
  `save_count`, `comments_per_K_play` / `faves_per_K_play` /
  `shares_per_K_play` / `saves_per_K_play`, `plays_per_day`) and the
  genuinely platform-specific fields, each with its PyArrow dtype and
  var_schema metadata (role/scale/display_name/description/section).
- **Popularity counts and the author handle are platform-agnostic base
  fields**: each scraper's `_RAW_TO_CANONICAL` translates its platform names
  at scrape time (`stats_diggCount` / `ig_like_count` / `yt_like_count` →
  `fave_count`; `author_uniqueId` / `ig_author_handle` / `yt_author_handle` →
  `author_handle`; ...), and the flat `[perk]` table maps each `*_per_K_play`
  rate to its generic count.
- The registered platform list is the explicit `[meta].platforms` (a
  platform may own zero platform-scoped fields — Instagram does).
- At config load, `fyp_config._apply_contract_scrape_metadata` overlays that
  metadata onto `var_schema` (self-healing legacy→canonical rename via
  `LEGACY_COLUMN_ALIASES` + injection of any missing rows), and the admin
  schema editor renders those cells **read-only**, exactly like the
  annotation contract.
- Engagement per-K ratios and `plays_per_day` are derived at **scrape time**.
  Legacy on-disk scrape parquets are migrated at consolidation by
  `_coalesce_retired_columns` (retired platform-specific columns → generic
  base fields per `scrape_contract.RETIRED_TO_GENERIC`; a coalesce, never a
  rename — several sources share one target) followed by
  `_canonicalize_legacy_scrape` (legacy base-name renames + rate
  re-derivation). The retired columns' `web_*_prio` surface flags migrate to
  their generic successors automatically inside
  `var_presentation.load_presentation()`.

## 3. Annotation (`fyp/annotation/`)

Downloaded media is queued (`to_annotate.json`) and sent to the active
annotation backend, driven by a prompt + structured response schema
generated from `config/annotation_contract.toml` (`annotation_schema.py`).
Annotation runs as a self-chaining Cloud Task
(`web_interface/workers/run_queue_annotator.py`), one batch per task.

Annotation covers every platform. Eligibility is decided at queue entry
(`routes/management/enrichment.py`): `scraped_ok` AND `video_downloaded` AND under
`max_duration_for_annotation` — metadata-only items (e.g. YouTube long-form
past the media duration cap) never queue. An unscraped item in the annotate
queue fails ("DNF - file not found") and is pruned as failed, so unscraped
items are never annotate-queued. The queue stays a bare list of item ids;
each entry's platform is resolved via `machine_annotation.platform_map_for()`
(an `enrichment_status.parquet` lookup, fallback: the default platform) and
drives media resolution (`media_paths.resolve_media`) plus the per-row
`source_platform` stamp on annotation output. Annotation rows are keyed
composite `(source_platform, item_id)` throughout (archive dedup, active
view, the scrapes←annotations merge); legacy platform-less rows are
backfilled to the default platform at consolidation. Each row is stamped with
the annotation version (`annotation_versioning.py`, `av_` hash) so superseded
fields remain readable as "legacy"; the active/preferred version vocabulary
is explained in [contracts.md](contracts.md#the-runtime-annotation-contract).

### Annotation backends

Machine annotation is pluggable: `fyp/annotation/backends/` holds an
`AnnotationBackend` ABC (auto-registry, `get_backend()` /
`active_backend_name()`); the **raw-row dict** is the interface boundary, so
flatten/refine/versioning are backend-agnostic. The four implementations:

- `gemini` — default; a thin adapter over the historical
  `machine_annotation` path.
- `qwen_api` — hosted Qwen omni via DashScope's OpenAI-compatible intl
  endpoint, default `qwen3.5-omni-flash`; native video including audio,
  base64 upload, streaming-only, `json_object` + schema-in-prompt +
  fence-strip, 429 backoff; key `DASHSCOPE_API_KEY`, config
  `[machine.qwen_api]`, `cloud_run_capable=True`; throughput is bound by the
  account rate limit (~5 videos/min), so `max_workers` stays small.
- `qwen_local` — Qwen3-Omni-30B via mlx-vlm, Apple Silicon only,
  frames+audio, llguidance-constrained JSON; mlx-vlm 0.6.x bugs patched in
  `qwen_rope_fix.py` (upstream Blaizzy/mlx-vlm#1619/#1620); `mlx-vlm` ships
  as the `local_qwen` pyproject extra, never in requirements.txt.
- `minicpm_local` — MiniCPM-o 4.5 9B, the same mlx-vlm frames+audio recipe
  reused from `qwen_local`'s helpers, ~8 GB peak so it fits 16 GB Macs; the
  published MLX quants need the checkpoint-naming patch in
  `minicpm_sanitize_fix.py`; extra `local_minicpm`, checks in
  `minicpm_support.py`.

Selection and configuration:

- The backend choice lives in the **admin settings store** (Admin →
  Backends; `fyp/annotation/backends/settings.py` is the read side,
  `web_interface/services/admin_settings.py` the write side). The five
  `[machine.gemini]` params (model / temperature / thinking_budget /
  media_resolution / max_output_tokens) are **config-file-only**: edit
  `config/config.toml` and restart or redeploy, because model and parameters
  are part of the version identity (see
  [decision 0004](decisions/0004-gemini-parameters-config-only.md)).
- **Backend variants** (`fyp/annotation/backends/variants.py`) let a
  `[machine.<backend>.variants.<name>]` block declare a named selection = the
  parent implementation plus config overrides (typically `model` /
  `model_id`), so a legacy and a new model version of the same backend stay
  selectable side by side (e.g. `gemini` on 3.0 and a `gemini_35` variant on
  3.5). The admin setting `annotation_backend` stores either an
  implementation id or a variant name; variants inherit availability,
  `cloud_run_capable` and worker width from their implementation, appear
  automatically in the Admin dropdown and the per-arm ab_eval picker, and
  fork their own `av_` versions (identity stays content-based:
  model+prompt+schema+params; the variant name is descriptor metadata only).
  The plain `gemini` selection keeps its byte-identical legacy hash path.
  Variant mechanics for operators: [configuration.md](configuration.md#pinning-or-ab-ing-annotation-model-versions-backend-variants).
- Constraints: variant names are lowercase `[a-z0-9_]` and must not collide
  with a backend id; batch mode runs only on plain `gemini`; a local backend
  loads one resident model per worker process (switching local-model variants
  needs a worker restart).
- Every backend block or variant may carry `pricing = {input, output}` (USD
  per 1M tokens, metadata, never hash-affecting); `variants.selection_pricing()`
  feeds the A/B-evaluation cost display (Admin → Contracts).
- **Changing model or params forks a new `av_` annotation version
  automatically**: model + generation params are hashed into the version
  identity; a local backend's prompt addendum and frame/audio sampling
  params fold in too via `extra_params` (additive-only, existing Gemini
  hashes unchanged).

Safety nets:

- `annotation_configured()` dispatches to the active backend's
  `availability()` (hardware/deps/model-download checks for qwen via
  `qwen_support.py`), surfaced in the Admin → Backends requirements panel,
  `GET /api/manage/annotation/backends`, `scripts/setup.py --check-only`,
  and the System Health annotation chip.
- A local backend refuses on Cloud Run (`cloud_run_capable=False`, plus a
  defence-in-depth guard in `process_manager.start_process`); batch mode
  stays Gemini-only (the worker refuses otherwise).

**A/B evaluation.** ab_eval (the A/B-evaluation panel on Admin → Contracts)
runs explicit test arms via `arms_spec` — the same contract may run as
several arms under distinct labels, each with its own `backend` (UI: per-arm
backend dropdown; the legacy `candidate_names` / `include_live` / `arm_params`
API shape still works, including per-arm `model` / `temperature`). Numeric
metrics report exact agreement + mean-abs-diff as the headline (Pearson r
flagged/suppressed under low variance); items lacking a usable annotation
from both arms of a pair are excluded. Enabling the local and hosted
backends: [installation.md](installation.md#enabling-local-qwen-annotation);
authoring a new one: [extending.md](extending.md).

### The Gemini batch-API lane

The Gemini *batch-API* lane (`run_queue_annotator_batch.py`) stages its
request JSONL under `machine_annotations_batch_input/` and reads the job's
results from `machine_annotations_batch_output/<ts>/` exactly once. Those
files are dead after ingestion and the code never removes them, so the
bucket needs a GCS lifecycle rule (delete after 30 days on both prefixes);
production has one, a fresh cloud deployment must add its own, e.g.
`gsutil lifecycle set <rules.json> gs://<bucket>`.

Regression protection: `tests/golden/` replays saved raw Gemini responses
through the whole parse/flatten/repair pipeline — run it
(`python tests/golden/run_safety_net.py`) whenever you touch annotation code.

## 4. Consolidation & recoding (`scrape.py`, `organize_datasets.py`, `recode_variables.py`)

"Consolidate & Refresh" (web UI) folds new scrape/annotation parquets into
the enrichment store, merges enrichment seeds, migrates legacy columns to
canonical names, and detects value changes from re-scrapes (so affected
studies auto-refresh). `organize_datasets.new_merge` joins activity with
enrichment on `(source_platform, item_id)`; `recode_variables.py` derives
analysis variables per the var_schema (type-driven generic recoder).

**Pre-scraper merge safety.** A freshly-ingested platform has activity rows
but no scrape or annotation enrichment yet. `new_merge` always emits the
enrichment-status and derived columns for both branches:
`_ensure_enrichment_status_columns` guarantees `scraped_ok` /
`annotated_ok` / `annotated_fail` / `video_downloaded` (False-filled when
absent) and `_add_merge_calculated_columns` guarantees
`days_since_created` / `plays_per_day` / `scraped_fail` / `completion_rate`
(NA/False-defaulted when their inputs are absent), so Explore and Video
Analysis, which gate on these flags, render a clean empty result instead of
erroring on a missing column. `update_enrichment_status`'s item-id-length
sanity filter is **per `source_platform`** (modal id length computed within
each platform group): a single global modal length would drop every
shorter-id platform's items (TikTok ~19 digits vs Instagram/YouTube ~11
characters). It falls back to the global modal when `source_platform` is
absent.

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
`tests/golden/test_incremental_consolidation.py`, run by
`tests/golden/run_safety_net.py` (and so by `scripts/verify.sh` and CI) —
plain `pytest` collects only `tests/unit/`.

**Scrape → annotate handoff for participant first batches.** A
participant's prioritised first batch is queued to the *scrape* queue only,
at ingest; the items reach the annotate queue at **consolidation** — the
moment scrape results become visible — once they show as
scraped-but-unannotated, exactly once per ledger entry
(`web_interface/services/participant_enrichment.py`, invoked from
`run_consolidate_enrichment.py`). The ordering follows from the rule that
unscraped items are never annotate-queued. First-batch auto-enqueueing ships
**disabled** (`AUTO_ENQUEUE_ENABLED = False` in `participant_enrichment.py`):
the ledger/handoff/notification machinery stays wired but is a no-op until an
operator enables it.

### Automatic per-collection enrichment

An armed collection is enriched unattended by a *supervisor* loop
(`web_interface/workers/run_enrichment_supervisor.py` +
`web_interface/services/collection_enrichment.py`; how the worker itself
runs is in [web_interface.md](web_interface.md#background-workers)). Each
short tick either starts a queue worker, consolidates, hands newly scraped
items to the annotation queue, or cuts the next slice into the scrape queue —
one action per tick, so the loop unrolls to
`plan → scrape → consolidate(light) → annotate → consolidate(full refresh)`.

**The target.** A plan's goal is an **annotation target** — keep going until
this many of the collection's unique videos are annotated. The target is a
*state*, not a spend meter: annotation done by any other means counts toward
it, nothing is ever paid for twice, and reopening a finished plan is just
raising the number. A target of 0 means no goal — the plan does nothing
rather than run to 100%. Arming stamps `run_started_at` and
`run_start_annotated` on the ledger entry — the annotated count read from the
data at that moment — which is what the modal's run meter measures against; a
resume keeps them, a re-arm replaces them.

**Sizing a cycle.** `plan_cycle` clamps every cycle to `target − annotated`
**inflated by the plan's expected yield** (scrape success × annotation
success, measured from its own recent runs in the enrichment history, 0.85
until there is history), because a slice cut to exactly the shortfall comes
back short and costs a whole extra cycle. `_auto_cycle_items` caps a cycle at
ONE annotation job (2,000), the size the scraper can feed during one job's
turnaround, since the loop serialises on consolidation and a larger slice
only delays the first annotation. The clamp counts videos already queued or
claimed for annotation as done (they are invisible to enrichment status
until consolidated), scrapes carry a 5 % margin (`CUT_MARGIN`), and the
handoff allows for the measured share of annotations that fail — together
what makes a plan's last cycle actually its last. A slice is never smaller
than `MIN_CYCLE_ITEMS` (200) while the plan still needs anything: a cycle's
fixed cost is the same for one video as for two hundred, and cuts sized to
the exact shortfall shrink toward one-video cycles (see
[decision 0018](decisions/0018-enrichment-slice-sizing.md)). The handoff
annotates whatever the plan's own slice scraped, so the plan may overshoot
its target by at most that floor.

**The handoff and the annotation lane.** The handoff clamps what may enter
annotation to the target, the same way. It always sweeps the collection's
**scraped-but-unannotated backlog first**, bounded by the target — the
cheapest step toward it — and because the handoff outranks the plan step in
the tick, new scraping starts only once that backlog is clear. Batch jobs are
not started small: while more scrapes are on their way the annotation lane
holds a queue below `MIN_ANNOTATE_BATCH` (500) for the next handoff, bounded
by `MAX_ANNOTATE_HOLD_MIN` (45) and never when nothing more is coming — the
lane therefore runs LAST in the tick, after the handoff and the next slice.
The annotate-side eligibility predicate is shared with the manual queue
builder (`collection_enrichment.annotation_eligible`), so the "never
annotate-queue unscraped items" rule has exactly one implementation. The
loop's consolidations are started through `_start_consolidation`, which
seeds a consolidate-only refresh-run record so the Refresh Pipeline chart
draws them like a manual consolidation.

**Deep dive and random daily sample.** Slices interleave two processes that
both buy **whole collection-days** (the unit every analysis floors on):
Process B ("deep dive") takes consecutive recent days uncapped (what Sessions
needs); Process A (the **random daily sample** in the UI; "spread" in the
code) samples whole days per month backwards through history, capped at
`a_day_cap` per day (default 50 — the Timelines/Correlations long arc), with
`sample_share` splitting each cycle's items between them. Whatever the spread
cannot spend the deep dive walks on with (and vice versa; a zero share stays
disabled), and on the plan's **last slice** — the one the target rather than
the cycle size bounds — the deep dive may buy part of a day, leaving its
cursor on that day so a later target raise completes it first.

**Whole sessions within a day.** Within a cut day — the spread's capped days
and the deep dive's partial last day — the cutter takes whole viewing
sessions first (`_pick_in_day`): the day's candidate sessions (at least the
Sessions tab's `min_session_plays`, resolved by `session_min_plays()` from the
admin store and passed into the pure planner), the session that crosses the
cap included, then single items up to the cap. A scattering of items never
yields a session anyone can analyse; whole sittings do, and this puts
analysable sessions across the whole history rather than only inside the
deep-dive window. Sessions are keyed by their local start timestamp
(`load_activity`'s `session` column, with `session_plays` counting the
session's play rows; the positional `session_id` renumbers on a re-ingest).
The spread draws them in a salted ranking of their own, so a raised cap adds
sessions rather than swapping them; the deep dive's partial day takes them
newest first; and a sitting that runs past midnight is taken whole from the
day it started. `plan_cycle` reports `sessions` — the candidate sessions
whose last unscraped items are in the slice — and the journal's
`slice.queued` carries it. The handoff still clamps annotation to the target,
so a run's very last session can end part-annotated, exactly like its partial
last day; a later target raise completes it first.

**Days per month are derived, not set**
(`collection_enrichment.spread_days_per_month`): the fewest days, the same in
every month still ahead of the spread's cursor, whose capped videos cover the
spread's share of what the target still needs — so the target is the only
quantity knob. The supervisor derives it at the start of a walk and again
when the target, balance, cap or earliest date changes
(`entry["spread_days_per_month"]` + `spread_days_basis`), and `plan_cycle`
derives it for itself when no value is stored. It walks the same salted
ranking the cutter draws from (`stable_rank`), so a higher density is a
superset of a lower one.

**Coverage and the estimate.** With any deep-dive share above zero the plan
can eventually reach everything processable; only at a 100% random daily
sample (or under an `earliest_date` floor) does its per-day cap bound the
final coverage — the panel warns when the chosen target sits above that line.
The modal's estimate mirrors the planner's day pick: `progress()` ships each
day's place in its month's salted draw (`daily.draw`, the same `stable_rank`
`take_a` samples from — a hash order keeps its relative order on any
subset), charges everything already scraped on a day (annotated, awaiting,
failed for good) against the cap as the planner's quota does, and takes the
measured `last_yield` and the burnt-free backlog (`unique_awaiting`) for its
time estimate. `progress()["sessions"]` (`_session_figures`: total /
candidates / ready, plus the incomplete candidates' per-session counts,
newest first) feeds the modal's "analysis-ready sessions" figure and its
estimate line.

**Where it lives and what triggers it.** Plans, cursors and targets live in
`cache/collection_enrichment.json`; they are armed from the Edit Collections
modal, the site-wide switch is the `auto_enrichment_enabled` admin setting
(ships **off**), and the triggers are worker completions, the end of each
consolidation, and an hourly Cloud Scheduler heartbeat on
`/internal/run-task/enrichment_supervisor`. The plan's `in_flight` ledger list
records queued scrapes for stall detection only.

**Deferred refreshes.** The loop's consolidations run with the downstream
refresh **deferred** (`auto_refresh=False`, tagged `plan_deferred`); the
impact accumulates in the ledger's `deferred_impact` entry with `from_plan`
set, and the supervisor's finalize spends it once per cycle when the loop
goes quiet, or after a 24 h backstop. **That is the only deferred debt the
supervisor may spend.** An operator's own consolidate-without-refresh writes
the same entry without `from_plan`, and the finalize leaves it alone — it
waits on the Dataset Assembly page for "Refresh All Affected" (see
[decision 0014](decisions/0014-refresh-runs.md)). A plan deferral landing on
top of an operator's makes the merged entry the loop's (the flag is sticky),
since the plan's cycle needs that refresh and it covers the operator's items
too.

**The loop and the shared queues.** The scrape queues, the annotation queue
and their workers are shared with people working by hand, which gives four
rules (see [decision 0015](decisions/0015-enrichment-loop-and-shared-queues.md)):

1. **The drain step serves the platform queue whoever filled it** — `_drain`
   filters by platform, not by who queued the items, so arming a plan adopts
   anything already queued and runs it first. The Edit Collections panel
   therefore asks (`queue_preview`) before *Arm* / *Resume* / *Run a cycle
   now* when a queue holds videos that are not the collection's own,
   offering to drain them first or empty them.
2. **The stall counter is reset by every productive handoff**: `_plan`
   reloads the plan entry immediately before its read-modify-write, because
   the handoff earlier in the same boundary tick resets `stall_count` and
   prunes `in_flight`, and an earlier snapshot would put the stale values
   back.
3. **A job the loop started owes a consolidation** (`__meta__.settle_owed`,
   set when the loop starts a scraper or annotator, cleared when it
   consolidates): a plan parked or finished while its job still runs leaves
   results that no tick would otherwise fold in, so the no-plans path settles
   that debt before the quiet finalize, and a worker completion still
   dispatches a tick while the loop owes a settle or its own deferred refresh
   (`tasks/runtime.loop_owes_work`), not only while a plan is armed. The
   Dataset Assembly banner reads the same flag and says the loop has the
   consolidation in hand.
4. **A plan with nothing more to scrape stays Running while its own videos
   are still queued for, or inside, an annotation job** (`entry["finishing"]`,
   one `plan.finishing` history line, bounded by `FINISHING_MAX_H`): the owed
   consolidation's completion ticks the loop, the pending count reaches zero,
   and the plan closes (`plan.done`) with the quiet finalize in the same tick.

**The enrichment history.** Every one of these decisions is written to the
enrichment history (`services/enrichment_journal.py`,
`cache/enrichment_journal.json`, a bounded ring): plans armed / paused /
parked, queues built / emptied / drained (with the split between the armed
plans' own slices and everything else), slices, handoffs, and every scraper,
annotator, consolidation and refresh run with its totals — written by the
supervisor, the queue endpoints, `start_process` (hand-started workers) and
the workers' terminal exits. Dataset Assembly shows the whole history; a
collection's Edit Collections panel shows its slice.

## 5. Analysis & studies

Study definitions (`studies.py`) filter the recoded corpus into datasets.
Composed system studies (the participant "Everyone & Me" study) are the
exception: they store no artifacts of their own and are assembled at read
time from the default study plus the user's data, and SYSTEM study
definitions are excluded from all-studies sweeps.
On top of them: PCA + distance metrics (`pca.py`), ANOVA/PERMANOVA
(`stats.py`), timeline metrics (`timeline_analysis.py`), session and
binge-episode segmentation (`session_explorer.py`), sequence windowing
(`sequence_analysis.py`), dense semantic embeddings + niche clustering + 2D
map (`embeddings.py`, `video_map.py`). Research analyses the app does not use
(text-based niche detection, within-session profiling, predictive sequence
modelling) live in `fyp/analysis/experimental/`. The session boundaries themselves are stamped at ingest:
`session_id` is set on every activity row (`fyp/ingest/base.py`
`assign_session_ids`, a `[sessions] session_gap_s` = 900 s gap rule on
`utc_timestamp` alone), which is what lets the enrichment planner sample
whole sessions before anything is scraped (it keys them by local start
timestamp, not the positional id).

Every study refresh also writes a **methods/provenance note**
(`{study}_methods.json` in `cache`, built by
`web_interface/services/methods_note.py`): a plain-language, export-ready
record of the study's filters, sample sizes, the annotation/scrape/activity
contract versions present in the rows, the embedding model behind any niche
columns, and refresh dates. Both study-refresh workers write it on every
refresh — including short-circuited ones, so a newly *preferred* annotation
version reaches the note without a rebuild. Surfaced as the "Methods" panel
on each study's row under My stuff → My Studies
(`GET /api/studies/<study>/methods`); it becomes the bundled README in the
planned per-study export.

### Embedding backends

The semantic-space pipeline is pluggable the same way as annotation:
`fyp/analysis/embedding_backends/` holds an `EmbeddingBackend` ABC
(auto-registry, `get_backend()` / `active_backend_name()`), chosen
**independently** of the annotation backend via the admin setting
`embedding_backend` (Admin → Backends → Embeddings; with both set local,
embeddings and the map are fully cloud-free). Implementations:

- `gemini` — default; API details explicit in `[embedding.gemini]`
  (`model_id` / `dim` / `location` / `task_type`, defaults
  `gemini-embedding-001` @ 1536), so the model is upgradeable by config edit.
- `qwen_api` — hosted Qwen text embeddings via DashScope's OpenAI-compatible
  `/embeddings` endpoint, default `text-embedding-v4` @ 1024, config
  `[embedding.qwen_api]`, key `DASHSCOPE_API_KEY`, 10-inputs-per-request API
  cap, `cloud_run_capable=True`.
- `qwen_local` — `Qwen/Qwen3-Embedding-0.6B` @ 1024 via sentence-transformers
  — MPS/CUDA/CPU, no Apple-Silicon hard gate; config `[embedding.qwen_local]`;
  pyproject extra `local_embeddings`, never in requirements.txt.

Design points:

- Embedding backends deliberately have **no variant system** (unlike
  annotation): the model is a plain config value, and the model-scoped shard
  store already isolates outputs per model.
- The **shard store is model-scoped**: every row of
  `video_embeddings__*.parquet` stamps `model` / `dim`, and
  `embedded_item_ids()` / `load_embeddings()` filter to one model —
  switching backends re-embeds the corpus into new shards (old shards kept;
  switching back is free). Readers dedupe on item id, last occurrence wins,
  and `embeddings_refresh` runs single-flight (see
  [decision 0009](decisions/0009-embeddings-single-flight-lease.md)).
- `build_niche_map` consumes only the active model's vectors, writes
  provenance to `recoded/video_map_meta.json` (`embedding_model` / `dim` /
  `naming_mode` / …), and **niche naming degrades to deterministic
  term-based labels when Gemini is not configured** (the `_ask` seam is the
  hook for a future `local_llm` naming mode).
- Gating mirrors annotation: `process_routes.api_start` refuses
  `embeddings_refresh` when the active backend's `availability()` fails,
  `process_manager.start_process` and the consolidate pipeline skip Cloud Run
  dispatch for a `cloud_run_capable=False` backend,
  `GET /api/manage/embedding/backends` feeds the admin requirements panel,
  and System Health has an `embedding` chip. The Semantic Space status
  endpoint reports `model_mismatch` (map built by a different model than the
  active backend) as staleness.

Enabling the alternatives: [installation.md](installation.md#enabling-local-embeddings).

### The niche map and its per-video measures

`video_map.py` clusters the video embeddings into niches and a 2D semantic
map (with `video_map_meta.json` provenance). It also emits two per-video
**percentiles**, `typicality_pct` and `niche_isolation_pct`, joined into every
study frame by `organize_datasets._join_niche_columns` as numeric measures,
so they reach the Correlations tab as group means per collection-day. They
are percentiles rather than the raw cosine/PCA distances because those scales
drift with every rebuild. Both are NULL for videos not yet in the map, and
the PCA build drops rows with any null feature, so an out-of-date map
silently shrinks the correlations frame for **every** variable (logged as a
warning at merge time). The fix is to refresh embeddings and the video map
BEFORE recoding studies.

The map is warm-started: `video_map_refresh` seeds its k-means from the
previous build's niches — each old niche's members averaged in the current
PCA space, `n_init=1` — so an append refines the old partition instead of
redrawing it and ~150/150 niche names carry over without a Gemini naming
pass. `reset_labels`, a missing previous map, a changed niche count or a
niche with under five surviving members cold-start instead.

The niche map's fingerprint for study-cache freshness is a hash over its
`(item_id, niche)` pairs, not a file stat: `build_niche_map` rewrites the
parquet on every run (fresh 2D coordinates, a new `built_at`), so a stat
would report a change after a rebuild that moved nothing and force every
study to rebuild anyway.

### Sessions refresh

The Sessions tab's artifacts (session index, binge episodes, low-entropy
windows, and a `sessions_plays.parquet` detail fast path) are built by
`fyp/analysis/session_explorer.py` + `entropy_metrics.py` over a dense
random-access embedding sidecar (`fyp/analysis/embedding_store.py`), by the
`sessions_refresh` worker (`web_interface/workers/run_sessions_refresh.py`), a
self-chaining Cloud Task with O(batch) memory. Each link segments a few
collections against the sidecar and writes per-link shards; the final link
folds them into the artifacts. The chain pins one corpus-mean fingerprint at
link 0 and restarts (bounded) if the shard store moves mid-run.

- **Study-window scope.** Only collections in at least one study are built,
  within the padded union of their studies' date windows.
- **Incremental by default.** `stale_only` mode re-segments only the
  collections whose coverage windows or in-window play/annotated counts
  moved, and returns immediately when none did — a second line of defence
  behind the refresh planner's own decision. The merge publish replaces just
  those collections' rows, with per-collection provenance in
  `sessions_meta.json`. A targeted `collections` run also merges; a run with
  no arguments is a forced full rebuild.
- **Scoped enrichment staleness**
  (`session_explorer.enrichment_change_scope`): because the embedding shards
  are append-only, a store whose previously recorded shards are all still
  present byte-identical has only grown; the vectors past the last build's
  count and the annotation rows past its `inference_ts` watermark (an epoch
  in seconds — read from Arrow, not via pandas) name the changed items, and
  only the collections holding them join the refresh as a merge. A rewritten
  or missing shard, a build predating the recorded shard set or watermark, or
  appends beyond `[sessions] rebaseline_fraction` (5 %) of the corpus at the
  last full build fall back to the full rebuild, which resets the baseline
  (see [decision 0013](decisions/0013-scoped-sessions-enrichment-staleness.md)).
- **Parallel segmentation.** Within each link the per-session segmentation —
  pure Python, and nearly all of a rebuild's wall time — runs on a forked
  process pool over (collection, session-chunk) work units
  (`[sessions] workers`, default one per core less one; serial where `fork`
  is unavailable), and every link logs a `[TIMING] sessions_link` line
  splitting its time into load / vectors / segment. The worker count never
  changes the rows.
- **Vector cache.** On a hosted deployment the links cache the dense
  embedding parts whole on the task-runner instance (`[sessions]
  vector_cache`, memory-backed `/tmp`, keyed by store fingerprint) — a
  batch's scattered rows otherwise cost a near-whole read of the store per
  link.
- **Triggers.** A sessions refresh is chained automatically after every
  study save (`pipeline_remaining`, `skip_if_busy` keeps it off the toes of a
  sessions run already in flight); that is a plain chain rather than a
  refresh run. It also runs as a step of a refresh run, and on its own from
  Data Pipeline → Dataset Assembly, where "Force full rebuild" re-segments
  every covered collection.

### Refresh dependencies

Each step is a background job. The graph is declared once, in
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
signal is unknown, and unknown always runs (see
[decision 0014](decisions/0014-refresh-runs.md)). How a run is scoped and
shown in the UI: [web_interface.md](web_interface.md#refresh-runs).

## Adding a platform

A new platform is one ingestion subclass, one scraper subclass and a contract
block; queues, worker processes, UI blocks and media subdirectories derive
automatically. The complete checklist, including the supporting steps that
are easy to miss, is in [extending.md](extending.md#adding-a-new-platform).
