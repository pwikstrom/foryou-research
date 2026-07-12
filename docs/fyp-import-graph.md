# fyp/ subpackage restructure — import graph & module assignment (Phase 8)

This document records the import-dependency analysis behind the Phase 8
restructure of the flat `fyp/` package into domain subpackages, and the
reasoning for each module's assignment. The raw matrix at the bottom is
regenerated with:

```bash
python scripts/gen_import_graph.py > /tmp/matrix.md   # then paste below the marker
```

## Final module → subpackage assignment

| Subpackage | `__init__` behavior | Modules |
|---|---|---|
| `fyp/core/` | inert (docstring only) | `paths` (new), `fyp_config`, `data_io`, `types`, `utils`, `logging_setup`, `polars_ops`, `media_paths`, `registry_metadata`, `activity_contract`, `activity_versioning`, `derived_contract`, `structure_sentinel` |
| `fyp/ingest/` | **eager** — imports `base`, then `tiktok`, `instagram`, `youtube` (registration order pinned) | split of `ingest.py` only: `base`, `tiktok`, `instagram`, `youtube` |
| `fyp/scrape/` | eager `from .scrape import …` re-exports + forwarding `__getattr__`; **must not boot config** | `scrape`, `platform_scraper`, `tiktok_dl`, `instagram_dl`, `youtube_dl`, `scraper_cookies`, `scrape_queues`, `scrape_contract`, `scrape_versioning` |
| `fyp/annotation/` | inert | `machine_annotation`, `machine_annotation_batch`, `annotation_contract`, `annotation_schema`, `annotation_versioning`, `ab_eval`, `human_eval`, `recode_variables`, `var_presentation`, `irrelevant_words` |
| `fyp/analysis/` | inert | `pca`, `stats`, `embeddings`, `video_map`, `niche_detection`, `session_profile`, `sequence_analysis`, `sequence_model`, `timeline_analysis`, `activity_analysis`, `calc_collection_stats`, `studies`, `organize_datasets`, `donations` |

Every old path (`fyp/<module>.py`) remains forever as a back-compat shim.

## Back-compat mechanism

**Renamed modules** use a *sys.modules alias shim*:

```python
import sys
from fyp.core import data_io as _real
sys.modules[__name__] = _real
```

Old and new paths resolve to the **same module object**, so all of the
following keep working unchanged: `import fyp.data_io as data_io`,
`from fyp import data_io`, `from fyp.data_io import _private_name`,
`from fyp.recode_variables import *`, PEP 562 module `__getattr__`
constants, and — critically — the attribute-assignment patching used across
the test suite (`data_io.load_json = fake`). CPython ≥ 3.7 honors the
sys.modules swap for every import form, including parent-package attribute
binding.

**Name-collision packages** (`fyp/ingest.py` → `fyp/ingest/`,
`fyp/scrape.py` → `fyp/scrape/`) cannot alias (a package needs its
`__path__` for submodule imports), so their `__init__.py` genuinely
re-exports the old module surface and forwards stragglers via a module
`__getattr__`.

## Placement rules (why the layout deviates from the first sketch)

1. **Boot rule.** `import fyp.ingest` triggers the config boot *by design*
   (collection `__init_subclass__` → `data_io.register_location()`); every
   other module must stay lazy (Phase 4, pinned by
   `tests/unit/test_lazy_config_boot.py`). Importing any submodule of a
   package executes the package `__init__` first — so a module placed inside
   `fyp/ingest/` would boot config on import. Therefore `fyp/ingest/`
   contains **only** the split of `ingest.py` itself, and
   `organize_datasets`, `donations` (→ `analysis/`), `structure_sentinel`,
   `activity_contract`, `activity_versioning` (→ `core/`) live elsewhere.
