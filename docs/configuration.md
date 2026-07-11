# Configuration

FYP is configured by `config/config.toml` (committed), four declarative TOML
contracts alongside it, and a small set of environment variables. The loader
is `fyp/fyp_config.py`: it walks up the directory tree looking for the empty
sentinel file `__proj__.py` to find the project root, so imports work from
any working directory. **Note:** config loads at `fyp.fyp_config` import
time (it also connects to GCS and synthesizes the variable schema) — see the
import-cycle rule in `CONTRIBUTING.md`.

## config/config.toml sections

| Section | Purpose | Keys you'll actually touch |
|---|---|---|
| `[machine]` | Gemini annotation | model name, prompt file, temperature, Vertex AI project |
| `[paths]` | local storage roots | `local_data` — **set this to a writable directory on your machine**; everything (cache, recoded, users, media) lives under it locally |
| `[misc]` | runtime behavior | timezone, `local_mode`, media duration caps (`max_duration_for_download[_<platform>]`, `max_duration_for_annotation`) |
| `[features]` | feature toggles | rarely changed |
| `[data_io]` | storage backend | GCS bucket name, `use_gcs_*` per-location toggles |
| `[viz]` | dashboard visuals | palette etc. |
| `[labels]` | content categories | category lists, generic mapper, `IRRELEVANT_WORDS` (seed for the admin-editable hashtag stoplist) |

For a new collaborator the only required edit is `[paths] local_data` (and
possibly `[machine]` if you have your own Gemini/Vertex project).

## The four contracts

`annotation_contract.toml`, `scrape_contract.toml`, `activity_contract.toml`,
and `derived_contract.toml` own the variable schemas (field names, dtypes,
display metadata, prompt/response schema for Gemini). They are loaded and
validated by their same-named `fyp/*_contract.py` modules and overlaid onto
the synthesized `var_schema` at config load. The annotation contract can also
be replaced at runtime via the admin UI (stored in `users/`); setting
`FYP_BAKED_CONTRACTS_ONLY=1` forces the committed ("baked") contract —
tests and the golden safety net use this.

## Environment variables

| Variable | Effect |
|---|---|
| `GEMINI_API_KEY` | Gemini API access for annotation/embeddings |
| `FLASK_SECRET_KEY` | Flask session secret (falls back to a dev key locally) |
| `FYP_GCS_BUCKET_NAME` | GCS bucket (production) |
| `K_SERVICE` | Set automatically by Cloud Run — switches storage to GCS and job dispatch to Cloud Tasks |
| `FYP_FORCE_GCS` | Force ALL storage to the prod GCS bucket from a local process (e.g. the local scrape-queue drain runbook in `AGENT.md`); refuses to fall back to local storage if the GCS connection fails |
| `FYP_BAKED_CONTRACTS_ONLY` | Ignore any runtime-uploaded annotation contract; use the committed one |
| `FLASK_DEBUG` | Optional Flask debug toggle |
| `CLOUD_RUN_SERVICE_URL`, `GCP_PROJECT_ID`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SA_EMAIL` | Cloud Tasks dispatch configuration (production) |
| `BGUTIL_POT_SERVER_HOME` | Path to the bgutil PO-token provider script (YouTube media downloads from datacenter IPs) |

## Storage locations

`fyp/data_io.py` maps named locations to directories under `local_data`
locally, or to GCS prefixes in cloud mode (`use_gcs_*` toggles / `K_SERVICE`).
Code must always use location names — `load_parquet("recoded", ...)` — never
absolute paths. Locations can also be registered at runtime
(`data_io.register_location`), which is how platform ingestion classes
self-register their raw-upload directories.

## Admin-editable stores (runtime state, in the `users` location)

- `var_presentation.json` — which variables appear on which UI surface
  (never affects the schema hash)
- `irrelevant_words.json` — hashtag stoplist (seeded from `[labels]`)
- `annotation_contract.toml` — runtime-uploaded annotation contract, if any
- user accounts and per-user settings (JSON files)
