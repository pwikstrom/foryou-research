# Post-holiday follow-ups

Personal scratchpad — things to pick back up when you're back. Add your own items before you leave; check this file when you log on again.

---

## Things to deploy

- [x] ~~Deploy the consolidate+refresh pipeline work to Cloud Run.~~
  Done — rode along with the `9194674` collection-upload fix deploy on 2026-04-21. Both services (`fyp-data-hub` rev `00108-nwn`, `fyp-task-runner` rev `00061-592`) are now running the latest `main`. No base-image rebuild was needed.
- [x] ~~Move heavy ingestion routes off the data-hub (commit `15bfe70`, deployed 2026-04-21).~~
  `ingest_refresh`, `aio_fetch`, `collection_metadata_refresh` and `collection_delete` are now Cloud Tasks running on `fyp-task-runner`. Final revisions: `fyp-data-hub-00121-ctv`, `fyp-task-runner-00067-nnb`. Base image was rebuilt for boto3.

---

## Things to verify end-to-end after deploy

The full cross-task pipeline (`consolidate → recode → meta → pca → timelines`) was verified locally via the subprocess orchestrator. The Cloud Tasks path (`_run_task_with_stats` with `next_task` + `pipeline_remaining`) is code-complete but hasn't yet been exercised on real Cloud Run infrastructure with real data.

- [ ] **Click Consolidate & Refresh on Cloud Run with actual new scraped/annotated data.** Watch:
  - Stage text advances from 1/N through to N/N across tasks.
  - `pipeline_in_flight` bridges the dispatch gap cleanly.
  - `last_pipeline_summary` ends up written to `process_stats["consolidate_enrichment"]` (look at the GCS JSON directly if the UI is ambiguous).
  - Cloud Tasks console shows the chain: one consolidate task, then one recode task, then meta, pca, timelines (timelines may self-chain across many collections).
- [ ] **Arm-and-wait flow on Cloud Run.** Start scraper on a non-empty queue, click Consolidate & Refresh while it's running (should arm instead of fire; button goes pulsing blue), wait for scraper to finish, confirm the pipeline auto-fires.
- [ ] **Arm-prompt modal on queue start.** Click Start Scraper with a non-empty queue while consolidate is idle/un-armed — modal should appear; click Yes and confirm the button arms after the queue begins.
- [ ] **Pipeline abort path.** If one of the downstream steps fails (e.g. recode), verify `pipeline_in_flight` clears, `last_pipeline_summary` reads "Pipeline aborted at '<step>'", and the remaining steps don't fire.
- [ ] **Collection upload modal on Cloud Run.** Commits `9194674` + `15bfe70` together fixed two bugs (chosen collection ID being ignored; uploads writing to ephemeral disk instead of GCS). Verify all three modal modes work end-to-end against the production bucket:
  - "Use filename as collection ID" → file ingests under the filename stem.
  - "Join existing collection" → dropdown loads in <1s with display IDs like `AIO-00001 (uuid)`, and uploaded file ends up with the selected raw `collection_id`.
  - "Create new collection ID" → file ingests under the typed ID, not the filename.
  Then run Process New Collections and confirm the uploaded rows appear under the right `collection_id` in the processed data.

### New since 2026-04-21 — heavy routes moved to Cloud Tasks

