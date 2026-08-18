"""Cross-instance lease for local scrape-queue drains.

A local drain (``FYP_FORCE_GCS=1 python web_interface/run_queue_scraper.py``,
see the runbook in DEVELOPING.md) writes to the same GCS storage as the Cloud Run
services, but its process is invisible to them: the web service's in-memory
process table can't see a laptop subprocess, and the laptop consults no GCS
status. This module closes that gap with a small heartbeat lease file on the
shared storage: the drain holds ``local_drain_<platform>.json`` (location
``"cache"``) while it runs, and ``process_manager.start_process`` refuses to
start the same platform's scraper — or a consolidation — while a fresh lease
exists, telling the researcher who is holding it instead of silently racing.

A lease whose heartbeat is older than ``LEASE_STALE_S`` is ignored (laptop
slept or the drain was killed), mirroring the 600 s task-status convention.
In pure-local dev (no shared bucket) the lease is invisible to Cloud Run by
construction and merely inert.
"""

import getpass
import os
import socket
import threading
from datetime import UTC, datetime

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

LEASE_LOCATION = "cache"
LEASE_STALE_S = 600
HEARTBEAT_INTERVAL_S = 30


def _data_io():
    """Lazy fyp.data_io accessor (keeps import light for worker __main__)."""
    import fyp.data_io as data_io

    return data_io






def lease_filename(platform: str) -> str:
    """Return the lease filename for one platform's drain."""
    return f"local_drain_{platform}.json"






def read_drain_lease(platform: str) -> dict | None:
    """Return the platform's drain lease if present and fresh, else ``None``.

    Args:
        platform: Platform whose drain lease to read.

    Returns:
        The lease dict (``platform``/``host``/``user``/``pid``/``started_at``/
        ``heartbeat_at``) when its heartbeat is within ``LEASE_STALE_S``,
        otherwise ``None`` (missing, unreadable, or stale).
    """
    data_io = _data_io()
    try:
        if not data_io.exists(storage_location=LEASE_LOCATION, filename=lease_filename(platform)):
            return None
        lease = data_io.load_json(storage_location=LEASE_LOCATION, filename=lease_filename(platform))
    except Exception:
        return None
    if not isinstance(lease, dict):
        return None
    try:
        heartbeat = datetime.fromisoformat(lease.get("heartbeat_at", ""))
        age = (datetime.now(UTC) - heartbeat).total_seconds()
    except (ValueError, TypeError):
        return None
    if age > LEASE_STALE_S:
        return None
    return lease






def active_drain_leases() -> dict[str, dict]:
    """Return ``{platform: lease}`` for every registered platform with a fresh lease."""
    import fyp.scrape_queues as scrape_queues

    leases = {}
    for platform in scrape_queues.registered_platforms():
        lease = read_drain_lease(platform)
        if lease:
            leases[platform] = lease
    return leases






def describe_lease(lease: dict) -> str:
    """One-line human description of who holds a lease, for block messages."""
    holder = lease.get("user") or "someone"
    host = lease.get("host") or "another machine"
    started = lease.get("started_at", "")[:16].replace("T", " ")
    return f"a local {lease.get('platform', '')} drain by {holder}@{host} (running since {started} UTC)"






class DrainLease:
    """Holds a heartbeat lease for one platform's local drain.

    Use as a context manager around the drain loop::

        with DrainLease(platform):
            queue_scraper_loop(...)

    Acquiring writes the lease file and starts a daemon heartbeat thread
    (every ``HEARTBEAT_INTERVAL_S``); exiting stops the thread and removes
    the file. Lease writes are best-effort — a storage hiccup never aborts
    the drain itself.
    """

    def __init__(self, platform: str):
        self.platform = platform
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _write(self, started_at: str) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            _data_io().save_json(
                data={
                    "platform": self.platform,
                    "host": socket.gethostname(),
                    "user": getpass.getuser(),
                    "pid": os.getpid(),
                    "started_at": started_at,
                    "heartbeat_at": now,
                },
                storage_location=LEASE_LOCATION,
                filename=lease_filename(self.platform),
            )
        except Exception as exc:
            logger.warning(f"Drain lease write failed (continuing): {exc}")

    def _heartbeat_loop(self, started_at: str) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            self._write(started_at)

    def __enter__(self) -> "DrainLease":
        existing = read_drain_lease(self.platform)
        if existing and existing.get("pid") != os.getpid():
            logger.warning(
                f"Another drain lease is active: {describe_lease(existing)}. "
                f"Taking it over — make sure that drain is really finished."
            )
        started_at = datetime.now(UTC).isoformat()
        self._write(started_at)
        self._thread = threading.Thread(
            target=self._heartbeat_loop, args=(started_at,), daemon=True
        )
        self._thread.start()
        logger.info(f"Drain lease acquired for '{self.platform}' "
                    f"(heartbeat every {HEARTBEAT_INTERVAL_S}s).")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        data_io = _data_io()
        try:
            if data_io.exists(storage_location=LEASE_LOCATION, filename=lease_filename(self.platform)):
                data_io.remove(storage_location=LEASE_LOCATION, filename=lease_filename(self.platform))
            logger.info(f"Drain lease released for '{self.platform}'.")
        except Exception as exc_release:
            logger.warning(f"Drain lease release failed: {exc_release}")
