# 0017. Ingest review: approvals stick, ledger saves merge, and a parse-rate floor

Date: 2026-09-08

## Context

Two problems with the structure sentinel's review flow surfaced with the
uploads of 2026-09-07.

**Approvals did not stick.** Two uploads an admin had approved stayed pending.
An ingest run loads the ingestion ledger at its start and saved it whole at
its end, so a review recorded while the run held the ledger in memory was
erased. And the sentinel re-evaluated an approved file from scratch, so it
could quarantine it again for the same findings.

**A warn verdict hid a near-total loss.** One upload lost 85,033 of its
85,933 rows. They sat in sections the TikTok parser excludes by design, but
were counted as `not_parseable`. The drift layer had too few accepted files
for that source to notice anything, the verdict was only a warning, and the
operator approved it in 97 s.

## Decision

- An admin's approval **sticks**: the sentinel reads the stored verdicts at
  the start of a run and does not re-quarantine an approved file for findings
  of a kind the approval covered (one learned file cannot move a 20-file
  baseline).
- `save_ledger` merges the run's changes into the stored ledger instead of
  overwriting it, so a review recorded mid-run is never erased.
- By-design exclusions are counted apart from parse failures: a parser that
  excludes records on purpose records them itself as `outside_whitelist`
  (`self._record_file_drops(counts, "outside_whitelist")`), and the base class
  subtracts them, so `not_parseable` is only the residue it could not read
  (2026-09-18).
- **Parse-rate floor**, independent of any baseline: a file whose parser kept
  under `PARSE_RATE_FLOOR` (10 %) of its ingestible rows (raw rows minus the
  file's `outside_whitelist` count) quarantines with code `parse_rate_floor`,
  even while the baseline is still learning (2026-09-18).
- The review modal shows "parser kept X of Y rows" before Approve.

## Consequences

- The same change added a shared-seconds floor to the donor merge
  (`min_shared_seconds`, three seconds), so a few-event capture that
  coincides with one second of a large export no longer merges with it.
- The sentinel and the ledger are documented in
  [pipeline.md](../pipeline.md#structure-sentinel).
- Withheld sections are the donor's choice and are never treated as drift on
  either baseline.
