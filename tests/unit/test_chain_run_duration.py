"""last_run_duration must span a whole self-chained run, not the final link.

2026-08-16: prod timelines_refresh reported "last run took 5m35s" for a run
that took 40+ minutes — each chain link measured its own boot-to-finish time,
and the final link's stats write overwrote the truth. The status file's
``start_time`` (written by link 0, preserved across links by ``resume()``) is
the run's real origin; ``_chain_run_start`` prefers it.
"""

from datetime import UTC, datetime, timedelta

from web_interface.routes.process_routes import _chain_run_start




def test_prefers_the_older_status_start_time():
    """The final chain link measures from link 0's start, not its own boot."""
    link_start = datetime(2026, 8, 16, 12, 40, tzinfo=UTC)
    chain_start = "2026-08-16T12:00:00+00:00"
    assert _chain_run_start(link_start, chain_start) == datetime(
        2026, 8, 16, 12, 0, tzinfo=UTC
    )




def test_unchained_run_keeps_local_start():
    """A status start_time written moments before (start()) must not win by
    more than clock skew — min() keeps whichever is earlier, so a plain
    single-link run is unaffected."""
    link_start = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    status_start = (link_start + timedelta(seconds=3)).isoformat()
    assert _chain_run_start(link_start, status_start) == link_start




def test_missing_or_bad_status_start_falls_back():
    link_start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert _chain_run_start(link_start, None) == link_start
    assert _chain_run_start(link_start, "") == link_start
    assert _chain_run_start(link_start, "not-a-timestamp") == link_start




def test_naive_status_start_is_tolerated():
    """A legacy naive timestamp can't be compared to an aware one — the
    TypeError path must fall back instead of crashing the stats write."""
    link_start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert _chain_run_start(link_start, "2026-08-16T11:00:00") == link_start