- [ ] **Click "Process New Collections" on production.** Until this session it was running synchronously on the data-hub and (originally) crashing the worker. Now dispatches the `ingest_refresh` Cloud Task. Verify: the global running-tasks badge in the header appears, the button text shows live stage progress (`Processing... 75% — Adding local time features...`), and the pending count drops to 0 on completion.
- [ ] **Click "Fetch from AWS" on production with a window that contains real new donations.** During the deploy day we only verified with `0 donations_found` (legitimate, but not exercising the S3 download path). Set days-back to something that includes recent activity, click, watch the toast at the end. Should say `AWS fetch: N donation(s) uploaded (M found).` If it errors, look in `fyp-task-runner` logs for boto3 stack traces — most likely cause would be AWS keys having expired/rotated (then re-add a version with `gcloud secrets versions add AWS_ACCESS_KEY_ID --data-file=-`).
- [ ] **Delete a small test collection on production.** The route now dispatches `collection_delete` to the task-runner instead of running inline. Verify: alert at the end shows `Deleted "X". Dropped N row(s), archived M raw file(s). Refreshing K study/studies in the background.`, the header badge shows `study_refresh__<study>` running afterwards for each affected study, and the collection disappears from the list.
- [ ] **Click "Refresh Collection Metadata" (admin tab).** Promoted to `collection_metadata_refresh` Cloud Task. Verify it completes without OOMing the data-hub (the 1+ GB recoded parquet is now loaded on the task-runner).
- [ ] **Pending uploads UX.** Upload a couple of small test files; confirm the pending-uploads panel under the source cards lists each filename with its collection_id and tags inline. Click "Cancel All Pending Uploads" — toast says `Cancelled N pending upload(s).`, files are gone from GCS, panel shows "No new collections to process.".
- [ ] **Toast doesn't fire 5–10 times** after AWS fetch (was a poller race; should now fire exactly once per completion).

---

## Known rough edges worth revisiting

- **DNFs are treated as permanent annotation failures.** Worker-timeouts get `annotated_fail=True` after refinement, which propagates through consolidation into `enrichment_status.parquet`, and `calculate_to_annotate` then filters them out forever. This is pre-existing behaviour, now documented in memory. A proper fix would distinguish DNFs at the raw results layer (before refinement) and keep them retriable. Not urgent — low rate in practice.
- **`meta_refresh_groups` processes ALL studies, ignoring the affected-studies list from the pipeline.** The pipeline dispatches it for every run that has `affected_study_names`, so it's wasteful. Adding `--studies` filter support to `run_meta_refresh_groups.py`'s `__main__` + `run_meta_refresh_groups()` function would mirror the pattern already in `run_recode_refresh_studies.py` and `run_pca_refresh.py`.
- **`_write_pipeline_summary_cloud` heuristic.** It detects which steps "ran this pipeline" by comparing each downstream step's `last_run_end_time` against the consolidate's `last_run_end_time`. If a user manually kicked off one of those refreshes between consolidate's start and end, it'd be wrongly counted. Edge case but worth knowing.
- **Arm intent doesn't survive tab close.** `window._pendingArmAfterStart` is in memory only. If you click "Yes, arm it" in the modal and close the tab before the queue POST completes, arming never happens. Could be hardened by POSTing to an "arm intent" endpoint before the queue start rather than after.
- **Stale `pipeline_in_flight` cleanup is time-based (60s).** If a server restart happens mid-pipeline, the UI shows "in flight" for up to 60s before the stats endpoint auto-clears it. A startup hook could clear it immediately on boot. Only affects local dev; Cloud Run restarts are rare.
- **Cloud Tasks 503 mystery — root cause never identified.** When `ingest_refresh` was first introduced in Apr 2026, `client.create_task()` returned `503 Service Unavailable` repeatedly from the data-hub Python client even though `gcloud tasks create-http-task` from a laptop worked. After ~1 hour of debugging it self-resolved and dispatch has worked since. Mitigated with 3-attempt retry + diagnostic dump in `_dispatch_cloud_task`. If it recurs: the next failure log will dump `e.details()` + `trailing_metadata` + traceback so you can see what the API actually said. Keep the retry; don't silently delete it.
- **`collection_delete` uses a single status key.** Two simultaneous deletes serialise behind the task-runner's `concurrency=1` (which is fine, even an improvement over the old behaviour that loaded 1 GB twice on the data-hub). If we ever want truly parallel deletes, switch to a per-collection key like `study_refresh__<study>` does.
- **`/api/manage/ingestion/clear_pending` runs inline on the data-hub.** It's lightweight (only deletes raw files + resets manifests; no parquet I/O), but if someone has thousands of pending uploads it'd block a request thread. Promote to a Cloud Task if it ever becomes slow in practice.
- **Timeline refresh `_repair_stringified_multiindex` intermittently lands on a flat Index instead of MultiIndex.** One-off hit in prod 2026-04-21T03:39:51 on `fyp-task-runner-00073-vjh`: `[timelines_refresh] Error loading metadata: "None of [Index(['personas', 'active_days'], dtype='object')] are in the [columns]"`. Five subsequent runs on the same revision / same `collections_metadata.parquet` succeeded, and timelines are currently generating fine — this is latent brittleness, not an outage. Root cause: `fyp/data_io.py::load_parquet_selective` strips parquet `pandas_metadata` (needed to avoid unrelated ArrowDtype failures), then `_repair_stringified_multiindex` builds `pd.Index(mixed_scalar_and_tuple_list)`, which in pandas 2.2.x sometimes stays flat and sometimes auto-promotes to MultiIndex. When it lands flat, `df.loc[did, ('personas', 'active_days')]` in `web_interface/run_timelines_refresh.py:148` treats the tuple as a list-of-labels and raises. Diagnostic proof: the failing load reported `bytes=9187`, every successful load reports `bytes=9170` — different in-memory shape from identical GCS bytes.
  Fix when revisiting: (1) in `_repair_stringified_multiindex`, when **every** repaired column is a tuple, build `pd.MultiIndex.from_tuples` explicitly; (2) in `load_parquet_selective`, do `set_index('collection_id')` **before** the repair so the scalar is out of the way by the time repair sees it. Add a regression test that `df.loc[row, tuple_col]` works after repair regardless of pandas' flat-vs-MultiIndex auto-promotion behaviour. Small, self-contained change — maybe 30 min including the test.
