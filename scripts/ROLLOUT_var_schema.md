# Rollout: var_schema refactor (hash narrowing + admin UI)

Four independently rollback-able steps.  Order matters.

## Step 0 — Pre-flight (any time before Step 2)

```bash
# Confirm validator passes against the production CSV (no migration
# changes data; validator should already return zero errors).
python -c "from fyp.recode_variables import validate_var_schema; from fyp.fyp_config import fyp_cf; \
            errs = validate_var_schema(fyp_cf['var_schema']); \
            print(f'{len(errs)} errors'); print(errs[0] if errs else 'clean')"
```

Expected: `0 errors` / `clean`.  If errors are reported, fix the source
CSV before proceeding — the new save path enforces them and will reject
edits otherwise.

## Step 1 — Deploy backend with old hash invalidation intact

Phase-1 code changes (registry, validation, narrowed hash) are
backward-compatible at the *behaviour* level — pipelines keep producing
the same outputs — but the **hash string** changes shape from a bare
64-char hex to `v2:<hex>`.  Every existing study sidecar will mismatch
on the first refresh check and trigger a rebuild *unless* Step 2 runs
first.

Build + deploy:

```bash
gcloud builds submit \
  --tag australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
  --project=<gcp-project> --region=australia-southeast1

# DO NOT deploy fyp-data-hub yet.  Deploy the task-runner only — it is
# the service that writes sidecars.  Web service keeps serving
# pre-migration users until Step 3.
gcloud run deploy fyp-task-runner \
  --image australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
  --region=australia-southeast1 --project=<gcp-project>
```

Actually no — the hash function lives in `fyp/recode_variables.py`,
which both services import.  If they're on different versions, the web
service computes v1, the task-runner computes v2, and the sidecar
check is meaningless during the gap.  **Deploy both services or neither
in this step.**  Recommended: schedule Steps 2 and 3 within minutes of
this one.

## Step 2 — Migrate study sidecars

```bash
# Dry-run first.  Inspect output for `v1_rewritten` count.
python scripts/migrate_var_schema_hash_v2.py --dry-run

# When happy, apply.  Idempotent — safe to re-run.
python scripts/migrate_var_schema_hash_v2.py --apply
```

Run this **after** Step 1 so the script's `compute_var_schema_hash()`
returns a v2 hash to write into the sidecars.  Run it **before** any
study refresh triggers post-deploy or every study will be rebuilt
needlessly.

Cloud Run Jobs are the natural home for this on production:

```bash
gcloud run jobs create migrate-var-schema-hash \
  --image australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
  --region=australia-southeast1 --project=<gcp-project> \
  --command python --args=scripts/migrate_var_schema_hash_v2.py,--apply \
  --set-env-vars FYP_GCS_BUCKET_NAME=fyp_bucket_01,K_SERVICE=migrate

gcloud run jobs execute migrate-var-schema-hash --region=australia-southeast1
```

## Step 3 — Deploy the web UI

```bash
gcloud run deploy fyp-data-hub \
  --image australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-app:latest \
  --region=australia-southeast1 --project=<gcp-project>
```

The new `tab.admin.schema` permission is unassigned by default; admin
users access the UI implicitly.  To grant non-admin researchers access:
Admin → User Roles → tick `Admin — Variable Schema` for the relevant
role.

## Step 4 — Disable in an incident

If the editor causes problems and a redeploy is too slow:

1. Edit `config/config.toml` on the GCS-mounted location, set
   `[features].var_schema_admin = false`.
2. Restart `fyp-data-hub` (no code change needed).

The four endpoints will return 503; the existing
`/api/manage/schema/reload` endpoint (under `tab.admin.general`) still
works for manual CSV edits.

## Rollback

- After Step 1 + 3: re-deploy the previous image of both services.
  Sidecars already migrated to `v2:` will still match the *old* code's
  expectation (v1 prefix-less hex) for exactly zero studies — every
  study refresh will fire once.  That's loud but not destructive; the
  refresh produces correct output.

- The migration script does **not** preserve a copy of the old hash,
  intentionally — keeping both invites confusion.  If a full revert is
  needed, run any single study refresh under the old code and the
  sidecar will be rewritten with the v1 hash.

## Test plan

- Phase 1 unit tests: `python tests/unit/test_var_schema_phase1.py`
  (24 tests; covers hash stability, registry, validation, save/etag,
  freshness, migration).
- Phase 2 API integration tests:
  `python tests/unit/test_var_schema_api.py` (6 tests; covers all four
  endpoints incl. permission, etag, validation, persistence).
- Manual smoke after Step 3: Admin → Variable Schema → edit
  `display_name` of one row → Save.  Confirm: response carries
  `hash_changed: false`; no study refresh banner appears.

## What this rollout does NOT do

- Does not rewrite the CSV format.  All legacy literal forms
  (`{}`, `[]`, `IRRELEVANT_WORDS + [...]`, `[yes, no]`) keep working.
- Does not change recode pipeline outputs.  Phase-1 parsers produce the
  same in-memory values for every existing row.
- Does not split the schema into multiple files.  That was the
  thinking but turned out unnecessary once the hash was narrowed —
  one file, one editor, two trust levels.
