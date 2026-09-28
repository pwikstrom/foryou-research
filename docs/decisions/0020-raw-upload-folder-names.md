# 0020. Raw-upload folders keep their historical names

Date: 2026-09-16

## Context

Raw uploads land in one folder per ingestion class under `activity_data/`,
and the names follow two conventions:

- `ddp/ddp_raw`, `aio/aio_raw`, `zeeschuimer/zeeschuimer_raw` — all TikTok,
  keyed by *source*. They were named in November 2025, when TikTok was the
  only platform and "ddp" meant "TikTok data-download export".
- `instagram/instagram_raw`, `youtube/youtube_raw` — keyed by *platform*,
  from the July 2026 convention under which an ingestion class self-registers
  `activity_data/{source_platform}/{raw_path}`.

The TikTok keys are static entries in `fyp/core/fyp_config.py`, and
`data_io.register_location()` never overrides an existing key, so the newer
convention does not apply to them. An audit of the bucket layout on
2026-09-16 asked whether to rename them.

## Decision

Keep the names. A rename is not a config change: nothing below
`paths.local_data` is configurable. It would be a data migration touching
the ledger, withdrawal and sentinel `raw_path` keys,
`data_io.APPEND_ONLY_LOCATIONS`, the tests, and the stored bucket objects.
The inconsistency is documented instead.

## Consequences

- The mapping of ingestion class to folder is documented in
  [configuration.md](../configuration.md#storage-locations).
- A new platform follows the platform-keyed convention automatically.