- **Task-runner memory peaks at ~3× the final dataframe size on the shebang merge.** For `everything_2` (2026-04-21, commit `62ba8b2` memory instrumentation): `rss_start=724MB → rss_after_load=4028MB → rss_after_merge=20123MB → peak_during=22291MB`, while the final merged dataframe was only `df_size=7531MB`. The 12 GB gap between `rss_after_merge` and `df_size` is the source frames (`activity_data`, `enriched_data`) still referenced by `new_merge`'s caller while the merged result is being built. Against a 32 GB container that leaves ~10 GB headroom — fine for `everything_2`, tight for anything 2× bigger. Three options when you want to scale further, in ascending cost/payoff:
  1. **Drop source frames inside `new_merge`.** After the `fast_join` line in `fyp/organize_datasets.py:1205`, `del activity_data` and `del enriched_data` (or zero out the corresponding entries in `all_datasets`) so Python can free them before the calculated-column work runs. Est. ~3–4 GB peak reduction, 30 min of work, safe.
  2. **Bump `fyp-task-runner` memory to 64 GB.** `gcloud run services update fyp-task-runner --region=australia-southeast1 --memory=64Gi --project=<gcp-project>`. Cloud Run bills memory per request-second, so idle cost is unchanged and heavy merges just get more room. Gives ~40 GB of working room for merges. Cheapest way to handle studies 2–3× bigger than `everything_2`.
  3. **Streaming polars on the shebang merge.** Rewrite `new_merge` to use `pl.scan_parquet(activity) → join(pl.scan_parquet(enriched)) → sink_parquet(result)` and read the parquet back for the calculated-column work (or switch those to lazy too). Peak memory becomes "size of one chunk × thread count" regardless of input size — probably 2–4 GB. Medium-sized surgery; do this only if you cross the 15–20 GB final-DF threshold. Recommended order when you come back: do (1)+(2) first, revisit (3) only if a real study pushes past the 64 GB ceiling.
