# `fyp/` package layout — module placement and import rules

`fyp/` is organized into five subpackages. This document maps each module to
its subpackage and states the **placement rules** that govern where a new
module may live (see also the invariants in
[CONTRIBUTING.md](../CONTRIBUTING.md#invariants-you-must-not-break)). The
import-dependency analysis behind the layout, and the history of the
restructure, are in [decision 0002](decisions/0002-fyp-subpackage-restructure.md).
The raw import matrix is generated data, not documentation; produce a current
one with `python scripts/gen_import_graph.py` (see the end of this file).

## Module → subpackage assignment

| Subpackage | `__init__` behavior | Modules |
|---|---|---|
| `fyp/core/` | inert (docstring only) | `paths`, `fyp_config`, `runtime`, `artifacts`, `var_schema_vocab`, `activity_vocabulary`, `progress_monitor`, `data_io`, `types`, `utils`, `logging_setup`, `polars_ops`, `memory`, `media_paths`, `gemini_client`, `registry_metadata`, `activity_contract`, `activity_versioning`, `derived_contract` |
| `fyp/ingest/` | **eager** — imports `base`, then `tiktok`, `instagram`, `youtube` (registration order pinned) | `base`, `transforms`, `ingestion_ledger`, `structure_sentinel`, `donations`, `tiktok`, `instagram`, `youtube`, `raw_names`, `migrations/` |
| `fyp/scrape/` | eager re-exports from `.scrape` (and `.consolidate`, `.failures`, `.slideshow`, split out of it) + forwarding `__getattr__`; **must not boot config** | `scrape`, `consolidate`, `failures`, `slideshow`, `platform_scraper`, `tiktok_dl`, `instagram_dl`, `youtube_dl`, `scraper_cookies`, `scrape_queues`, `scrape_contract`, `scrape_versioning`, `scraper_alerts`, `connectivity` |
| `fyp/annotation/` | inert | `machine_annotation`, `gemini_calls`, `response_parsing`, `annotation_refinement`, `machine_annotation_batch`, `annotation_contract`, `annotation_schema`, `annotation_versioning`, `ab_eval`, `human_eval`, `recode_variables`, `var_presentation`, `irrelevant_words`, `backends/` |
| `fyp/analysis/` | inert | `pca`, `stats`, `embeddings`, `embedding_store`, `embedding_backends/`, `video_map`, `session_explorer`, `sessions/` (`inputs`, `segment`, `plan`, `publish`), `entropy_metrics`, `sequence_analysis`, `timeline_analysis`, `activity_analysis`, `calc_collection_stats`, `studies`, `organize_datasets`, `datasets/` (`common`, `loading`, `sampling`, `merge`, `refresh`, `enrichment_status`) |

Most modules also keep an old flat path (`fyp/<module>.py`) as a
back-compat shim for code outside this repository. First-party code imports
only the canonical paths: ruff's banned-api rule (`TID251`, listed in
`pyproject.toml`) rejects the flat ones, and
`tests/unit/test_subpackage_shims.py` keeps the shims themselves working.
These modules have **no** flat shim and exist only at their subpackage path:
`fyp/core/paths.py` (project-root discovery), `fyp/core/memory.py` (RSS/peak
probes), `fyp/core/runtime.py` (import-light
accessors: `cf`, `label`, `is_cloud_run`, `graceful_stop_requested`),
`fyp/core/gemini_client.py` (shared Google GenAI client construction),
`fyp/ingest/raw_names.py` (generated raw-upload names and collection ids),
`fyp/ingest/migrations/` (one-off rewrites of stored activity data, driven by
`scripts/migrate_*.py`), `fyp/scrape/scraper_alerts.py` (persistent
per-platform scraper alerts), `fyp/scrape/connectivity.py` (the scrapers'
online probe and batch gate), `fyp/annotation/backends/` and
`fyp/analysis/embedding_backends/` (backend registries),
`fyp/analysis/embedding_store.py` (dense random-access sidecar),
`fyp/analysis/session_explorer.py`, `fyp/analysis/sessions/` and
`fyp/analysis/entropy_metrics.py` (Sessions tab build), `fyp/core/artifacts.py`
(shared artifact names and readers), and every module split out of a larger
one below.

**Split modules keep their old surface.** When a large module is split
(`scrape` → `consolidate` / `failures` / `slideshow`; `machine_annotation`
→ `gemini_calls` / `response_parsing` / `annotation_refinement`;
`organize_datasets` → `datasets/`; `session_explorer` → `sessions/`;
`ingest/base` → `transforms` / `ingestion_ledger`), the original module
re-exports the moved public names, so its flat shim and any outside caller
keep working. First-party code imports and patches each name where it now
lives, and the split modules call their siblings through the module
(`inputs.load_plays(...)`), so a test patches one place and reaches every
caller.

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

## Placement rules

1. **Boot rule.** `import fyp.ingest` triggers the config boot *by design*
   (collection `__init_subclass__` → `data_io.register_location()`); every
   other module must stay lazy (pinned by
   `tests/unit/test_lazy_config_boot.py`). Importing any submodule of a
   package executes the package `__init__` first — so a module placed inside
   `fyp/ingest/` boots config on import. Therefore `fyp/ingest/` holds
   **only** ingest code whose callers already run with config booted — the
   collection classes, `raw_names` (imported by `base` and the upload
   routes) and `migrations/` (run from `scripts/migrate_*.py`) — while
   ingest-adjacent modules such as `organize_datasets`, `donations`
   (→ `analysis/`), `structure_sentinel`, `activity_contract` and
   `activity_versioning` (→ `core/`) live elsewhere.
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
   `fyp/scrape/__init__` → `scrape.py` / `platform_scraper.py`; if those
   did `from fyp import scrape_contract as sc`, `sc` would be bound to the
   empty shim; every later `sc.load_contract()` dies at runtime while
   import, boot, and the hash tripwire all pass. **Therefore all
   same-package sibling imports inside `fyp/scrape/*` (and `fyp/ingest/*`)
   are relative** (`from . import scrape_contract as sc`), which routes the
   cascade through the package directly and never back through a mid-flight
   shim. Cross-package old-path imports are safe: the underlying module
   graph is acyclic at module level (`python scripts/gen_import_graph.py`
   prints it), so no shim can be
   re-entered while partial. `tests/unit/test_subpackage_shims.py` probes
   exactly this failure mode in fresh interpreters.
4. **Scrape `__init__` eagerness is minimal.** It imports only `.scrape`
   (needed to re-export the old `fyp.scrape` API). It must *not* import the
   `*_dl` modules: they load `yt_dlp`, which would newly run inside every
   config boot and inside `import fyp.pca` — a real behavior change.
   Scrapers keep loading lazily via
   `platform_scraper._ensure_scrapers_imported()`.

## The import matrix

The internal `fyp → fyp` adjacency and the external importers of each module
are generated on demand rather than kept here, so they cannot drift:

```bash
python scripts/gen_import_graph.py
```

The placement rules above are the durable content of this document; the
matrix is reproducible data.
