# Contracts

The end-to-end guide to the contract system — the four declarative TOML files
in `config/` that own the entire variable schema, and everything built on top
of them: validation, runtime editing, versioning, and the schema hash that
keys the study caches. For where contracts sit in the overall system, see
[architecture.md](architecture.md) §"The contract system"; for how they relate
to `config.toml` and the environment, see [configuration.md](configuration.md).
Adding a field or a platform in practice is walked through in
[extending.md](extending.md).

## What the contracts are

A *contract* is a TOML file declaring a set of fields — names, dtypes, and
per-field analysis/display metadata — that a pipeline stage emits. The code
reads the contract instead of hardcoding the schema, so a field change is a
TOML edit, and every stored row can be stamped with the contract version that
produced it.

| Contract | Loader | Owns |
|---|---|---|
| `config/annotation_contract.toml` | `fyp/annotation/annotation_contract.py` | The LLM annotation surface: the generated prompt (header + one bullet per field + footer), the structured-output `response_schema`, the flattener that turns responses into columns, and the recode normalization (enum vocabularies, `[recode.drop]` stop words) |
| `config/scrape_contract.toml` | `fyp/scrape/scrape_contract.py` | The canonical cross-platform scrape schema: base + per-platform field sets with PyArrow dtypes, the `[perk]` per-K engagement map (`count / play_count * 1000`), and the registered platform list (`[meta].platforms`) |
| `config/activity_contract.toml` | `fyp/core/activity_contract.py` | The platform-agnostic activity schema ingestion emits: the required-column set (the `REQUIRED_COLUMNS` analogue), dtypes, and the `required = true` core fields that drive the per-row hard-drop integrity gate |
| `config/derived_contract.toml` | `fyp/core/derived_contract.py` | **Metadata only** — rows for columns no single stage emits: merge-time enrichment (`days_since_created`, `completion_rate`, `engaged`, ...), the niche columns, the `desc` recode fan-out, and the per-item status flags. The maths stays in `fyp/analysis/organize_datasets.py:new_merge()`; the contract just owns the columns' role/scale/display metadata |

The annotation contract has a companion, `config/annotation_contract_help.toml`
— the UI-servable transcription of the baked file's header comments, shown as
help texts in the admin form editor (`contract_help()`).

Two contract-adjacent runtime stores complete the picture. Both live in the
`users` storage location, are admin-editable, and are deliberately **outside**
the schema hash so editing them never invalidates study caches:

- **Presentation store** (`fyp/annotation/var_presentation.py`) — the four
  `web_*_prio` membership flags (which variables appear on the filter /
  timeline / viz / display surfaces), stored as `users/var_presentation.json`
  and seeded once from `config/var_presentation_defaults.json`. Edited via
  Admin → Variable Visibility.
- **Hashtag stoplist** (`fyp/annotation/irrelevant_words.py`) — junk tokens
  dropped during hashtag extraction, stored as `users/irrelevant_words.json`
  and seeded from `[labels] IRRELEVANT_WORDS` in `config.toml`. Supports
  character-squeeze matching (`fyp` catches `fyyyyp`) and `*` prefix
  wildcards. Edited via Admin → Hashtag Stoplist.

## Authoring a contract: the field keys

The **canonical key reference is the header comment block of each TOML file
itself** — `config/annotation_contract.toml` and `config/scrape_contract.toml`
open with a full description of every key (the activity contract's header
additionally documents the role-admission test the other contracts refer to).
This section is a summary, not a replacement.

### Annotation fields

Each `[[fields]]` entry describes one LLM output field. Everything except
`name` is optional:

| Key | Meaning |
|---|---|
| `name` | Output key (and column prefix for object fields) |
| `desc` | One-line instruction — feeds both the prompt bullet and the JSON-schema node description |
| `enum` | Name of an entry in `[enums]` — a closed value set, auto-rendered into the prompt |
| `array` | `true` (uncapped) or an integer N (`maxItems = N`) — the field is a list |
| `type` | `"string"` (default), `"int"`, or `"object"` |
| `min` / `max` | Integer bounds for `type = "int"` |
| `keys` | `[fields.keys]` sub-table for `type = "object"` — each sub-key is a compact spec string (`"enum:gender"`, `"list: ..."`, `"int(0,100): ..."`, free text) or an inline table carrying its own metadata |
| `scale` | Recode kind. **Inferred from the shape** (array → `list`, int → `numeric`, enum → `categorical`); required only for a free-text string, where `categorical` (short labels) vs `text` (long prose) picks the recode function. Metadata-only |
| `role` | var_schema analysis role — `measure` (a Correlations variable) or `skip` (dropped from recoded output). Metadata-only |
| `display_name` | Web-UI label for the recoded column. Metadata-only |

