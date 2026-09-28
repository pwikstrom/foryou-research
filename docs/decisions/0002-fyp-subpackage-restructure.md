# 0002. Restructure the flat `fyp/` package into five subpackages

Date: 2026-07-12

## Context

`fyp/` was one flat package of modules. It was reorganized into domain
subpackages — `core/`, `ingest/`, `scrape/`, `annotation/`, `analysis/` —
from an import-dependency analysis of the flat package (a generated
`fyp → fyp` adjacency matrix plus the external importers of every module).
The first sketch assigned modules by domain alone; the import analysis
showed that several of those placements would change behaviour:

- `import fyp.ingest` boots the configuration by design (collection classes
  register their upload locations at class definition), and importing any
  submodule of a package runs the package `__init__` first. A module placed
  in `fyp/ingest/` therefore boots config on import. `organize_datasets` and
  `donations` went to `analysis/`, and `structure_sentinel`,
  `activity_contract` and `activity_versioning` to `core/`, rather than into
  `ingest/`.
- The contract and versioning modules are imported *during* the config boot,
  so none of them may sit behind an eagerly-initializing package `__init__`.
- A partially-initialized old-path shim poisons any module that re-imports
  the same old path mid-cascade (the shim-poisoning rule), which forced
  relative sibling imports inside `fyp/scrape/` and `fyp/ingest/`.
- Importing the `*_dl` scraper modules from `fyp/scrape/__init__` would load
  `yt_dlp` inside every config boot and inside `import fyp.pca`.

These became the placement rules in
[fyp-import-graph.md](../fyp-import-graph.md).

Two consolidations were considered and not done in the move:

- **Splitting `machine_annotation.py`.** The module (one of the largest) has
  a clean internal DAG — orchestration (`annotate_from_video_id_list`,
  `queue_annotation_loop`) → {calls (`initialize_machine`, `call_machine*`,
  `_generate_with_retry`), parse (`flatten_*`,
  `fuzzy_load_of_json_from_string`,
  `consolidate_rare_columns_from_gemini_output`), refine
  (`refine_one_raw_annotation_batch`, `clean_up_machine_annotations`,
  `remove_repetitions_from_transcripts`)}; refine → parse; no mutable module
  state (the Gemini client lives in the config dict). A four-way split is
  structurally feasible, but ~18 test files plus the golden harness reach
  and patch `ma._*` private names on the module object, and a facade split
  would silently break those patch targets (functions in submodules resolve
  their own globals, not the facade's). Zero behaviour gain, real regression
  risk.
- **Deduplicating the scraper helpers.** `_empty_fail` is byte-identical in
  all three `*_dl.py` modules and `_cleanup_temp_files` differs only in a
  parameter name (`video_id` vs `item_id`); both are hoistable, but no
  production or test code imports either, so the hoist is cosmetic.
  `_info_to_row` is genuinely platform-specific (TikTok builds the full
  `_DEFAULTS` schema with dtype casts; Instagram and YouTube emit small raw
  frames with different signatures, later renamed by `map_to_canonical`).

## Decision

- Assign each module by the placement rules, not by domain alone. The
  original assignment was: `core/` — `paths` (new), `fyp_config`,
  `data_io`, `types`, `utils`, `logging_setup`, `polars_ops`, `media_paths`,
  `registry_metadata`, `activity_contract`, `activity_versioning`,
  `derived_contract`, `structure_sentinel`; `ingest/` — the split of
  `ingest.py` into `base`, `tiktok`, `instagram`, `youtube`; `scrape/` —
  `scrape`, `platform_scraper`, `tiktok_dl`, `instagram_dl`, `youtube_dl`,
  `scraper_cookies`, `scrape_queues`, `scrape_contract`,
  `scrape_versioning`; `annotation/` — `machine_annotation`,
  `machine_annotation_batch`, `annotation_contract`, `annotation_schema`,
  `annotation_versioning`, `ab_eval`, `human_eval`, `recode_variables`,
  `var_presentation`, `irrelevant_words`; `analysis/` — `pca`, `stats`,
  `embeddings`, `video_map`, `niche_detection`, `session_profile`,
  `sequence_analysis`, `sequence_model`, `timeline_analysis`,
  `activity_analysis`, `calc_collection_stats`, `studies`,
  `organize_datasets`, `donations`.
- Keep every old flat path (`fyp/<module>.py`) as a back-compat shim for
  code outside the repository.
- Move `machine_annotation.py` whole. A future split must relocate the test
  patch targets in the same change.
- Leave `_empty_fail` and `_cleanup_temp_files` duplicated (the hoist was
  deferred out of the mechanical move commits); never force-share
  `_info_to_row`.

## Consequences

- Modules added after the restructure were born inside a subpackage and have
  no flat shim: `core/memory`, `core/runtime`, `core/gemini_client`,
  `ingest/raw_names`, `ingest/migrations/`, `scrape/scraper_alerts`,
  `scrape/connectivity`, `annotation/backends/`, `analysis/embedding_store`,
  `analysis/entropy_metrics`, `analysis/session_explorer`,
  `analysis/embedding_backends/`.
- `tests/unit/test_subpackage_shims.py` keeps the shims working and probes the
  shim-poisoning failure mode in fresh interpreters;
  `tests/unit/test_lazy_config_boot.py` pins the boot rule.
- The generated import matrix was first embedded in the layout document. It
  was a point-in-time snapshot and drifted (the post-restructure modules were
  never part of it), so it was removed; `python scripts/gen_import_graph.py`
  generates a current one on demand.
- First-party code later stopped using the flat shims altogether
  ([decision 0010](0010-canonical-imports-only.md)).
