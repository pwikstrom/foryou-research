# fyp/ subpackage restructure — import graph & module assignment

This document records the import-dependency analysis behind the restructure
of the flat `fyp/` package into domain subpackages, and the reasoning for
each module's assignment. The **placement rules** below are load-bearing —
they explain constraints that still govern where new modules may live (see
also the invariants in `CONTRIBUTING.md`). The raw import matrix is
generated data, not documentation; produce a current one with
`python scripts/gen_import_graph.py` (see the end of this file).

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

## The import matrix

Earlier revisions of this document embedded the full generated import matrix
(internal `fyp → fyp` adjacency plus external importers per module). It was a
point-in-time snapshot of the restructure and had drifted badly — the
post-restructure modules listed above were never part of it — so it has been
removed rather than left to mislead. Generate a current one on demand:

```bash
python scripts/gen_import_graph.py
```

The placement rules above are the durable content of this document; the
matrix is reproducible data.