Everything else is inferred and never written: the flatten rule (`scalar` /
`list_join` / `object_unpack`), the full JSON-schema node, enum rendering, and
the `required` set (every field). A minimal field:

```toml
[[fields]]
name = "multilingual"
role = "measure"
display_name = "Multilingual"
enum = "yes_no"
desc = "Are multiple languages spoken or sung in the video? ..."
```

Everything a field declares feeds the annotation-version hash **except** the
metadata-only keys (`scale`, `role`, `display_name`): changing `desc`, an
enum, or the shape mints a new version; relabeling a column does not.

### Scrape / activity / derived fields

The data contracts share a flatter surface — no prompt, no inference; each
field is a stored column:

| Key | Meaning |
|---|---|
| `name` | Canonical stored column name |
| `scope` | `"base"` (every platform emits it) or `"platform"` |
| `platform` | For `scope = "platform"`: the owning platform (`"tiktok"`, ...) |
| `dtype` | PyArrow dtype string, e.g. `"double[pyarrow]"` |
| `section` | var_schema UI section (Activity / Item metadata / Popularity / backstage / ...) — cosmetic, excluded from the schema hash |
| `role` / `scale` | Analysis role and scale (the full role vocabulary + admission test: `activity_contract.toml` header) |
| `display_name` / `description` | var_schema display metadata |
| `required` | Activity only: a null value hard-drops the row at ingest |
| `derived` | Computed by the stage itself (not raw-mapped from the platform payload / raw export) — e.g. `scrape_status`, the per-K rates, `session_id` |
| `per_k_of` | Scrape only: marks an engagement-rate field's absolute-count denominator (paired with the `[perk]` table) |

The derived contract uses the same keys minus `scope`/`platform`/`required`
(all its fields are derived by definition), plus `skip_recode` and the
`transform = "log1p"` pre-aggregation hint.

## Validation

Every loader exposes `validate_contract(contract) -> list[str]` and its
`load_contract(path)` raises `ValueError` listing every error when the list is
non-empty — the failure is a readable bullet list, one line per problem:

```
ValueError: Invalid scrape contract (config/scrape_contract.toml):
  - field 'saves_per_K_play': per_k_of 'play_countt' is not a base field
  - [meta].default_platform 'tikok' is not in [meta].platforms
```

What the validators enforce, per contract:

- **All four**: non-empty `[[fields]]`, no missing or duplicate `name`s, and
  `role` / `scale` values drawn from the vocabularies in
  `fyp/annotation/recode_variables.py` (`VAR_SCHEMA_ROLES` /
  `VAR_SCHEMA_SCALES`; legacy role aliases stay accepted).
- **Annotation**: `[prompt].header` present; enums non-empty and every
  `enum` / `enum:` reference resolvable; `type` in `{string, int, object}`;
  object fields have a non-empty `[fields.keys]`; and the one inference gap is
  closed — a free-text string field **must** declare `scale` explicitly.
- **Scrape**: `[meta].default_platform` set and registered; every field has a
  `dtype` and `section`; platform-scoped fields name a registered platform;
  every `per_k_of` and `[perk]` entry resolves to real contract fields.
- **Activity / derived**: dtypes/sections present, plus the shared checks.

How a bad contract actually surfaces depends on the consumer. Explicit loads
(tests, tools, the scraper constructing itself, the versioning modules) raise
immediately. The var_schema overlays in `fyp/core/fyp_config.py` and the
ingest module's import-time load deliberately **degrade instead of crashing
boot** — the overlay becomes a no-op (with a warning), so the app comes up but
the contract's columns are missing or stale and the first scrape/annotation
fails loudly. Either way the practical check is the same: boot the app locally
(config loads and synthesizes the schema at import) or run
`scripts/verify.sh`, whose unit tests load and validate every baked contract.