- **`UserManager` still bulk-loads every user JSON on `fyp-data-hub` cold start.** Task-runner is already lazy (commit `ce9f416`, 2026-04-21 — `K_SERVICE == "fyp-task-runner"` → `bulk_load=False` in `web_interface/security.py`), but the web service still fans out 32 parallel GCS reads for every user at boot. At 63 users it takes ~0.5s and is fine. Revisit when any of these become true:
  - Cold start on `fyp-data-hub` exceeds ~2s from the user-load step (grep `[AUTH] Loaded`).
  - Startup probe timeouts start appearing in Cloud Run logs.
  - Heavy annotators push individual user JSONs past ~100 KB (check `gsutil du -s gs://fyp_bucket_01/data/users/`).
  - User count approaches ~500.
  Recommended fix: lazy per-user load with an LRU cache in `UserManager.get_user()` — same code path that task-runner already uses, just apply to web too. The "iterate all users" call sites (`auth.py:91`, `344`, `371` for admin/role checks, `data_service.py:1678` for the Kappa report) need a separate cheap path: maintain a tiny `_users_index.json` with `{username: {role, approved, last_login}}` and rewrite the iterators against that, or keep a `list_users_lite()` that scans the directory without loading bodies. Login (`verify_user`) only touches one file per attempt so it stays fast.

---

## Things to scan quickly on return

- [ ] Any alerts / error emails from Cloud Run while you were away.
- [ ] `gcloud logging read` for unusual volume of failed Cloud Tasks in the `fyp-background-tasks` queue.
- [ ] Scraper/annotator worker drift — verify queue counts on the UI look sane (now that both prune, any stuck counts are a bug).
- [ ] **`gcloud logging read ... textPayload:"CloudTasks"`** — scan for any 503/dispatch failures across all Cloud Tasks (the mystery 503 from Apr 2026 may have recurred while you were away). If yes, the new diagnostic logs will have full tracebacks.
- [ ] **AIO storage on the RA's behalf.** Your RA was supposed to click "Fetch from AWS" while you were away. Check `gs://fyp_bucket_01/data/activity_data/aio/aio_participants/` and `…/aio_raw/` for new files dated during the holiday — if there are none, the RA either didn't click or it's silently failing. Look at `process_stats.json["aio_fetch"]["last_run_outcome"]` and the recent `task_status/aio_fetch.json` for the actual story.

---

## Reminder of what the "Data Management" tab looks like now

Behavior you may not remember after a few weeks away:

- **Consolidate & Refresh** button has three states:
  - Idle — fires immediately.
  - Workers running, not armed — clicking arms (doesn't fire).
  - Armed — pulsing blue, click to cancel. Auto-fires when workers go idle.
- **Force Reconsolidate** is disabled whenever *any* worker (scraper, annotator, consolidate, or downstream refresh) is running.
- After a successful run, a green "✓ {outcome}" line appears below "Last consolidation …". It persists across reloads. When there's nothing to refresh, it says so explicitly so you know everything is in order.
- Starting Scraper / Annotator on a non-empty queue prompts "Auto-run Consolidate & Refresh when this finishes?" — Yes arms the button.

### Ingestion sub-page — new since 2026-04-21

- Below the source cards there's now a **pending-uploads panel** that lists every staged file (per source) with its `collection_id` and tags. When nothing is pending: "No new collections to process.".
- Next to **Process New Collections** there's a **Cancel All Pending Uploads** button that appears whenever the pending count is > 0. It deletes the staged files from GCS and resets every manifest. Confirms first; not undoable.
- **Toast notifications** (bottom-right, fade in/out, ~5s) fire after upload, AWS fetch, and cancel. Errors are 7s and red-bordered.
- The upload modal now opens **instantly** (used to take ~5s waiting for collection-IDs to load); the existing-collection dropdown shows "Loading collections..." for a moment then populates.
- The **header badge** (top of every page) shows running tasks. Now also surfaces `ingest_refresh`, `aio_fetch`, `collection_delete`, `collection_metadata_refresh` while they execute.

---

## Your own additions

_Add below before you leave_:

- 
- 
- 