2. **Mid-boot rule.** `fyp_config.load_var_schema` imports the contract and
   versioning modules *during* the boot it may itself be running inside
   (`data_io`, `var_presentation`, `annotation_contract`,
   `annotation_versioning`, `scrape_contract`, `scrape_versioning`,
   `activity_contract`, `activity_versioning`, `derived_contract`). These
   must remain import-inert and keep their function-level `_cf()` /
   `_data_io()` accessors — none may gain an eagerly-initializing package
   `__init__` in its import chain (beyond `fyp/scrape/`'s non-booting one).
3. **Shim-poisoning rule (the subtle one).** When an old-path shim is
   *partially initialized* (it sits empty in `sys.modules` while its body
   imports the new location), any module in that import cascade that
   re-imports the same old path receives the **empty shim object** via
   CPython's circular-import fallback — permanently. Concretely:
   `from fyp.scrape_contract import …` (e.g. from `var_presentation` during
   boot) starts the `fyp/scrape_contract.py` shim → triggers
   `fyp/scrape/__init__` → `scrape.py` / `platform_scraper.py`, which used
   to do `from fyp import scrape_contract as sc` → `sc` would be bound to
   the empty shim; every later `sc.load_contract()` dies at runtime while
   import, boot, and the hash tripwire all pass. **Therefore all
   same-package sibling imports inside `fyp/scrape/*` (and `fyp/ingest/*`)
   are relative** (`from . import scrape_contract as sc`), which routes the
   cascade through the package directly and never back through a mid-flight
   shim. Cross-package old-path imports are safe: the underlying module
   graph is acyclic at module level (see matrix), so no shim can be
   re-entered while partial. `tests/unit/test_subpackage_shims.py` probes
   exactly this failure mode in fresh interpreters.
4. **Scrape `__init__` eagerness is minimal.** It imports only `.scrape`
   (needed to re-export the old `fyp.scrape` API). It must *not* import the
   `*_dl` modules: they load `yt_dlp`, which would newly run inside every
   config boot and inside `import fyp.pca` — a real behavior change.
   Scrapers keep loading lazily via
   `platform_scraper._ensure_scrapers_imported()`.

## machine_annotation: moved whole (seam analysis for a future split)

`machine_annotation.py` (~2.2k lines) has a clean internal DAG —
orchestration (`annotate_from_video_id_list`, `queue_annotation_loop`) →
{calls (`initialize_machine`, `call_machine*`, `_generate_with_retry`),
parse (`flatten_*`, `fuzzy_load_of_json_from_string`,
`consolidate_rare_columns_from_gemini_output`), refine
(`refine_one_raw_annotation_batch`, `clean_up_machine_annotations`,
`remove_repetitions_from_transcripts`)}; refine → parse; no mutable module
state (the Gemini client lives in the config dict). A 4-way split is
structurally feasible, **but** ~18 test files plus the golden harness reach
and patch `ma._*` private names on the module object; a facade split would
silently break those patch targets (functions in submodules resolve their
own globals, not the facade's). Zero behavior gain, real regression risk —
so Phase 8 moves the module whole. A future split should relocate the test
patch targets in the same change.

## Scraper helpers: what was (not) deduplicated

- `_empty_fail` — byte-identical in all three `*_dl.py`; hoistable.
- `_cleanup_temp_files` — identical except the parameter name
  (`video_id` vs `item_id`); hoistable.
- `_info_to_row` — **genuinely platform-specific** (TikTok builds the full
  `_DEFAULTS` schema with dtype casts; Instagram/YouTube emit small raw
  frames with different signatures, later renamed by `map_to_canonical`).
  Not hoisted; do not force-share it.

No production or test code imports the first two, so the hoist is cosmetic
and deferred out of the mechanical move commits.

<!-- GENERATED MATRIX BELOW — regenerate with scripts/gen_import_graph.py -->
# fyp import-dependency matrix

Generated by `python scripts/gen_import_graph.py > docs/fyp-import-graph.md`.

## Internal adjacency (fyp -> fyp)

`(L)` marks a lazy (function-scoped) import.

- **ab_eval** -> annotation
- **activity_analysis** -> analysis
- **activity_contract** -> core
- **activity_versioning** -> core
- **analysis.activity_analysis** -> logging_setup
- **analysis.calc_collection_stats** -> activity_analysis, logging_setup
- **analysis.donations** -> calc_collection_stats, data_io, fyp_config (L), logging_setup, organize_datasets (L), recode_variables
- **analysis.embeddings** -> data_io, fyp_config (L), logging_setup
- **analysis.organize_datasets** -> annotation_versioning, data_io, fyp_config (L), logging_setup, machine_annotation, polars_ops, recode_variables, scrape, scrape_contract, studies
- **analysis.pca** -> data_io, fyp_config (L), logging_setup, organize_datasets, recode_variables, types
- **analysis.stats** -> logging_setup
- **analysis.studies** -> data_io, fyp_config (L), logging_setup
- **analysis.timeline_analysis** -> logging_setup
- **analysis.video_map** -> data_io, embeddings, fyp_config (L), logging_setup
- **annotation.ab_eval** -> annotation_contract, annotation_schema, data_io, fyp_config (L), machine_annotation (L), media_paths (L), recode_variables (L), types
- **annotation.annotation_contract** -> data_io (L), logging_setup, recode_variables (L)
- **annotation.annotation_schema** -> annotation_contract
- **annotation.annotation_versioning** -> annotation_contract (L), annotation_schema (L), data_io (L), fyp_config (L), logging_setup, registry_metadata (L)
- **annotation.human_eval** -> ab_eval, annotation_contract, data_io, fyp_config (L), logging_setup
- **annotation.irrelevant_words** -> data_io (L), fyp_config (L), logging_setup
- **annotation.machine_annotation** -> annotation_schema, annotation_versioning, data_io, fyp_config (L), logging_setup, media_paths, recode_variables, scrape_queues, types, utils
- **annotation.machine_annotation_batch** -> annotation_schema, annotation_versioning, data_io, fyp_config (L), machine_annotation, media_paths
- **annotation.recode_variables** -> activity_contract (L), activity_versioning (L), annotation_contract (L), annotation_versioning (L), derived_contract (L), fyp_config (L), irrelevant_words, logging_setup, scrape_contract (L), scrape_versioning (L), types, utils
- **annotation.var_presentation** -> data_io (L), logging_setup, scrape_contract
- **annotation_contract** -> annotation
- **annotation_schema** -> annotation
- **annotation_versioning** -> annotation
- **calc_collection_stats** -> analysis
- **core.activity_contract** -> recode_variables (L)
- **core.activity_versioning** -> activity_contract, data_io (L), logging_setup, registry_metadata (L)
- **core.data_io** -> fyp_config (L), logging_setup, types
- **core.derived_contract** -> recode_variables (L)
- **core.fyp_config** -> activity_contract (L), activity_versioning (L), annotation_contract (L), annotation_versioning (L), core.paths, data_io (L), derived_contract (L), scrape_contract (L), scrape_versioning (L), var_presentation (L)
- **core.media_paths** -> fyp_config (L), scrape_queues (L)
- **core.polars_ops** -> logging_setup, types
- **core.structure_sentinel** -> data_io, logging_setup, utils
- **core.types** -> logging_setup
- **core.utils** -> logging_setup
- **data_io** -> core
- **derived_contract** -> core
- **donations** -> analysis
- **embeddings** -> analysis
- **fyp_config** -> core
- **human_eval** -> annotation
- **ingest.__init__** -> ingest, ingest.base, ingest.instagram, ingest.tiktok, ingest.youtube
- **ingest.base** -> activity_contract, activity_versioning, data_io, donations, fyp_config (L), logging_setup, organize_datasets (L), polars_ops, recode_variables, scrape_contract, scrape_versioning, structure_sentinel, types, utils
- **ingest.instagram** -> data_io, ingest.base, logging_setup, utils
- **ingest.tiktok** -> data_io, donations (L), ingest.base, logging_setup, recode_variables, utils
- **ingest.youtube** -> data_io, ingest.base, logging_setup, utils
- **instagram_dl** -> scrape
- **irrelevant_words** -> annotation
- **logging_setup** -> core
- **machine_annotation** -> annotation
- **machine_annotation_batch** -> annotation
- **media_paths** -> core
- **niche_detection** -> analysis
- **organize_datasets** -> analysis
- **pca** -> analysis
- **platform_scraper** -> scrape
- **polars_ops** -> core
- **recode_variables** -> annotation
- **registry_metadata** -> core
- **scrape.__init__** -> scrape, scrape.scrape
- **scrape.instagram_dl** -> fyp_config (L), scrape, scrape.platform_scraper
- **scrape.platform_scraper** -> fyp_config (L), scrape
- **scrape.scrape** -> data_io, fyp_config (L), logging_setup, media_paths, recode_variables, scrape, scrape.platform_scraper, utils
- **scrape.scrape_contract** -> recode_variables (L)
- **scrape.scrape_queues** -> data_io (L), logging_setup, scrape (L)
- **scrape.scrape_versioning** -> data_io (L), logging_setup, registry_metadata (L), scrape
- **scrape.scraper_cookies** -> fyp_config (L)
- **scrape.tiktok_dl** -> fyp_config (L), logging_setup, scrape, scrape.platform_scraper
- **scrape.youtube_dl** -> fyp_config (L), scrape, scrape.platform_scraper
- **scrape_contract** -> scrape
- **scrape_queues** -> scrape
- **scrape_versioning** -> scrape
- **scraper_cookies** -> scrape
- **sequence_analysis** -> analysis
- **sequence_model** -> analysis
- **session_profile** -> analysis
- **stats** -> analysis
- **structure_sentinel** -> core
- **studies** -> analysis
- **tiktok_dl** -> scrape
- **timeline_analysis** -> analysis
- **types** -> core
- **utils** -> core
- **var_presentation** -> annotation
- **video_map** -> analysis
- **youtube_dl** -> scrape

## External importers (web_interface / tests / scripts / experiments)

- **fyp.ab_eval**: 5 file(s)
  - tests/unit/test_ab_eval.py
  - tests/unit/test_human_eval.py
  - web_interface/routes/human_eval_routes.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/run_ab_eval.py
- **fyp.activity_contract**: 2 file(s)
  - tests/test_activity_contract.py
  - web_interface/routes/management/schema.py
- **fyp.activity_versioning**: 2 file(s)
  - web_interface/routes/management/schema.py
  - web_interface/run_ingest_refresh.py
- **fyp.annotation_contract**: 12 file(s)
  - tests/golden/test_generated_contract_equivalence.py
  - tests/golden/test_schema_pipeline_consistency.py
  - tests/unit/test_ab_eval.py
  - tests/unit/test_annotation_contract_api.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_contract_accepted_labels.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_runtime_annotation_contract.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/schema.py
  - web_interface/run_ab_eval.py
- **fyp.annotation_schema**: 13 file(s)
  - tests/ab_eval/generated_prompt_ab.py
  - tests/ab_eval/media_resolution_ab.py
  - tests/ab_eval/run_ab_eval.py
  - tests/ab_eval/smoke_structured.py
  - tests/ab_eval/structured_annotator.py
  - tests/ab_eval/temperature_ab.py
  - tests/golden/test_generated_contract_equivalence.py
  - tests/golden/test_structured_flatten_equivalence.py
  - tests/unit/test_ab_eval.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_runtime_annotation_contract.py
  - web_interface/routes/management/contracts.py
- **fyp.annotation_versioning**: 11 file(s)
  - tests/ab_eval/batch_spike.py
  - tests/ab_eval/structured_annotator.py
  - tests/golden/test_schema_pipeline_consistency.py
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/test_annotation_versioning.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_multiplatform_annotation.py
  - tests/unit/test_runtime_annotation_contract.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/schema.py
  - web_interface/routes/management/studies.py
- **fyp.data_io**: 91 file(s)
  - scripts/adhoc/build_global_niche_map.py
  - scripts/adhoc/migrate_clear_discarded.py
  - scripts/adhoc/migrate_share_annotations_optin.py
  - scripts/adhoc/render_full_map.py
  - scripts/adhoc/repair_overflowed_playcounts.py
  - scripts/adhoc/repro_assemble_cache.py
  - scripts/adhoc/repro_diag.py
  - scripts/adhoc/repro_diff.py
  - scripts/adhoc/repro_diff2.py
  - scripts/adhoc/repro_explore_endpoints.py
  - scripts/adhoc/repro_explore_fields.py
  - scripts/adhoc/repro_explore_schema.py
  - scripts/adhoc/repro_hashtag.py
  - scripts/adhoc/repro_playcount.py
  - scripts/adhoc/repro_resumable_refine.py
  - scripts/adhoc/repro_richfields.py
  - scripts/adhoc/repro_seedsweep.py
  - scripts/adhoc/repro_session_profile.py
  - scripts/adhoc/requeue_instagram_viewcounts.py
  - scripts/backfill_display_usernames.py
  - scripts/bootstrap_structure_baselines.py
  - scripts/migrate_var_schema_hash_v2.py
  - tests/ab_eval/local_worker_test.py
  - tests/bench/bench_parquet_loads.py
  - tests/bench/bench_post_load_overhead.py
  - tests/debug/list_columns.py
  - tests/debug/sanity_check_pca.py
  - tests/debug/smoke_enrichment_patch.py
  - tests/golden/test_batch_worker.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_versioning_consolidation.py
  - tests/test_identify_similar_file_content.py
  - tests/test_instagram_youtube_ingest.py
  - tests/test_sequence_analysis.py
  - tests/unit/test_ab_eval.py
  - tests/unit/test_annotate_calc.py
  - tests/unit/test_annotation_contract_api.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_bbc_jacqui_stats.py
  - tests/unit/test_calc.py
  - tests/unit/test_empty_filter.py
  - tests/unit/test_empty_write.py
  - tests/unit/test_fillna.py
  - tests/unit/test_human_eval.py
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_load_parquet_selective.py
  - tests/unit/test_metadata_selective_loads.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_retokenise_hashtags.py
  - tests/unit/test_runtime_annotation_contract.py
  - tests/unit/test_timeline_analysis.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_zee_generic_fix.py
  - tests/unit/test_zee_generic_step.py
  - web_interface/activity_log.py
  - web_interface/admin_settings.py
  - web_interface/auth.py
  - web_interface/explorer_backend.py
  - web_interface/process_manager.py
  - web_interface/routes/api_correlations_routes.py
  - web_interface/routes/api_explorer_routes.py
  - web_interface/routes/api_semantic_space_routes.py
  - web_interface/routes/api_timelines_routes.py
  - web_interface/routes/api_viewer_routes.py
  - web_interface/routes/auth_routes.py
  - web_interface/routes/management/collections.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/enrichment.py
  - web_interface/routes/management/ingestion.py
  - web_interface/routes/management/studies.py
  - web_interface/routes/process_routes.py
  - web_interface/run_benchmark_parquet_read.py
  - web_interface/run_collection_delete.py
  - web_interface/run_collection_metadata_refresh.py
  - web_interface/run_consolidate_enrichment.py
  - web_interface/run_meta_refresh_groups.py
  - web_interface/run_queue_annotator.py
  - web_interface/run_queue_annotator_batch.py
  - web_interface/run_recode_refresh_studies.py
  - web_interface/run_retokenise_hashtags.py
  - web_interface/run_sequence_refresh.py
  - web_interface/run_study_refresh.py
  - web_interface/run_timelines_refresh.py
  - web_interface/semantic_trajectory.py
  - web_interface/services/analysis_data.py
  - web_interface/services/preview_cache.py
  - web_interface/services/stats_service.py
  - web_interface/services/study_data.py
  - web_interface/services/timeline_service.py
  - web_interface/services/user_variables.py
  - web_interface/task_status.py
- **fyp.derived_contract**: 2 file(s)
  - tests/test_derived_contract.py
  - web_interface/routes/management/schema.py
- **fyp.donations**: 2 file(s)
  - web_interface/run_aio_fetch.py
  - web_interface/run_collection_metadata_refresh.py
- **fyp.embeddings**: 3 file(s)
  - web_interface/routes/api_semantic_space_routes.py
  - web_interface/run_embeddings_refresh.py
  - web_interface/semantic_trajectory.py
- **fyp.fyp_config**: 103 file(s)
  - scripts/adhoc/audit_audio_only_media.py
  - scripts/adhoc/build_global_niche_map.py
  - scripts/adhoc/migrate_clear_discarded.py
  - scripts/adhoc/migrate_share_annotations_optin.py
  - scripts/adhoc/repair_overflowed_playcounts.py
  - scripts/adhoc/repro_assemble_cache.py
  - scripts/adhoc/repro_diag.py
  - scripts/adhoc/repro_diff.py
  - scripts/adhoc/repro_diff2.py
  - scripts/adhoc/repro_explore_endpoints.py
  - scripts/adhoc/repro_explore_fields.py
  - scripts/adhoc/repro_explore_new_prompt.py
  - scripts/adhoc/repro_explore_schema.py
  - scripts/adhoc/repro_hashtag.py
  - scripts/adhoc/repro_niche_naming.py
  - scripts/adhoc/repro_playcount.py
  - scripts/adhoc/repro_readpath_smoke.py
  - scripts/adhoc/repro_registry_field_sets.py
  - scripts/adhoc/repro_resumable_refine.py
  - scripts/adhoc/repro_richfields.py
  - scripts/adhoc/repro_seedsweep.py
  - scripts/adhoc/repro_session_profile.py
  - scripts/adhoc/requeue_instagram_viewcounts.py
  - scripts/adhoc/spike_ig_counts.py
  - tests/ab_eval/_ab_common.py
  - tests/ab_eval/batch_spike.py
  - tests/ab_eval/generated_prompt_ab.py
  - tests/ab_eval/local_worker_test.py
  - tests/ab_eval/media_resolution_ab.py
  - tests/ab_eval/run_ab_eval.py
  - tests/ab_eval/smoke_production_structured.py
  - tests/ab_eval/smoke_structured.py
  - tests/ab_eval/structured_annotator.py
  - tests/ab_eval/temperature_ab.py
  - tests/bench/bench_parquet_loads.py
  - tests/bench/bench_post_load_overhead.py
  - tests/debug/list_columns.py
  - tests/debug/probe_parquet_schemas.py
  - tests/debug/smoke_enrichment_patch.py
  - tests/golden/_harness.py
  - tests/golden/test_batch_worker.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_versioning_consolidation.py
  - tests/test_identify_similar_file_content.py
  - tests/test_instagram_youtube_ingest.py
  - tests/test_pipeline_indicator_sim.py
  - tests/test_sequence_analysis.py
  - tests/test_structure_sentinel.py
  - tests/unit/conftest.py
  - tests/unit/test_annotate_calc.py
  - tests/unit/test_annotation_contract_api.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_bbc_jacqui_stats.py
  - tests/unit/test_calc.py
  - tests/unit/test_call_machine_retry.py
  - tests/unit/test_check_existing_media.py
  - tests/unit/test_consolidate_progress.py
  - tests/unit/test_content_category_recoding.py
  - tests/unit/test_contract_accepted_labels.py
  - tests/unit/test_contract_source_platform.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_fillna.py
  - tests/unit/test_import_cycle_hash.py
  - tests/unit/test_instagram_scraper.py
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_load_parquet_selective.py
  - tests/unit/test_media_path_resolution.py
  - tests/unit/test_media_resolution.py
  - tests/unit/test_metadata_selective_loads.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_recode_series_branches.py
  - tests/unit/test_registry_metadata.py
  - tests/unit/test_runtime_annotation_contract.py
  - tests/unit/test_scraper_cookies.py
  - tests/unit/test_timeline_analysis.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_var_schema_phase1.py
  - tests/unit/test_zee_generic_fix.py
  - tests/unit/test_zee_generic_step.py
  - web_interface/activity_log.py
  - web_interface/explorer_backend.py
  - web_interface/process_manager.py
  - web_interface/routes/api_correlations_routes.py
  - web_interface/routes/api_explorer_routes.py
  - web_interface/routes/api_viewer_routes.py
  - web_interface/routes/auth_routes.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/routes/management/collections.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/enrichment.py
  - web_interface/routes/management/ingestion.py
  - web_interface/routes/management/schema.py
  - web_interface/routes/management/studies.py
  - web_interface/routes/process_routes.py
  - web_interface/run_collection_delete.py
  - web_interface/run_meta_refresh_groups.py
  - web_interface/run_pca_refresh.py
  - web_interface/run_recode_refresh_studies.py
  - web_interface/run_sequence_refresh.py
  - web_interface/run_study_refresh.py
  - web_interface/services/study_data.py
  - web_interface/services/timeline_service.py
  - web_interface/services/user_variables.py
- **fyp.human_eval**: 3 file(s)
  - tests/unit/test_human_eval.py
  - web_interface/routes/human_eval_routes.py
  - web_interface/routes/management/ab_eval.py
- **fyp.ingest**: 11 file(s)
  - scripts/adhoc/migrate_clear_discarded.py
  - scripts/bootstrap_structure_baselines.py
  - tests/test_identify_similar_file_content.py
  - tests/test_instagram_youtube_ingest.py
  - tests/test_structure_sentinel.py
  - tests/unit/test_derive_play_duration.py
  - tests/unit/test_platform_backfills.py
  - web_interface/routes/api_viewer_routes.py
  - web_interface/routes/management/collections.py
  - web_interface/routes/management/ingestion.py
  - web_interface/run_ingest_refresh.py
- **fyp.instagram_dl**: 2 file(s)
  - scripts/adhoc/spike_ig_counts.py
  - tests/unit/test_instagram_scraper.py
- **fyp.irrelevant_words**: 4 file(s)
  - tests/unit/test_irrelevant_words.py
  - tests/unit/test_irrelevant_words_api.py
  - tests/unit/test_retokenise_hashtags.py
  - web_interface/routes/auth_routes.py
- **fyp.machine_annotation**: 25 file(s)
  - scripts/adhoc/repro_resumable_refine.py
  - tests/ab_eval/_ab_common.py
  - tests/ab_eval/batch_spike.py
  - tests/ab_eval/generated_prompt_ab.py
  - tests/ab_eval/local_worker_test.py
  - tests/ab_eval/media_resolution_ab.py
  - tests/ab_eval/run_ab_eval.py
  - tests/ab_eval/smoke_production_structured.py
  - tests/ab_eval/structured_annotator.py
  - tests/ab_eval/temperature_ab.py
  - tests/golden/_harness.py
  - tests/golden/test_batch_annotation.py
  - tests/golden/test_batch_worker.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_structured_flatten_equivalence.py
  - tests/golden/test_structured_refinement_path.py
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/test_annotation_repair.py
  - tests/unit/test_call_machine_retry.py
  - tests/unit/test_consolidate_rare_columns.py
  - tests/unit/test_content_category_recoding.py
  - tests/unit/test_media_resolution.py
  - web_interface/routes/management/contracts.py
  - web_interface/run_queue_annotator.py
  - web_interface/run_queue_annotator_batch.py
- **fyp.machine_annotation_batch**: 4 file(s)
  - tests/ab_eval/batch_spike.py
  - tests/golden/test_batch_annotation.py
  - tests/unit/test_multiplatform_annotation.py
  - web_interface/run_queue_annotator_batch.py
- **fyp.media_paths**: 2 file(s)
  - tests/unit/test_media_path_resolution.py
  - web_interface/routes/api_viewer_routes.py
- **fyp.niche_detection**: 8 file(s)
  - scripts/adhoc/build_global_niche_map.py
  - scripts/adhoc/repro_diag.py
  - scripts/adhoc/repro_diff.py
  - scripts/adhoc/repro_diff2.py
  - scripts/adhoc/repro_hashtag.py
  - scripts/adhoc/repro_richfields.py
  - scripts/adhoc/repro_seedsweep.py
  - scripts/adhoc/repro_session_profile.py
- **fyp.organize_datasets**: 28 file(s)
  - tests/debug/smoke_enrichment_patch.py
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/test_annotate_calc.py
  - tests/unit/test_calc.py
  - tests/unit/test_consolidate_progress.py
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_load_parquet_selective.py
  - tests/unit/test_metadata_selective_loads.py
  - tests/unit/test_platform_backfills.py
  - web_interface/explorer_backend.py
  - web_interface/routes/api_timelines_routes.py
  - web_interface/routes/management/collections.py
  - web_interface/routes/management/enrichment.py
  - web_interface/routes/management/ingestion.py
  - web_interface/routes/management/studies.py
  - web_interface/run_benchmark_parquet_read.py
  - web_interface/run_collection_delete.py
  - web_interface/run_collection_metadata_refresh.py
  - web_interface/run_consolidate_enrichment.py
  - web_interface/run_recode_refresh_studies.py
  - web_interface/run_retokenise_hashtags.py
  - web_interface/run_timelines_refresh.py
  - web_interface/semantic_trajectory.py
  - web_interface/services/preview_cache.py
  - web_interface/services/stats_service.py
  - web_interface/services/study_data.py
  - web_interface/services/timeline_service.py
  - web_interface/services/user_variables.py
- **fyp.pca**: 8 file(s)
  - tests/debug/sanity_check_pca.py
  - tests/test_pca_explode_dtype_guard.py
  - tests/unit/test_pca_crosstab_regression.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_single_column_pca.py
  - web_interface/run_pca_refresh.py
  - web_interface/run_study_refresh.py
  - web_interface/services/analysis_data.py
- **fyp.platform_scraper**: 12 file(s)
  - scripts/adhoc/repair_overflowed_playcounts.py
  - scripts/adhoc/repro_instagram_scrape.py
  - scripts/adhoc/repro_youtube_scrape.py
  - tests/test_engagement_per_play.py
  - tests/test_scrape_contract.py
  - tests/test_youtube_media_retry.py
  - tests/unit/test_platform_stamping.py
  - tests/unit/test_scraper_slideshow_hooks.py
  - tests/unit/test_youtube_scraper.py
  - web_interface/routes/management/enrichment.py
  - web_interface/run_queue_scraper.py
  - web_interface/services/worker_status.py
- **fyp.polars_ops**: 2 file(s)
  - scripts/adhoc/migrate_clear_discarded.py
  - tests/unit/test_polars_ops_migration.py
- **fyp.recode_variables**: 19 file(s)
  - scripts/migrate_var_schema_hash_v2.py
  - tests/ab_eval/_ab_common.py
  - tests/golden/build_golden.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_schema_pipeline_consistency.py
  - tests/unit/test_australian_relevance_derivation.py
  - tests/unit/test_contract_accepted_labels.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_irrelevant_words.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_recode_series_branches.py
  - tests/unit/test_retokenise_hashtags.py
  - tests/unit/test_schema_cell_parsers.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_var_schema_phase1.py
  - web_interface/routes/api_correlations_routes.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/schema.py
  - web_interface/run_retokenise_hashtags.py
- **fyp.registry_metadata**: 1 file(s)
  - tests/unit/test_contract_source_platform.py
- **fyp.scrape**: 7 file(s)
  - tests/test_youtube_media_retry.py
  - tests/unit/test_changed_scrape_ids.py
  - tests/unit/test_check_existing_media.py
  - tests/unit/test_retired_column_migration.py
  - tests/unit/test_seed_merge.py
  - tests/unit/test_slideshow_audio.py
  - web_interface/run_queue_scraper.py
- **fyp.scrape_contract**: 7 file(s)
  - tests/test_engagement_per_play.py
  - tests/test_scrape_contract.py
  - tests/unit/test_contract_source_platform.py
  - tests/unit/test_platform_stamping.py
  - tests/unit/test_retired_column_migration.py
  - tests/unit/test_scrape_contract_platforms.py
  - web_interface/routes/management/schema.py
- **fyp.scrape_queues**: 7 file(s)
  - scripts/adhoc/requeue_instagram_viewcounts.py
  - scripts/adhoc/spike_ig_counts.py
  - tests/unit/test_scrape_queue_migration.py
  - web_interface/fyp_data_hub.py
  - web_interface/process_manager.py
  - web_interface/routes/management/enrichment.py
  - web_interface/run_queue_scraper.py
- **fyp.scrape_versioning**: 1 file(s)
  - web_interface/routes/management/schema.py
- **fyp.scraper_cookies**: 2 file(s)
  - tests/test_cookie_race.py
  - tests/unit/test_scraper_cookies.py
- **fyp.sequence_analysis**: 2 file(s)
  - tests/test_sequence_analysis.py
  - web_interface/run_sequence_refresh.py
- **fyp.session_profile**: 7 file(s)
  - scripts/adhoc/repro_diag.py
  - scripts/adhoc/repro_diff.py
  - scripts/adhoc/repro_diff2.py
  - scripts/adhoc/repro_hashtag.py
  - scripts/adhoc/repro_richfields.py
  - scripts/adhoc/repro_seedsweep.py
  - scripts/adhoc/repro_session_profile.py
- **fyp.structure_sentinel**: 4 file(s)
  - scripts/bootstrap_structure_baselines.py
  - tests/test_structure_sentinel.py
  - web_interface/routes/management/ingestion.py
  - web_interface/run_ingest_refresh.py
- **fyp.studies**: 17 file(s)
  - scripts/adhoc/repro_assemble_cache.py
  - scripts/adhoc/repro_session_profile.py
  - tests/debug/smoke_enrichment_patch.py
  - tests/unit/test_bbc_jacqui_stats.py
  - tests/unit/test_calc.py
  - tests/unit/test_timeline_analysis.py
  - web_interface/routes/management/collections.py
  - web_interface/routes/management/studies.py
  - web_interface/run_collection_delete.py
  - web_interface/run_meta_refresh_groups.py
  - web_interface/run_pca_refresh.py
  - web_interface/run_recode_refresh_studies.py
  - web_interface/run_sequence_refresh.py
  - web_interface/run_study_refresh.py
  - web_interface/run_timelines_refresh.py
  - web_interface/services/study_data.py
  - web_interface/services/user_variables.py
- **fyp.tiktok_dl**: 3 file(s)
  - scripts/adhoc/repair_overflowed_playcounts.py
  - tests/unit/test_scraper_slideshow_hooks.py
  - tests/unit/test_ytdlp_backend.py
- **fyp.timeline_analysis**: 6 file(s)
  - tests/unit/test_timeline_analysis.py
  - tests/unit/test_timeline_optimizations.py
  - web_interface/run_timelines_refresh.py
  - web_interface/semantic_trajectory.py
  - web_interface/services/timeline_service.py
  - web_interface/services/user_variables.py
- **fyp.types**: 7 file(s)
  - tests/ab_eval/_ab_common.py
  - tests/bench/bench_parquet_loads.py
  - tests/bench/bench_post_load_overhead.py
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_polars_ops_migration.py
  - tests/unit/test_types_downgrade.py
  - web_interface/run_retokenise_hashtags.py
- **fyp.utils**: 4 file(s)
  - tests/test_progress_message_format.py
  - tests/unit/test_consolidate_rare_columns.py
  - web_interface/explorer_backend.py
  - web_interface/services/timeline_service.py
- **fyp.var_presentation**: 5 file(s)
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_retired_column_migration.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_var_schema_phase1.py
  - web_interface/routes/management/schema.py
- **fyp.video_map**: 5 file(s)
  - scripts/adhoc/repro_niche_naming.py
  - tests/test_niche_name_dedupe.py
  - web_interface/routes/api_semantic_space_routes.py
  - web_interface/run_video_map_refresh.py
  - web_interface/semantic_trajectory.py
- **fyp.youtube_dl**: 2 file(s)
  - tests/test_youtube_media_retry.py
  - tests/unit/test_youtube_scraper.py
