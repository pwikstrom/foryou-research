# 0021. The donor's time zone is authoritative; `tz_offset` is fractional hours

Date: 2026-09-17

## Context

An upload may carry the donor's time zone (an IANA name or a fixed offset),
collected in the upload modal because export labels can be ambiguous (e.g.
IST). Two defects surfaced in September 2026:

- **TikTok ignored it.** The TikTok DDP parser called
  `infer_timezone_offset` directly and silently ignored a supplied zone; only
  the Instagram and YouTube parsers honoured it. Fixed 2026-09-17.
- **The contract and the data disagreed.** The activity contract declared
  `tz_offset` as `int64` while the pipeline stored doubles (zones such as
  `+05:30` or Adelaide's +9.5 are fractional). Corrected 2026-09-25.

## Decision

- A supplied zone is the authoritative source for local-time conversion on
  every platform, overriding the export's own label. All three DDP parsers
  reach it through `ForYouBaseCollection._finalize_activity_frame()`.
- `tz_offset` is fractional hours (`double[pyarrow]`): `+05:30` → 5.5,
  Adelaide → 9.5; per row from a supplied zone (daylight saving applied), or
  once per file when inferred.

## Consequences

- `tests/unit/test_tiktok_ddp_parser.py` pins the TikTok fix.
- The contract correction minted a new `acv_` activity-contract version and
  changed the var_schema hash, which fully rebuilds cached studies once.
- Documented in [pipeline.md](../pipeline.md#donor-time-zone).
