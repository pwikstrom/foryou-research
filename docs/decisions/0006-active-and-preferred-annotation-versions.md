# 0006. "Active" and "preferred" annotation versions

Date: 2026-07-27

## Context

The annotation version registry has two pointers that were easy to confuse:
the version the next annotation will be stamped with, and the version studies
read when an item has been annotated under several. The registry stored the
second under the key `active`, while the code and the UI used "active",
"current" and "live" loosely for either.

## Decision

Two words, used verbatim in code and UI, never interchangeably:

- **active** — the version the next annotation is stamped with. Derived, not
  stored: `annotation_versioning.active_annotation_version()` /
  `active_version_descriptor()`, from the live contract, the selected
  backend and the generation parameters.
- **preferred** — the version studies read. Stored in the registry's
  `preferred` key and changed only by `promote_version()`
  (`get_preferred_version()`, `select_preferred_view()`,
  `rebuild_preferred_annotations_from_archive()`,
  `POST /api/manage/annotation-versions/promote`).

The Versions page uses "Activate" / "Active" and "Prefer" / "Preferred";
"current" and "live" are retired for this concept.

## Consequences

- Registries written before the change store the preferred version under
  `active`; `load_registry()` migrates the key on read, and the scrape and
  activity registries do the same.
- The ab_eval arm value `source: "live"` stays as a frozen wire value in
  stored run manifests but is displayed as "active contract".
- The per-study methods note uses the same vocabulary
  (`web_interface/services/methods_note.py`). The concept is explained in
  [contracts.md](../contracts.md#the-runtime-annotation-contract).