`FYP_BAKED_CONTRACTS_ONLY=1` makes the annotation loader ignore any
runtime-uploaded contract and use the committed ("baked") file. It exists for
two reasons: the golden regression suite sets it so a dev machine's local
runtime storage can never contaminate results, and it is an emergency ops
lever to neutralize a bad runtime contract without touching storage.

## The runtime annotation contract

The annotation contract is the **only contract editable at runtime**. An
admin-uploaded copy lives at `users/annotation_contract.toml` (with an audit
sidecar `annotation_contract_meta.json`); when present and valid it supersedes
the baked file with no redeploy. Absent, unreadable, or invalid, the loader
degrades to the baked file with a loud warning and the reason surfaced on the
admin status card — a broken upload can never take annotation down.

**Refresh is deliberate, not per-call.** The process-local snapshot is
refreshed only at explicit points via `refresh_runtime_contract()`:

- process boot (first lazy access),
- every `load_var_schema()` (so any schema rebuild sees the current file),
- Cloud Task entry, via `reload_var_schema_if_changed()` (so long-lived
  task-runner containers pick up an admin edit made on the web service),
- the upload / revert endpoints themselves.

The point of *not* polling per call: a whole annotation batch is pinned to one
contract — the prompt, schema, and version stamp cannot shift mid-batch.

**The admin flow** lives on Admin → Contracts
(`web_interface/routes/management/contracts.py`, template
`tabs/admin/annotation_contract_editor.html`). It offers a raw-TOML editor and
a form editor (which serializes back through tomlkit against the current text,
so comments on untouched keys survive), plus preview/parsed/rendered views,
download, and revert-to-baked. Upload is two-step: a dry run validates the
candidate and returns a **version-impact report** built from
`candidate_version_descriptor()` — the exact `av_` id the candidate would
produce on the target backend, and what changes — and only an explicit confirm
(etag-guarded against concurrent edits, with the previous runtime contract
backed up) persists it, refreshes the snapshot, and rebuilds the in-memory
schema.

**Active vs preferred.** Annotation versioning
(`fyp/annotation/annotation_versioning.py`) keeps two distinct pointers, and
the vocabulary is used verbatim in the UI:

- **active** — the version the *next* annotation will be stamped with. Never
  stored: derived live from the effective contract, the selected backend, and
  the generation parameters. Uploading a contract changes the active version
  immediately.
- **preferred** — the version studies *read* when an item was annotated under
  several. Stored in the registry and changed only by an explicit promote
  (Admin → Versions, `promote_version()`), so in-flight analyses never shift
  underfoot.

The intended lifecycle is therefore: upload contract → new annotations accrue
under the new `av_` version → inspect/compare → promote → analysis follows.

## The read-only contracts and the Data Contracts page

Admin → Data Contracts (`web_interface/routes/management/data_contracts.py`,
template `tabs/admin/data_contracts.html`) serves the scrape, activity, and
derived contracts for inspection: parsed field tables, the raw TOML, download,
and — for scrape and activity — the version history behind every stored row.

It is deliberately **read-only**. These three contracts only meaningfully
change together with the code that emits their fields (a new scrape column
needs a `_RAW_TO_CANONICAL` mapping; a new activity column needs the ingester
to produce it), so they are edited in the repository, reviewed, and deployed —
never uploaded at runtime. A deployed change forks a new version
automatically; nothing needs registering by hand.

## Versioning and synthesis

**Version registries.** Three registries stamp per-row provenance with
deterministic hashes:

| Prefix | Module | Registry file | Identity hashed over |
|---|---|---|---|
| `av_` | `fyp/annotation/annotation_versioning.py` | `annotation_versions.json` | model id, exact prompt text, response-schema shape, key generation parameters |
| `sv_` | `fyp/scrape/scrape_versioning.py` | `scrape_versions.json` | scrape field digest (dtypes, scopes, per-K map), platforms, default platform |
| `acv_` | `fyp/core/activity_versioning.py` | `activity_versions.json` | activity field digest |

