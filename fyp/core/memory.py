"""Process-memory instrumentation shared by the corpus-scale workers.

Cloud Run enforces container memory via cgroups; process RSS is a good proxy
for what the container reports against the task runner's memory limit. Current
RSS comes from psutil (cross-platform, bytes); the high-water mark comes from
``getrusage`` (KB on Linux, bytes on macOS). Windows has no ``resource``
module, so there the watermark comes from psutil's Windows-only ``peak_wset``
field instead.

The :func:`mem_probe` context manager wraps one phase of a worker and emits a
single ``[<TAG>][MEM]`` log line on exit, in the same format the merge hot
path established (``[RECODE][MEM]`` / ``[ENRICH PATCH][MEM]`` in
``fyp.organize_datasets``) — so every worker's memory telemetry greps the
same way.
"""

import sys
from collections.abc import Callable
from contextlib import contextmanager

import psutil

try:
    import resource as _resource
except ImportError:  # Windows
    _resource = None

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

_MEM_PROCESS = psutil.Process()

# getrusage reports ru_maxrss in bytes on macOS but kilobytes on Linux.
_RU_MAXRSS_DIVISOR_TO_MB = 1024 * 1024 if sys.platform == "darwin" else 1024






def rss_mb() -> float:
    """Current process resident-set size in MB."""
    return _MEM_PROCESS.memory_info().rss / (1024 * 1024)






def peak_rss_mb() -> float:
    """High-water-mark RSS of this process since startup, in MB."""
    if _resource is not None:
        return (
            _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
            / _RU_MAXRSS_DIVISOR_TO_MB
        )
    # Windows: `resource` is unavailable; psutil exposes the peak working set.
    mem = _MEM_PROCESS.memory_info()
    peak_bytes = getattr(mem, "peak_wset", None) or mem.rss
    return peak_bytes / (1024 * 1024)






def df_size_mb(df) -> float:
    """Return a DataFrame's deep memory usage in megabytes."""
    return df.memory_usage(deep=True).sum() / (1024**2)






@contextmanager
def mem_probe(tag: str, phase: str, log: Callable[[str], None] | None = None,
              **extra):
    """Measure one phase's memory footprint and log a ``[<TAG>][MEM]`` line.

    Args:
        tag: Worker tag for the log line (e.g. ``"SESSIONS"``, ``"PCA"``).
        phase: Short phase name (e.g. ``"load_plays"``).
        log: Log callable (e.g. a status reporter's ``.log``); defaults to
            this module's logger.
    Keyword Args:
        **extra: Additional ``key=value`` fields appended to the log line
            (e.g. ``chunk=3``, ``collections=8``).

    Example::

        with mem_probe("SESSIONS", "load_plays", log=reporter.log, chunk=2):
            plays = load_plays(batch)
    """
    emit = log if log is not None else logger.info
    rss_start = rss_mb()
    peak_start = peak_rss_mb()
    try:
        yield
    finally:
        rss_end = rss_mb()
        peak_end = peak_rss_mb()
        suffix = "".join(f" {k}={v}" for k, v in extra.items())
        emit(
            f"[{tag}][MEM] phase={phase} "
            f"rss_start={rss_start:.0f}MB rss_end={rss_end:.0f}MB "
            f"peak_during={peak_end:.0f}MB peak_delta=+{peak_end - peak_start:.0f}MB"
            f"{suffix}"
        )
