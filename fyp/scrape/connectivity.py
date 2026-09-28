"""Is this machine online? A probe and a batch-wide gate for the scrapers.

Instagram and YouTube are drained from a residential IP — the local install,
i.e. a laptop — and a laptop's network drops. On 2026-09-27 it dropped three
times during one YouTube drain. The first two outages (~1 min each) passed; the
third (~6 min) produced 25 consecutive ``transient:network`` results, and the
transient-storm guard read that as a broken platform: it stopped the run after
6 of 20 batches and tried to raise a scraper alert — which, had the bucket
been reachable at that moment, would have held YouTube enrichment plans until
someone dismissed it.

An outage is the machine's problem, not the platform's or the items'. So a
failed item first asks :class:`ConnectivityGate` whether the machine is still
online. If it is, the failure stands and feeds the guards as before. If not,
the worker waits (one thread probes; the others queue behind it) and re-runs
the item once the connection is back — the outage never reaches the circuit
breaker, the storm guards, the retry budgets or the alert file. An outage that
outlasts the wait gives up cleanly: the batch aborts with the items queued and
uncharged, and no alert is raised.

The probe opens a TCP connection to port 443 — DNS resolution plus a
handshake, which is exactly what failed on 2026-09-27 (``[Errno 8] nodename
nor servname provided``) — on the platform's own host and the configured
``[misc] connectivity_probe_host``. Reaching any one of them means online: a
platform host that answers proves the scraper's failure was not the network.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Iterable

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

# Same default as fyp_config's startup probe, and the same config key.
_DEFAULT_PROBE_HOST = "connectivitycheck.gstatic.com"
_PROBE_TIMEOUT = 3.0
# How often a waiting gate re-probes.
_POLL_SECONDS = 15.0
# A local drain waits out an outage for this long by default; Cloud Run gets
# a short wait — its request deadline is 1800 s and a container that cannot
# resolve DNS is not a laptop on bad Wi-Fi.
_DEFAULT_MAX_WAIT_LOCAL = 1800
_DEFAULT_MAX_WAIT_CLOUD_RUN = 120

# Gate verdicts.
ONLINE = "online"          # the machine is online: the failure stands
RECOVERED = "recovered"    # it was offline and is back: re-run the item
GAVE_UP = "gave_up"        # offline past the wait (or the batch was stopped)




def probe_hosts(platform_host: str | None = None) -> tuple[str, ...]:
    """Return the hosts the probe tries, platform host first."""
    try:
        from fyp.fyp_config import fyp_cf
        configured = fyp_cf["misc"].get("connectivity_probe_host") or _DEFAULT_PROBE_HOST
    except Exception:
        configured = _DEFAULT_PROBE_HOST
    return tuple(h for h in (platform_host, configured) if h)




def probe_online(hosts: Iterable[str], timeout: float = _PROBE_TIMEOUT) -> bool:
    """Return True when a TCP connection to port 443 of any host succeeds."""
    for host in hosts:
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False




def offline_max_wait() -> int:
    """Seconds a batch waits out an outage (``[misc] scraper_offline_max_wait_seconds``)."""
    default = _DEFAULT_MAX_WAIT_CLOUD_RUN if os.environ.get("K_SERVICE") else _DEFAULT_MAX_WAIT_LOCAL
    try:
        from fyp.fyp_config import fyp_cf
        return int(fyp_cf["misc"].get("scraper_offline_max_wait_seconds", default))
    except Exception:
        return default




class ConnectivityGate:
    """Batch-wide arbiter between "this item failed" and "we are offline".

    Thread-safe. Workers call :meth:`check` after any failed item; while the
    machine is offline exactly one of them probes and waits, the rest block on
    the same lock and are released together when it comes back.

    Args:
        hosts: Hosts for :func:`probe_online`.
        max_wait: Seconds one outage may last before the gate gives up.
        poll: Seconds between probes while waiting (default ``_POLL_SECONDS``).
        probe: Injectable probe, ``probe(hosts) -> bool`` (tests).
    """

    def __init__(self, hosts: Iterable[str], max_wait: float,
                 poll: float | None = None,
                 probe: Callable[[tuple[str, ...]], bool] | None = None):
        self.hosts = tuple(hosts)
        self.max_wait = max_wait
        self.poll = _POLL_SECONDS if poll is None else poll
        self._probe = probe or probe_online
        self._lock = threading.Lock()
        self.gave_up = False
        self.outages = 0
        self.offline_seconds = 0.0
        # monotonic() when the last outage ended — an item started before it
        # may have failed on the outage even if the network is back by the
        # time that worker asks.
        self._last_outage_end: float | None = None
        # True while one worker waits an outage out. The network can come
        # back before that worker's next probe notices; a failure that meets
        # an online probe in that window still belongs to the outage.
        self._waiting = False

    def _online(self) -> bool:
        return self._probe(self.hosts)

    def check(self, stop_event: threading.Event | None = None,
              started_at: float | None = None) -> str:
        """Classify a failure as ordinary (online) or an outage; wait one out.

        Args:
            stop_event: The batch's abort event — a waiting gate returns
                ``GAVE_UP`` as soon as it is set (another guard or the batch
                deadline stopped the batch), without marking the gate gave-up.
            started_at: ``time.monotonic()`` when the failed item started. An
                outage that ended after it makes the failure suspect even when
                the network is back by now — ``RECOVERED``, not ``ONLINE``.

        Returns:
            ``ONLINE``, ``RECOVERED`` or ``GAVE_UP``.
        """
        if self.gave_up:
            return GAVE_UP
        if self._online():
            end = self._last_outage_end
            if self._waiting or (started_at is not None and end is not None
                                 and end >= started_at):
                return RECOVERED
            return ONLINE
        with self._lock:
            # Another worker may have waited the outage out (or given up)
            # while this one queued for the lock.
            if self.gave_up:
                return GAVE_UP
            if self._online():
                return RECOVERED
            if stop_event is not None and stop_event.is_set():
                return GAVE_UP
            self.outages += 1
            self._waiting = True
            started = time.monotonic()
            logger.warning(
                f"  [scrape] Network offline (no connection to {', '.join(self.hosts)}) — "
                f"pausing the batch until it returns (up to {int(self.max_wait)}s). Items "
                f"that failed meanwhile are re-run; nothing counts against the scraper.")
            try:
                while True:
                    elapsed = time.monotonic() - started
                    if elapsed >= self.max_wait:
                        self.gave_up = True
                        logger.warning(
                            f"  [scrape] Still offline after {int(elapsed)}s — giving up on "
                            f"this run. Unfinished items stay queued, uncharged; no scraper "
                            f"alert is raised.")
                        return GAVE_UP
                    if stop_event is not None:
                        if stop_event.wait(min(self.poll, self.max_wait - elapsed)):
                            return GAVE_UP
                    else:
                        time.sleep(min(self.poll, self.max_wait - elapsed))
                    if self._online():
                        self._last_outage_end = time.monotonic()
                        logger.info(
                            f"  [scrape] Network back after {int(time.monotonic() - started)}s "
                            f"— resuming; re-running the items that failed while offline.")
                        return RECOVERED
            finally:
                self._waiting = False
                self.offline_seconds += time.monotonic() - started

    def hold_until_online(self, log_every: float = 300.0) -> None:
        """Block, without a limit, until the machine is online.

        For the batch's final writes: once the scraping is done (or given up)
        its rows and the queue update still need the bucket, and a write
        attempted offline times out and fails the run with the rows unsaved.
        Returns at once when already online.
        """
        if self._online():
            return
        started = last_log = time.monotonic()
        logger.warning("  [scrape] Offline with this batch's results unsaved — holding them "
                       "until the connection returns.")
        while not self._online():
            time.sleep(self.poll)
            now = time.monotonic()
            if now - last_log >= log_every:
                logger.info(f"  [scrape] Still offline ({int(now - started)}s) — holding the "
                            f"batch's results.")
                last_log = now
        logger.info(f"  [scrape] Network back after {int(time.monotonic() - started)}s — "
                    f"saving the batch.")
