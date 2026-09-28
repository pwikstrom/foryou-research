# 0027. Network outages are waited out, never counted as item failures

Date: 2026-09-27

## Context

Instagram and YouTube queues are drained from a laptop on a residential
connection, and a laptop's network drops. On 2026-09-27 a ~6-minute Wi-Fi
outage produced 25 consecutive `transient:network` results. The
transient-storm guard stopped the run at batch 6 of 20 and tried to raise a
scraper alert — which holds the platform's enrichment plans until an admin
dismisses it. Only the fact that the bucket was unreachable too kept the
alert from landing.

## Decision

Every failed item, on either leg, first asks a connectivity gate
(`fyp/scrape/connectivity.py`, `ConnectivityGate`): a TCP probe to port 443 of
the platform host and of `[misc] connectivity_probe_host`.

- **Online:** the failure stands and feeds the guards as before.
- **Offline:** one worker waits (the rest queue on the gate's lock), then
  every item that failed during the outage is re-run (at most
  `_OFFLINE_RERUNS` times per item). An item whose failure meets an online
  probe while the outage is still being waited out, or that started before
  the outage ended, counts as the outage's.
- Outage failures never reach the throttle, the circuit breaker, the storm
  guards, the retry budgets or the alert file.
- Past `[misc] scraper_offline_max_wait_seconds` (1800 s locally, 120 s on
  Cloud Run) the gate gives up: the batch aborts with an `offline` attr, the
  loop or chain stops, nothing is charged, and no alert is raised or
  cleared.
- Before its bucket writes, a batch that saw an outage holds (unbounded,
  local only) until the network is back, so its rows are not lost to a write
  timeout.

## Consequences

- `tests/unit/conftest.py` pins the probe online for every unit test.
- Documented in [pipeline.md](../pipeline.md#2-scraping-fypscrape).
