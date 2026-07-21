"""Persistent per-platform scraper alerts (``scraper_alerts.json`` in ``cache``).

Platforms change their sites/APIs frequently; when a scraping session starts
failing *systematically* (e.g. the permanent-storm guard in ``fyp.scrape``
detects a run of identical permanent verdicts for live posts) the problem is
the scraper or its session, not the queued items — and someone has to look at
it. This module owns the durable, cross-service record of that condition: the
task-runner (or a local drain) raises an alert, the web UI surfaces it on the
enrichment scraper cards and the System Health panel, and it clears either
automatically (a later batch succeeds) or manually (an admin dismisses it).

All writes go through ``data_io.update_json`` (compare-and-swap), so a worker
raising an alert and an admin dismissing another platform's alert can never
clobber each other. Every function is non-raising by design: alerting must
never break or block scraping itself.
"""

from datetime import UTC, datetime

import fyp.data_io as data_io
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

ALERTS_LOCATION = "cache"
ALERTS_FILENAME = "scraper_alerts.json"

# Alert kinds — one for now; the schema leaves room for future systematic
# failure modes (e.g. a repeated bot-wall verdict across runs).
KIND_PERMANENT_STORM = "permanent_storm"




def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()




def load_alerts() -> dict:
    """Return the active alerts as ``{platform: alert_dict}`` (never raises)."""
    try:
        # A missing file is the normal no-alerts state — check exists() first
        # so every status poll doesn't log a [DATA_IO] load error for it.
        if not data_io.exists(storage_location=ALERTS_LOCATION,
                              filename=ALERTS_FILENAME):
            return {}
        alerts = data_io.load_json(storage_location=ALERTS_LOCATION,
                                   filename=ALERTS_FILENAME)
        return alerts if isinstance(alerts, dict) else {}
    except Exception as e:
        logger.warning(f"Could not load scraper alerts: {e}")
        return {}




def raise_alert(platform: str, kind: str, category: str | None = None,
                count: int | None = None, message: str | None = None) -> None:
    """Raise (or refresh) a platform's alert. Never raises.

    A re-raise of the same kind keeps the original ``raised_at`` and bumps
    ``occurrences``/``last_seen`` so the UI can say "seen N times since ...".

    Args:
        platform: platform key, e.g. ``"instagram"``.
        kind: alert kind, e.g. ``KIND_PERMANENT_STORM``.
        category: the failure classification behind the alert
            (e.g. ``"permanent:removed"``).
        count: how many consecutive items hit the failure.
        message: human-readable explanation shown verbatim in the UI.
    """

    def _mutate(alerts):
        alerts = alerts if isinstance(alerts, dict) else {}
        previous = alerts.get(platform) or {}
        same_kind = previous.get("kind") == kind
        alerts[platform] = {
            "kind": kind,
            "category": category,
            "count": count,
            "message": message,
            "raised_at": previous.get("raised_at") if same_kind else None,
            "last_seen": _now_iso(),
            "occurrences": (previous.get("occurrences") or 0) + 1 if same_kind else 1,
        }
        if not alerts[platform]["raised_at"]:
            alerts[platform]["raised_at"] = alerts[platform]["last_seen"]
        return alerts

    try:
        data_io.update_json(storage_location=ALERTS_LOCATION,
                            filename=ALERTS_FILENAME, mutate=_mutate, default={})
        logger.warning(f"  [scrape] Raised scraper alert for '{platform}': "
                       f"{kind} ({category}) — visible in the web UI until the "
                       f"scraper succeeds again or an admin dismisses it.")
    except Exception as e:
        logger.warning(f"Could not raise scraper alert for '{platform}': {e}")




def clear_alert(platform: str, reason: str = "") -> None:
    """Clear a platform's alert if one is active. Never raises.

    Reads first and skips the write entirely when no alert is active, so the
    per-batch auto-clear path costs one JSON read, not a write.

    Args:
        platform: platform key whose alert to clear.
        reason: log-only note (e.g. ``"healthy batch"`` / ``"dismissed by admin"``).
    """
    try:
        if platform not in load_alerts():
            return

        def _mutate(alerts):
            alerts = alerts if isinstance(alerts, dict) else {}
            if platform not in alerts:
                return None  # already gone — skip the save
            alerts.pop(platform)
            return alerts

        data_io.update_json(storage_location=ALERTS_LOCATION,
                            filename=ALERTS_FILENAME, mutate=_mutate, default={})
        logger.info(f"  [scrape] Cleared scraper alert for '{platform}'"
                    f"{f' ({reason})' if reason else ''}.")
    except Exception as e:
        logger.warning(f"Could not clear scraper alert for '{platform}': {e}")