Each registry snapshots the full inputs behind every version it has seen, so
the precise prompt/schema/field set that produced any stored row is preserved
even after the contract is edited. New versions are recorded automatically but
never auto-promoted.

**Legacy-field preservation.** `fyp/core/registry_metadata.py` provides
`snapshot_field_metadata()` / `union_field_metadata()`: at registration each
version snapshots its contract's column metadata, and the schema synthesis
merges those snapshots (newest wins) minus whatever the current contract still
owns. A field a later contract *drops* therefore stays contract-owned and
read-only — badged "legacy" in the admin UI — instead of degrading into an
editable orphan. This is the general mechanism for "contracts change over
time but data spans versions".

**var_schema synthesis.** `fyp/core/fyp_config.py:load_var_schema()` builds
the in-memory variable schema from scratch on every (re)load — the old CSV
var_schema is retired. The order is fixed:

1. refresh the runtime annotation-contract snapshot;
2. start from an empty typed skeleton;
3. overlay the contracts in order — annotation (current + registry legacy
   fields) → scrape → activity → derived — then rebuild `accepted_labels`
   from the annotation contract's enums;
4. fill the four `web_*_prio` columns from the presentation store.

A source fingerprint (contract files + presentation store + registries) is
stored alongside; `reload_var_schema_if_changed()` compares it at every Cloud
Task entry and re-synthesizes only when a source actually moved — cheap enough
to run on every task.

**The schema hash.** `fyp/annotation/recode_variables.py:
compute_var_schema_hash()` is what study parquet caches key on. It hashes the
recode-driving columns of the synthesized schema and then **folds in a digest
of every contract's field set** — the annotation contract's normalization
(enums, drop words, recode ops) and the scrape, activity, and derived field
digests. Any structural contract edit therefore invalidates cached study
parquets. Cosmetic columns (`display_name`, `description`, `section`, the
`web_*` flags) are excluded by design, so relabeling, re-sectioning, and
presentation edits are hash-neutral.

## Operational cost of a contract change

For operators with existing data, honestly, per kind of change:

- **Any structural edit** (field added/removed, dtype, enum values, scope,
  per-K map, prompt text) bumps the schema hash → every study's cached
  parquet rebuilds on its next dataset assembly. That is a recode pass, not a
  re-scrape or re-annotation — existing raw data is untouched.
- **An annotation-contract change** mints a new `av_` version that applies to
  *future* annotations only. Existing annotations stay stored under the
  version that produced them — nothing is re-annotated automatically, and
  analysis keeps reading the **preferred** version until you promote. Expect a
  transition window where the corpus spans two versions; the Versions page
  shows per-version counts.
- **A scrape/activity-contract field change** takes effect for future
  scrapes/ingests under a new `sv_` / `acv_` stamp; already-stored rows keep
  their columns and version. A brand-new column is simply null for historical
  rows until they are re-scraped.
- **Renames and removals**: prefer letting the registry's legacy-metadata
  union keep the old column contract-owned (it happens automatically once the
  old version is registered). Never repurpose a name — the scrape loader's
  `LEGACY_COLUMN_ALIASES` / `RETIRED_TO_GENERIC` maps show what a managed
  rename involves, including on-disk self-healing at consolidation.
- **Metadata-only edits** (`display_name`, `description`, `section`, `role`,
  `scale` on the annotation side, presentation/stoplist edits) are cheap:
  no new version, no cache invalidation — except `scale`/`role` changes that
  alter recoding behavior, which the hash deliberately catches.

**Order of operations for rolling out a change:**

1. Edit the contract (repo file; or the Contracts page for annotation).
2. Validate locally — boot the app or run `scripts/verify.sh`; for annotation
   uploads the dry-run impact report is the validation step.
3. Deploy (scrape/activity/derived) or confirm the upload (annotation).
4. Run annotation (or scraping/ingest) so data accrues under the new version.
5. Promote the new version on Admin → Versions once you trust it.
6. Reassemble datasets (Data Pipeline → Dataset Assembly) — the bumped schema
   hash rebuilds the study parquets.

See also [DEVELOPING.md](../DEVELOPING.md) for the module-by-module reference,
[pipeline.md](pipeline.md) for where each stage runs, and
[extending.md](extending.md) for worked examples of adding a field and a
platform.
