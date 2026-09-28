# 0026. Only final scrape failures count as failed for good

Date: 2026-09-25

## Context

The failed-scrapes record stores each item's failure category so that storms
are diagnosable after the fact, and it records retryable failures too (a
timeout, a storm-aborted batch). Every recorded id was nevertheless treated as
failed: `scrape_fail` in `enrichment_status.parquet`, the enrichment plan's
skip list and the coverage bar's "failed for good" all read the whole
record. A timed-out item was therefore skipped by the enrichment plan for
good.

## Decision

Only an item whose **latest** record is final — `permanent:*`, or a legacy
bare id — counts as failed. `load_failed_scrapes()` applies the filter, and
it is what `scrape_fail`, the plan's skip and the coverage bar read.

## Consequences

- Retryable failures stay in the record for diagnosis without excluding the
  item from later plans.
- Documented in [pipeline.md](../pipeline.md#scraper-alerts-and-the-failed-scrapes-record).
