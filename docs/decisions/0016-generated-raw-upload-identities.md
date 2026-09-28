# 0016. Generated raw-upload identities and one collection per display ID

Date: 2026-09-07

## Context

A raw upload was stored under the browser's filename, and that name also fed
its collection id. Every TikTok export is called `user_data_tiktok.json`. On
2026-09-06 one such upload was skipped as "already processed" and, on the way
in, overwrote an older donor's raw file with the same name.

A related gap: display IDs (the human-readable collection names) were not
unique, so two collections could answer to the same name in pickers and
reports.

## Decision

- A raw upload's stored filename and its collection id are **generated**
  (platform, source, upload time, random suffix) by
  `fyp/ingest/raw_names.py`. The browser's filename is provenance only, kept
  on the manifest entry and used as the default display ID.
- Raw locations and the archive are append-only at the storage layer:
  `data_io.move` and renames into them raise instead of overwriting.
- The ingester reports a pending entry whose name is already taken rather
  than skipping it.
- An explicit collection id may append to an existing collection only under
  the same account.
- Display IDs name one collection each (2026-09-09): `unique_display_label`
  suffixes ` (2)` at upload, and a rename onto a name another collection
  answers to (its display ID or its collection id, case and whitespace
  ignored) is refused with a 409. Pre-existing duplicates are flagged, not
  renamed behind the operator's back.

## Consequences

- Only a rename is checked for collisions: the bulk edit and the modal's
  autosave resend the stored name on every tag change.
- Duplicates surface as `duplicate_display_ids` (an ops-report check) and a
  *duplicate* pill in Edit Collections and the study picker. The pill is
  computed over the tags file, not the listing (the listing endpoint sends
  each row its `displayIdTwins`), because a twin can be a tags entry with no
  metadata row; the ops report marks such an id *(no data)* and lists
  unowned ones under *Leftover collection entries*.
- Documented in [pipeline.md](../pipeline.md#1-ingestion-fypingest).
