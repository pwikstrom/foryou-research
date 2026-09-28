# Decision log

Dated records of why the Hub works the way it does. Each record captures one
decision (or a small group of closely related ones): the incident or finding
that prompted it, with the concrete evidence, the rule that was adopted, and
what follows from it — including the tests and guards that enforce it.

The how-to documents state the current rules and link here for the history.
A record is not updated when the code later changes; a new record supersedes
it and says so.

## Adding a record

1. Take the next free number and create `NNNN-short-kebab-title.md` in this
   directory. Records are numbered in chronological order of their date.
2. Use this layout:

   ```markdown
   # NNNN. Title

   Date: YYYY-MM-DD

   ## Context
   What happened or what was found, with the numbers.

   ## Decision
   The rule adopted.

   ## Consequences
   What it implies, and where it is enforced (tests and guards by path).
   ```

3. Add a row to the index below.
4. In the how-to document that states the rule, keep one to three sentences
   (the rule and its reason) and link the record, e.g.
   `(see [decision 0007](decisions/0007-empty-study-access-means-nobody.md))`.

Keep participant data, production identifiers and personal paths out of
records; describe what happened, not who or where.

## Index

| No. | Date | Title | Summary |
|---|---|---|---|
| [0001](0001-annotation-generation-settings.md) | 2026-06-19 | Annotation generation settings | n=80 A/B runs: keep HIGH media resolution, set temperature 0 with repetition penalties off |
| [0002](0002-fyp-subpackage-restructure.md) | 2026-07-12 | Subpackage restructure of `fyp/` | Why modules sit where they do, why `machine_annotation` moved whole, and what was not deduplicated |
| [0003](0003-scraper-storm-guards.md) | 2026-07-16 | Scraper storm guards | Abort, stop chaining and alert on runs of identical permanent or transient verdicts |
| [0004](0004-gemini-parameters-config-only.md) | 2026-07-21 | Gemini parameters are config-only | Model and generation parameters are version identity, so they are not runtime settings |
| [0005](0005-sectionless-annotation-prompt.md) | 2026-07-27 | Sectionless annotation prompt | Flat prompt; `scale` inferred from field shape except for free text |
| [0006](0006-active-and-preferred-annotation-versions.md) | 2026-07-27 | Active and preferred versions | Two pointers, two words; the registry key migrates on read |
| [0007](0007-empty-study-access-means-nobody.md) | 2026-07-31 | Empty study access means nobody | An unlisted study is shared with nobody; older studies were backfilled |
| [0008](0008-explicit-dispatch-deadlines.md) | 2026-08-09 | Explicit dispatch deadlines | Every self-chaining dispatch, including the first, carries a 1800 s deadline |
| [0009](0009-embeddings-single-flight-lease.md) | 2026-08-15 | Embeddings single-flight lease | A CAS lease and read-side dedupe after a redelivery wrote twin shards |
| [0010](0010-canonical-imports-only.md) | 2026-08-16 | Canonical imports only | Cold alias shims race in thread pools; first-party code uses canonical paths |
| [0011](0011-gcp-identifiers-supplied-at-build-time.md) | 2026-08-18 | GCP identifiers supplied at build time | No registry paths in the tree; Cloud Build configs take substitutions |
| [0012](0012-spawned-workers-pin-project-root.md) | 2026-08-31 | Spawned workers pin the project root | A worker spawned from a worktree pruned the production scrape queue |
| [0013](0013-scoped-sessions-enrichment-staleness.md) | 2026-09-03 | Scoped sessions enrichment staleness | Re-segment only the collections an append touches |
| [0014](0014-refresh-runs.md) | 2026-09-03 | Refresh runs | One dependency registry, pruning on stated no-change, consolidation scope wins, deferred debt has an owner |
| [0015](0015-enrichment-loop-and-shared-queues.md) | 2026-09-05 | Enrichment loop and shared queues | Four collisions between the loop and hand-run queues, and their fixes |
| [0016](0016-generated-raw-upload-identities.md) | 2026-09-07 | Generated raw-upload identities | Stored names and collection ids are generated; display IDs name one collection |
| [0017](0017-ingest-review-sticky-approvals-parse-rate-floor.md) | 2026-09-08 | Ingest review | Approvals stick, ledger saves merge, and a 10 % parse-rate floor quarantines |
| [0018](0018-enrichment-slice-sizing.md) | 2026-09-08 | Enrichment slice sizing | 200-item floor, whole sessions per day, derived days per month |
| [0019](0019-cloud-build-context.md) | 2026-09-09 | Cloud Build context | `.gcloudignore` is the filter that shapes deployed images |
| [0020](0020-raw-upload-folder-names.md) | 2026-09-16 | Raw-upload folder names | Historical folder names kept; a rename would be a data migration |
| [0021](0021-donor-time-zone.md) | 2026-09-17 | Donor time zone | A supplied zone wins on every platform; `tz_offset` is fractional hours |
| [0022](0022-engagement-vocabulary-and-inferred-links.md) | 2026-09-17 | Engagement vocabulary and inferred links | `fave` = like, `save` = bookmark; `link_method` names every inferred link |
| [0023](0023-media-leg-failures-batch-deadline-youtube-pacing.md) | 2026-09-19 | Media-leg failures, batch deadline, YouTube pacing | Lessons of a throttled YouTube drain |
| [0024](0024-corroborated-permanent-verdicts.md) | 2026-09-21 | Corroborated permanent verdicts | Per-item evidence exempts a verdict from the storm guards |
| [0025](0025-instagram-authentication.md) | 2026-09-23 | Instagram authentication | Anonymous first; the logged-in session spent sparingly |
| [0026](0026-only-final-scrape-failures-count.md) | 2026-09-25 | Only final scrape failures count | Retryable failures stay recorded but no longer exclude an item |
| [0027](0027-network-outages-are-waited-out.md) | 2026-09-27 | Network outages are waited out | A connectivity gate keeps outages away from the guards and alerts |
