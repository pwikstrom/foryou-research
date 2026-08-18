# fyp/ subpackage restructure — import graph & module assignment

This document records the import-dependency analysis behind the restructure
of the flat `fyp/` package into domain subpackages, and the reasoning for
each module's assignment. The raw matrix at the bottom is regenerated with:

```bash
python scripts/gen_import_graph.py > /tmp/matrix.md   # then replace the section below the marker
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

**Modules added after the restructure** (born inside a subpackage, no shim needed):
`fyp/core/memory.py` (RSS/peak probes), `fyp/scrape/scraper_alerts.py`,
`fyp/annotation/backends/` and `fyp/analysis/embedding_backends/` (backend
registries), `fyp/analysis/embedding_store.py` (dense random-access sidecar),
`fyp/analysis/session_explorer.py` and `fyp/analysis/entropy_metrics.py`
(Sessions tab build). The matrix below is the restructure snapshot and does not
include them.

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
   other module must stay lazy (pinned by
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
so the restructure moves the module whole. A future split should relocate the test
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

## Internal adjacency (fyp -> fyp)

`(L)` marks a lazy (function-scoped) import.

- **ab_eval** -> annotation
- **activity_analysis** -> analysis
- **activity_contract** -> core
- **activity_versioning** -> core
- **analysis.activity_analysis** -> logging_setup
- **analysis.calc_collection_stats** -> activity_analysis, logging_setup
- **analysis.donations** -> calc_collection_stats, data_io, fyp_config (L), logging_setup, organize_datasets (L), recode_variables
- **analysis.embedding_backends.__init__** -> analysis.embedding_backends.base, analysis.embedding_backends.settings (L)
- **analysis.embedding_backends.base** -> annotation.backends.base
- **analysis.embedding_backends.gemini** -> analysis.embedding_backends.base, core.gemini_client, fyp_config
- **analysis.embedding_backends.qwen_api** -> analysis.embedding_backends.base, annotation.backends.qwen_api, fyp_config, logging_setup
- **analysis.embedding_backends.qwen_local** -> analysis.embedding_backends (L), analysis.embedding_backends.base, fyp_config, logging_setup
- **analysis.embedding_backends.qwen_support** -> annotation.backends.base, annotation.backends.qwen_support, fyp_config
- **analysis.embedding_backends.settings** -> annotation.backends.settings, data_io
- **analysis.embedding_store** -> analysis, data_io, logging_setup
- **analysis.embeddings** -> analysis.embedding_backends, core.utils, data_io, logging_setup
- **analysis.organize_datasets** -> annotation_versioning, data_io, fyp_config (L), logging_setup, machine_annotation, memory, polars_ops, recode_variables, scrape, scrape_contract, studies, utils
- **analysis.pca** -> data_io, fyp_config (L), logging_setup, organize_datasets, recode_variables, types
- **analysis.session_explorer** -> analysis, data_io, fyp_config (L), logging_setup, organize_datasets
- **analysis.stats** -> fyp_config (L), logging_setup, recode_variables
- **analysis.studies** -> data_io, fyp_config (L), logging_setup
- **analysis.timeline_analysis** -> logging_setup
- **analysis.video_map** -> core.gemini_client, data_io, embeddings, fyp_config (L), logging_setup
- **annotation.ab_eval** -> annotation.backends (L), annotation.machine_annotation (L), annotation_contract, annotation_schema, core (L), data_io, fyp_config (L), machine_annotation (L), recode_variables (L), types
- **annotation.annotation_contract** -> data_io (L), logging_setup, recode_variables (L)
- **annotation.annotation_schema** -> annotation_contract
- **annotation.annotation_versioning** -> annotation.backends (L), annotation_contract (L), annotation_schema (L), data_io (L), fyp_config (L), logging_setup, registry_metadata (L)
- **annotation.backends.__init__** -> annotation.backends (L), annotation.backends.base, annotation.backends.settings (L), logging_setup (L)
- **annotation.backends.base** -> fyp_config (L)
- **annotation.backends.gemini** -> annotation (L), annotation.backends.base, core.gemini_client, fyp_config, machine_annotation (L)
- **annotation.backends.minicpm_local** -> annotation (L), annotation.annotation_schema (L), annotation.backends (L), annotation.backends.base, annotation.backends.minicpm_sanitize_fix (L), annotation.backends.qwen_local, fyp_config, logging_setup
- **annotation.backends.minicpm_sanitize_fix** -> logging_setup
- **annotation.backends.minicpm_support** -> annotation.backends.base, annotation.backends.qwen_support, fyp_config
- **annotation.backends.qwen_api** -> annotation (L), annotation.annotation_schema (L), annotation.backends.base, annotation.backends.qwen_local, fyp_config, logging_setup
- **annotation.backends.qwen_local** -> annotation (L), annotation.annotation_schema (L), annotation.backends (L), annotation.backends.base, annotation.backends.qwen_rope_fix (L), core (L), fyp_config, logging_setup, scrape (L)
- **annotation.backends.qwen_rope_fix** -> logging_setup
- **annotation.backends.qwen_support** -> annotation.backends.base, fyp_config
- **annotation.backends.settings** -> data_io
- **annotation.backends.variants** -> annotation.backends (L), annotation.backends.minicpm_local (L), annotation.backends.qwen_api (L), annotation.backends.qwen_local (L), fyp_config (L), logging_setup
- **annotation.human_eval** -> ab_eval, annotation_contract, data_io, fyp_config (L), logging_setup
- **annotation.irrelevant_words** -> data_io (L), fyp_config (L), logging_setup
- **annotation.machine_annotation** -> annotation.backends (L), annotation_schema, annotation_versioning, core.gemini_client, data_io, fyp_config (L), logging_setup, media_paths, recode_variables, scrape_queues, types, utils
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
- **core.fyp_config** -> activity_contract (L), activity_versioning (L), annotation_contract (L), annotation_versioning (L), core.paths, data_io (L), derived_contract (L), recode_variables (L), scrape_contract (L), scrape_versioning (L), var_presentation (L)
- **core.gemini_client** -> core.fyp_config (L), core.logging_setup
- **core.media_paths** -> fyp_config (L), scrape_queues (L)
- **core.memory** -> logging_setup
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
- **ingest.demo_dataset** -> annotation (L), annotation.machine_annotation (L), core.utils, data_io (L), scrape.platform_scraper (L), scrape.scrape (L), studies (L)
- **ingest.instagram** -> data_io, ingest.base, logging_setup, utils
- **ingest.tiktok** -> data_io, donations (L), fyp_config (L), ingest.base, logging_setup, recode_variables, utils
- **ingest.youtube** -> data_io, ingest.base, logging_setup, utils
- **instagram_dl** -> scrape
- **irrelevant_words** -> annotation
- **logging_setup** -> core
- **machine_annotation** -> annotation
- **machine_annotation_batch** -> annotation
- **media_paths** -> core
- **memory** -> core
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
- **scrape.scraper_alerts** -> data_io, logging_setup
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

- **fyp.__version__**: 1 file(s)
  - web_interface/services/methods_note.py
- **fyp.ab_eval**: 9 file(s)
  - tests/unit/test_ab_eval.py
  - tests/unit/test_ab_eval_backend_arms.py
  - tests/unit/test_ab_eval_manifest_candidate.py
  - tests/unit/test_backend_variants.py
  - tests/unit/test_graduation_backend.py
  - tests/unit/test_human_eval.py
  - web_interface/routes/human_eval_routes.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/run_ab_eval.py
- **fyp.activity_contract**: 3 file(s)
  - tests/unit/test_activity_contract.py
  - web_interface/routes/management/data_contracts.py
  - web_interface/routes/management/schema.py
- **fyp.activity_versioning**: 4 file(s)
  - tests/unit/test_data_contracts_api.py
  - web_interface/routes/management/data_contracts.py
  - web_interface/routes/management/schema.py
  - web_interface/run_ingest_refresh.py
- **fyp.analysis**: 21 file(s)
  - tests/unit/test_binge_skip_tolerance.py
  - tests/unit/test_embedding_store.py
  - tests/unit/test_entropy_metrics.py
  - tests/unit/test_fresh_install_fixes.py
  - tests/unit/test_group_stats.py
  - tests/unit/test_memory_probe.py
  - tests/unit/test_niche_join_dedupe.py
  - tests/unit/test_niche_join_measures.py
  - tests/unit/test_pca_yes_share.py
  - tests/unit/test_session_explorer.py
  - tests/unit/test_sessions_chain.py
  - tests/unit/test_sessions_chain_guard.py
  - tests/unit/test_sessions_enrichment_staleness.py
  - tests/unit/test_sessions_merge_publish.py
  - tests/unit/test_sessions_plays_artifact.py
  - tests/unit/test_sessions_refresh_plan.py
  - tests/unit/test_sessions_routes.py
  - web_interface/routes/api_sessions_routes.py
  - web_interface/run_embeddings_refresh.py
  - web_interface/run_sessions_refresh.py
  - web_interface/run_timelines_refresh.py
- **fyp.analysis.embedding_backends**: 10 file(s)
  - tests/unit/test_embedding_backend_registry.py
  - tests/unit/test_embedding_gating.py
  - tests/unit/test_embedding_qwen_api.py
  - tests/unit/test_embedding_qwen_support.py
  - web_interface/admin_settings.py
  - web_interface/process_manager.py
  - web_interface/routes/management/enrichment.py
  - web_interface/routes/process_routes.py
  - web_interface/run_consolidate_enrichment.py
  - web_interface/services/system_health.py
- **fyp.analysis.embedding_backends.base**: 1 file(s)
  - tests/unit/test_embedding_gating.py
- **fyp.analysis.embedding_backends.gemini**: 1 file(s)
  - tests/unit/test_embedding_qwen_api.py
- **fyp.analysis.embedding_backends.settings**: 1 file(s)
  - web_interface/admin_settings.py
- **fyp.analysis.embeddings**: 3 file(s)
  - tests/unit/test_demo_generator.py
  - tests/unit/test_embedding_qwen_api.py
  - tests/unit/test_embeddings_store_scoping.py
- **fyp.analysis.organize_datasets**: 1 file(s)
  - tests/unit/test_correlations_variable_redesign.py
- **fyp.analysis.pca**: 1 file(s)
  - tests/unit/test_correlations_variable_redesign.py
- **fyp.analysis.stats**: 2 file(s)
  - web_interface/run_pca_refresh.py
  - web_interface/services/correlations_service.py
- **fyp.analysis.studies**: 1 file(s)
  - tests/unit/test_study_access.py
- **fyp.analysis.timeline_analysis**: 2 file(s)
  - tests/unit/test_timelines_import_race.py
  - web_interface/run_timelines_refresh.py
- **fyp.analysis.video_map**: 3 file(s)
  - tests/unit/test_video_map_columns.py
  - tests/unit/test_video_map_local_naming.py
  - tests/unit/test_video_map_typicality.py
- **fyp.annotation**: 6 file(s)
  - tests/unit/test_contract_simplification.py
  - tests/unit/test_fresh_install_fixes.py
  - tests/unit/test_pool_import_race.py
  - web_interface/routes/api_viewer_routes.py
  - web_interface/routes/management/enrichment.py
  - web_interface/services/system_health.py
- **fyp.annotation.annotation_contract**: 1 file(s)
  - tests/unit/test_demo_generator.py
- **fyp.annotation.annotation_schema**: 2 file(s)
  - tests/unit/test_demo_generator.py
  - tests/unit/test_pool_import_race.py
- **fyp.annotation.backends**: 22 file(s)
  - scripts/setup.py
  - tests/unit/test_ab_eval_backend_arms.py
  - tests/unit/test_admin_settings_route.py
  - tests/unit/test_backend_dispatch.py
  - tests/unit/test_backend_registry.py
  - tests/unit/test_backend_variants.py
  - tests/unit/test_cost_guardrails.py
  - tests/unit/test_gemini_client_modes.py
  - tests/unit/test_graduation_backend.py
  - tests/unit/test_minicpm_support.py
  - tests/unit/test_qwen_local_backend.py
  - tests/unit/test_qwen_support.py
  - tests/unit/test_qwen_versioning.py
  - web_interface/admin_settings.py
  - web_interface/process_manager.py
  - web_interface/routes/auth_routes.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/enrichment.py
  - web_interface/run_queue_annotator.py
  - web_interface/run_queue_annotator_batch.py
  - web_interface/services/system_health.py
- **fyp.annotation.backends.base**: 1 file(s)
  - tests/unit/test_backend_dispatch.py
- **fyp.annotation.backends.minicpm_local**: 1 file(s)
  - tests/unit/test_minicpm_local_backend.py
- **fyp.annotation.backends.minicpm_sanitize_fix**: 1 file(s)
  - tests/unit/test_minicpm_local_backend.py
- **fyp.annotation.backends.qwen_api**: 1 file(s)
  - tests/unit/test_qwen_api_backend.py
- **fyp.annotation.backends.qwen_local**: 2 file(s)
  - tests/unit/test_ab_eval_backend_arms.py
  - tests/unit/test_qwen_local_backend.py
- **fyp.annotation.backends.qwen_rope_fix**: 1 file(s)
  - tests/unit/test_qwen_local_backend.py
- **fyp.annotation.backends.settings**: 2 file(s)
  - web_interface/admin_settings.py
  - web_interface/routes/management/contracts.py
- **fyp.annotation.machine_annotation**: 4 file(s)
  - tests/unit/test_gemini_client_modes.py
  - tests/unit/test_pool_import_race.py
  - web_interface/routes/management/enrichment.py
  - web_interface/routes/process_routes.py
- **fyp.annotation.machine_annotation_batch**: 1 file(s)
  - tests/unit/test_batch_annotator_ux.py
- **fyp.annotation_contract**: 16 file(s)
  - tests/golden/test_generated_contract_equivalence.py
  - tests/golden/test_schema_pipeline_consistency.py
  - tests/unit/test_ab_eval.py
  - tests/unit/test_ab_eval_backend_arms.py
  - tests/unit/test_ab_eval_manifest_candidate.py
  - tests/unit/test_annotation_contract_api.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_contract_accepted_labels.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_graduation_backend.py
  - tests/unit/test_role_rename.py
  - tests/unit/test_runtime_annotation_contract.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/schema.py
  - web_interface/run_ab_eval.py
- **fyp.annotation_schema**: 7 file(s)
  - tests/golden/test_generated_contract_equivalence.py
  - tests/golden/test_structured_flatten_equivalence.py
  - tests/unit/test_ab_eval.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_runtime_annotation_contract.py
  - web_interface/routes/management/contracts.py
- **fyp.annotation_versioning**: 17 file(s)
  - tests/golden/test_schema_pipeline_consistency.py
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/test_annotation_versioning.py
  - tests/unit/test_backend_variants.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_graduation_backend.py
  - tests/unit/test_machine_config_normalizer.py
  - tests/unit/test_multiplatform_annotation.py
  - tests/unit/test_qwen_versioning.py
  - tests/unit/test_runtime_annotation_contract.py
  - web_interface/routes/auth_routes.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/schema.py
  - web_interface/routes/management/studies.py
  - web_interface/services/methods_note.py
  - web_interface/services/study_data.py
- **fyp.core**: 5 file(s)
  - tests/unit/test_io_log_gate.py
  - tests/unit/test_pool_import_race.py
  - web_interface/routes/process_routes.py
  - web_interface/services/correlations_service.py
  - web_interface/services/system_health.py
- **fyp.core.data_io**: 2 file(s)
  - tests/unit/test_multiindex_repair.py
  - web_interface/run_timelines_refresh.py
- **fyp.core.fyp_config**: 1 file(s)
  - tests/unit/test_machine_config_normalizer.py
- **fyp.core.gemini_client**: 1 file(s)
  - tests/unit/test_gemini_client_modes.py
- **fyp.core.memory**: 1 file(s)
  - tests/unit/test_memory_probe.py
- **fyp.core.utils**: 3 file(s)
  - tests/unit/test_cost_guardrails.py
  - tests/unit/test_demo_generator.py
  - web_interface/routes/management/enrichment.py
- **fyp.data_io**: 89 file(s)
  - scripts/bootstrap_structure_baselines.py
  - tests/golden/test_batch_worker.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/test_ab_eval.py
  - tests/unit/test_ab_eval_backend_arms.py
  - tests/unit/test_ab_eval_manifest_candidate.py
  - tests/unit/test_admin_settings_route.py
  - tests/unit/test_annotate_calc.py
  - tests/unit/test_annotation_contract_api.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_backend_dispatch.py
  - tests/unit/test_bbc_jacqui_stats.py
  - tests/unit/test_calc.py
  - tests/unit/test_correlations_api.py
  - tests/unit/test_data_io_rename.py
  - tests/unit/test_data_io_streaming.py
  - tests/unit/test_embedding_store.py
  - tests/unit/test_embeddings_chain_guard.py
  - tests/unit/test_empty_filter.py
  - tests/unit/test_empty_write.py
  - tests/unit/test_fillna.py
  - tests/unit/test_graduation_backend.py
  - tests/unit/test_human_eval.py
  - tests/unit/test_io_log_gate.py
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_load_parquet_selective.py
  - tests/unit/test_metadata_selective_loads.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_retokenise_hashtags.py
  - tests/unit/test_runtime_annotation_contract.py
  - tests/unit/test_sequence_analysis.py
  - tests/unit/test_sessions_chain.py
  - tests/unit/test_sessions_chain_guard.py
  - tests/unit/test_sessions_enrichment_staleness.py
  - tests/unit/test_sessions_merge_publish.py
  - tests/unit/test_sessions_plays_artifact.py
  - tests/unit/test_task_failures.py
  - tests/unit/test_timeline_analysis.py
  - tests/unit/test_timelines_import_race.py
  - tests/unit/test_update_json.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_zee_generic_fix.py
  - tests/unit/test_zee_generic_step.py
  - web_interface/activity_log.py
  - web_interface/admin_settings.py
  - web_interface/auth.py
  - web_interface/drain_lease.py
  - web_interface/explorer_backend.py
  - web_interface/process_manager.py
  - web_interface/routes/api_explorer_routes.py
  - web_interface/routes/api_semantic_space_routes.py
  - web_interface/routes/api_sessions_routes.py
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
  - web_interface/run_embeddings_refresh.py
  - web_interface/run_logs.py
  - web_interface/run_meta_refresh_groups.py
  - web_interface/run_pca_refresh.py
  - web_interface/run_queue_annotator.py
  - web_interface/run_queue_annotator_batch.py
  - web_interface/run_recode_refresh_studies.py
  - web_interface/run_retokenise_hashtags.py
  - web_interface/run_sequence_refresh.py
  - web_interface/run_sessions_refresh.py
  - web_interface/run_study_refresh.py
  - web_interface/run_timelines_refresh.py
  - web_interface/semantic_trajectory.py
  - web_interface/services/analysis_data.py
  - web_interface/services/correlations_service.py
  - web_interface/services/methods_note.py
  - web_interface/services/preview_cache.py
  - web_interface/services/stats_service.py
  - web_interface/services/study_data.py
  - web_interface/services/timeline_service.py
  - web_interface/services/user_variables.py
  - web_interface/task_failures.py
  - web_interface/task_status.py
- **fyp.derived_contract**: 4 file(s)
  - tests/unit/test_correlations_variable_redesign.py
  - tests/unit/test_derived_contract.py
  - web_interface/routes/management/data_contracts.py
  - web_interface/routes/management/schema.py
- **fyp.donations**: 2 file(s)
  - web_interface/run_aio_fetch.py
  - web_interface/run_collection_metadata_refresh.py
- **fyp.embeddings**: 6 file(s)
  - tests/unit/test_embeddings_chain_guard.py
  - tests/unit/test_embeddings_decode.py
  - web_interface/routes/api_semantic_space_routes.py
  - web_interface/routes/api_sessions_routes.py
  - web_interface/run_embeddings_refresh.py
  - web_interface/semantic_trajectory.py
- **fyp.fyp_config**: 92 file(s)
  - tests/_storage_guard.py
  - tests/golden/_harness.py
  - tests/golden/test_batch_worker.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/conftest.py
  - tests/unit/test_ab_eval_backend_arms.py
  - tests/unit/test_admin_settings_route.py
  - tests/unit/test_annotate_calc.py
  - tests/unit/test_annotation_contract_api.py
  - tests/unit/test_annotation_contract_editor.py
  - tests/unit/test_backend_variants.py
  - tests/unit/test_bbc_jacqui_stats.py
  - tests/unit/test_binge_skip_tolerance.py
  - tests/unit/test_calc.py
  - tests/unit/test_call_machine_retry.py
  - tests/unit/test_call_machine_success_error_field.py
  - tests/unit/test_check_existing_media.py
  - tests/unit/test_consolidate_progress.py
  - tests/unit/test_content_category_recoding.py
  - tests/unit/test_contract_accepted_labels.py
  - tests/unit/test_contract_source_platform.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_correlations_variable_redesign.py
  - tests/unit/test_data_io_rename.py
  - tests/unit/test_data_io_streaming.py
  - tests/unit/test_embedding_qwen_api.py
  - tests/unit/test_embedding_store.py
  - tests/unit/test_fillna.py
  - tests/unit/test_fresh_install_fixes.py
  - tests/unit/test_gemini_client_modes.py
  - tests/unit/test_graduation_backend.py
  - tests/unit/test_import_cycle_hash.py
  - tests/unit/test_instagram_image_posts.py
  - tests/unit/test_instagram_scraper.py
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_load_parquet_selective.py
  - tests/unit/test_local_first_config.py
  - tests/unit/test_machine_config_normalizer.py
  - tests/unit/test_media_path_resolution.py
  - tests/unit/test_media_resolution.py
  - tests/unit/test_metadata_selective_loads.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_qwen_local_backend.py
  - tests/unit/test_recode_series_branches.py
  - tests/unit/test_registry_metadata.py
  - tests/unit/test_role_rename.py
  - tests/unit/test_runtime_annotation_contract.py
  - tests/unit/test_scraper_cookies.py
  - tests/unit/test_sequence_analysis.py
  - tests/unit/test_sessions_chain.py
  - tests/unit/test_sessions_merge_publish.py
  - tests/unit/test_sessions_plays_artifact.py
  - tests/unit/test_sessions_routes.py
  - tests/unit/test_sessions_worker_registration.py
  - tests/unit/test_study_access.py
  - tests/unit/test_timeline_analysis.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_var_schema_phase1.py
  - tests/unit/test_zee_generic_fix.py
  - tests/unit/test_zee_generic_step.py
  - web_interface/activity_log.py
  - web_interface/admin_settings.py
  - web_interface/explorer_backend.py
  - web_interface/fyp_data_hub.py
  - web_interface/mail_utils.py
  - web_interface/process_manager.py
  - web_interface/routes/api_explorer_routes.py
  - web_interface/routes/api_sessions_routes.py
  - web_interface/routes/api_viewer_routes.py
  - web_interface/routes/auth_routes.py
  - web_interface/routes/management/ab_eval.py
  - web_interface/routes/management/collections.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/data_contracts.py
  - web_interface/routes/management/enrichment.py
  - web_interface/routes/management/ingestion.py
  - web_interface/routes/management/schema.py
  - web_interface/routes/management/studies.py
  - web_interface/routes/process_routes.py
  - web_interface/run_collection_delete.py
  - web_interface/run_logs.py
  - web_interface/run_meta_refresh_groups.py
  - web_interface/run_pca_refresh.py
  - web_interface/run_recode_refresh_studies.py
  - web_interface/run_sequence_refresh.py
  - web_interface/run_study_refresh.py
  - web_interface/services/correlations_service.py
  - web_interface/services/study_data.py
  - web_interface/services/system_health.py
  - web_interface/services/timeline_service.py
  - web_interface/services/user_variables.py
- **fyp.human_eval**: 3 file(s)
  - tests/unit/test_human_eval.py
  - web_interface/routes/human_eval_routes.py
  - web_interface/routes/management/ab_eval.py
- **fyp.ingest**: 10 file(s)
  - scripts/bootstrap_structure_baselines.py
  - tests/unit/test_derive_play_duration.py
  - tests/unit/test_fresh_install_fixes.py
  - tests/unit/test_home_getting_started.py
  - tests/unit/test_platform_backfills.py
  - web_interface/fyp_data_hub.py
  - web_interface/routes/api_viewer_routes.py
  - web_interface/routes/management/collections.py
  - web_interface/routes/management/ingestion.py
  - web_interface/run_ingest_refresh.py
- **fyp.ingest.base**: 2 file(s)
  - tests/unit/test_fresh_install_fixes.py
  - tests/unit/test_ingest_drop_stats.py
- **fyp.ingest.demo_dataset**: 3 file(s)
  - scripts/generate_demo_dataset.py
  - tests/unit/test_demo_generator.py
  - web_interface/run_demo_dataset.py
- **fyp.ingest.instagram**: 1 file(s)
  - tests/unit/test_fresh_install_fixes.py
- **fyp.ingest.tiktok**: 2 file(s)
  - tests/unit/test_demo_generator.py
  - tests/unit/test_fresh_install_fixes.py
- **fyp.ingest.youtube**: 1 file(s)
  - tests/unit/test_fresh_install_fixes.py
- **fyp.instagram_dl**: 1 file(s)
  - tests/unit/test_instagram_scraper.py
- **fyp.irrelevant_words**: 4 file(s)
  - tests/unit/test_irrelevant_words.py
  - tests/unit/test_irrelevant_words_api.py
  - tests/unit/test_retokenise_hashtags.py
  - web_interface/routes/auth_routes.py
- **fyp.logging_setup**: 6 file(s)
  - web_interface/drain_lease.py
  - web_interface/routes/api_correlations_routes.py
  - web_interface/run_logs.py
  - web_interface/services/correlations_service.py
  - web_interface/services/methods_note.py
  - web_interface/task_failures.py
- **fyp.machine_annotation**: 19 file(s)
  - tests/golden/_harness.py
  - tests/golden/test_batch_annotation.py
  - tests/golden/test_batch_worker.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_structured_flatten_equivalence.py
  - tests/golden/test_structured_refinement_path.py
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/test_annotation_repair.py
  - tests/unit/test_backend_dispatch.py
  - tests/unit/test_batch_annotator_ux.py
  - tests/unit/test_call_machine_retry.py
  - tests/unit/test_call_machine_success_error_field.py
  - tests/unit/test_consolidate_rare_columns.py
  - tests/unit/test_content_category_recoding.py
  - tests/unit/test_first_run_hardening.py
  - tests/unit/test_media_resolution.py
  - web_interface/routes/management/contracts.py
  - web_interface/run_queue_annotator.py
  - web_interface/run_queue_annotator_batch.py
- **fyp.machine_annotation_batch**: 3 file(s)
  - tests/golden/test_batch_annotation.py
  - tests/unit/test_multiplatform_annotation.py
  - web_interface/run_queue_annotator_batch.py
- **fyp.media_paths**: 3 file(s)
  - tests/unit/test_media_path_resolution.py
  - tests/unit/test_qwen_local_backend.py
  - web_interface/routes/api_viewer_routes.py
- **fyp.memory**: 2 file(s)
  - tests/unit/test_memory_probe.py
  - web_interface/run_sessions_refresh.py
- **fyp.organize_datasets**: 30 file(s)
  - tests/golden/test_versioning_consolidation.py
  - tests/unit/test_annotate_calc.py
  - tests/unit/test_calc.py
  - tests/unit/test_consolidate_progress.py
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_load_parquet_selective.py
  - tests/unit/test_metadata_selective_loads.py
  - tests/unit/test_platform_backfills.py
  - tests/unit/test_sessions_chain.py
  - tests/unit/test_sessions_plays_artifact.py
  - web_interface/explorer_backend.py
  - web_interface/routes/api_sessions_routes.py
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
- **fyp.pca**: 7 file(s)
  - tests/unit/test_pca_crosstab_regression.py
  - tests/unit/test_pca_highcard_exact.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_single_column_pca.py
  - web_interface/run_pca_refresh.py
  - web_interface/run_study_refresh.py
  - web_interface/services/analysis_data.py
- **fyp.platform_scraper**: 9 file(s)
  - tests/unit/test_engagement_per_play.py
  - tests/unit/test_platform_stamping.py
  - tests/unit/test_scrape_contract.py
  - tests/unit/test_scraper_slideshow_hooks.py
  - tests/unit/test_youtube_media_retry.py
  - tests/unit/test_youtube_scraper.py
  - web_interface/routes/management/enrichment.py
  - web_interface/run_queue_scraper.py
  - web_interface/services/worker_status.py
- **fyp.polars_ops**: 1 file(s)
  - tests/unit/test_polars_ops_migration.py
- **fyp.recode_variables**: 19 file(s)
  - tests/golden/build_golden.py
  - tests/golden/test_contract_cutover.py
  - tests/golden/test_schema_pipeline_consistency.py
  - tests/unit/test_australian_relevance_derivation.py
  - tests/unit/test_contract_accepted_labels.py
  - tests/unit/test_contract_simplification.py
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_irrelevant_words.py
  - tests/unit/test_pca_selective_load.py
  - tests/unit/test_recode_series_branches.py
  - tests/unit/test_retokenise_hashtags.py
  - tests/unit/test_role_rename.py
  - tests/unit/test_schema_cell_parsers.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_var_schema_phase1.py
  - web_interface/routes/management/contracts.py
  - web_interface/routes/management/schema.py
  - web_interface/run_retokenise_hashtags.py
  - web_interface/services/correlations_service.py
- **fyp.registry_metadata**: 1 file(s)
  - tests/unit/test_contract_source_platform.py
- **fyp.scrape**: 16 file(s)
  - tests/unit/test_changed_scrape_ids.py
  - tests/unit/test_check_existing_media.py
  - tests/unit/test_failed_scrape_records.py
  - tests/unit/test_instagram_image_posts.py
  - tests/unit/test_instagram_scrape_fixes.py
  - tests/unit/test_permanent_storm_guard.py
  - tests/unit/test_retired_column_migration.py
  - tests/unit/test_scraper_alerts.py
  - tests/unit/test_seed_merge.py
  - tests/unit/test_slideshow_audio.py
  - tests/unit/test_transient_storm_guard.py
  - tests/unit/test_youtube_media_retry.py
  - web_interface/routes/api_explorer_routes.py
  - web_interface/routes/management/enrichment.py
  - web_interface/run_queue_scraper.py
  - web_interface/services/system_health.py
- **fyp.scrape.instagram_dl**: 1 file(s)
  - tests/unit/test_instagram_image_posts.py
- **fyp.scrape.platform_scraper**: 5 file(s)
  - tests/unit/test_demo_generator.py
  - tests/unit/test_instagram_image_posts.py
  - tests/unit/test_instagram_scrape_fixes.py
  - tests/unit/test_system_health.py
  - web_interface/services/system_health.py
- **fyp.scrape.scrape_contract**: 1 file(s)
  - tests/unit/test_demo_generator.py
- **fyp.scrape_contract**: 8 file(s)
  - tests/unit/test_contract_source_platform.py
  - tests/unit/test_engagement_per_play.py
  - tests/unit/test_platform_stamping.py
  - tests/unit/test_retired_column_migration.py
  - tests/unit/test_scrape_contract.py
  - tests/unit/test_scrape_contract_platforms.py
  - web_interface/routes/management/data_contracts.py
  - web_interface/routes/management/schema.py
- **fyp.scrape_queues**: 6 file(s)
  - tests/unit/test_scrape_queue_migration.py
  - web_interface/drain_lease.py
  - web_interface/fyp_data_hub.py
  - web_interface/process_manager.py
  - web_interface/routes/management/enrichment.py
  - web_interface/run_queue_scraper.py
- **fyp.scrape_versioning**: 3 file(s)
  - tests/unit/test_data_contracts_api.py
  - web_interface/routes/management/data_contracts.py
  - web_interface/routes/management/schema.py
- **fyp.scraper_cookies**: 2 file(s)
  - tests/unit/test_cookie_race.py
  - tests/unit/test_scraper_cookies.py
- **fyp.sequence_analysis**: 2 file(s)
  - tests/unit/test_sequence_analysis.py
  - web_interface/run_sequence_refresh.py
- **fyp.structure_sentinel**: 3 file(s)
  - scripts/bootstrap_structure_baselines.py
  - web_interface/routes/management/ingestion.py
  - web_interface/run_ingest_refresh.py
- **fyp.studies**: 15 file(s)
  - tests/unit/test_bbc_jacqui_stats.py
  - tests/unit/test_calc.py
  - tests/unit/test_timeline_analysis.py
  - web_interface/fyp_data_hub.py
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
- **fyp.tiktok_dl**: 2 file(s)
  - tests/unit/test_scraper_slideshow_hooks.py
  - tests/unit/test_ytdlp_backend.py
- **fyp.timeline_analysis**: 6 file(s)
  - tests/unit/test_timeline_analysis.py
  - tests/unit/test_timeline_optimizations.py
  - web_interface/run_timelines_refresh.py
  - web_interface/semantic_trajectory.py
  - web_interface/services/timeline_service.py
  - web_interface/services/user_variables.py
- **fyp.types**: 5 file(s)
  - tests/unit/test_load_parquet_fast_path.py
  - tests/unit/test_polars_ops_migration.py
  - tests/unit/test_surrogate_scrub.py
  - tests/unit/test_types_downgrade.py
  - web_interface/run_retokenise_hashtags.py
- **fyp.utils**: 3 file(s)
  - tests/unit/test_consolidate_rare_columns.py
  - web_interface/explorer_backend.py
  - web_interface/services/timeline_service.py
- **fyp.var_presentation**: 5 file(s)
  - tests/unit/test_contract_variable_metadata.py
  - tests/unit/test_retired_column_migration.py
  - tests/unit/test_var_schema_api.py
  - tests/unit/test_var_schema_phase1.py
  - web_interface/routes/management/schema.py
- **fyp.video_map**: 4 file(s)
  - tests/unit/test_niche_name_dedupe.py
  - web_interface/routes/api_semantic_space_routes.py
  - web_interface/run_video_map_refresh.py
  - web_interface/semantic_trajectory.py
- **fyp.youtube_dl**: 4 file(s)
  - tests/unit/test_permanent_storm_guard.py
  - tests/unit/test_transient_storm_guard.py
  - tests/unit/test_youtube_media_retry.py
  - tests/unit/test_youtube_scraper.py
