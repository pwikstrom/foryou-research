"""Unexpected-error reporting for request handlers.

An unexpected exception is logged once, with its traceback, under a short
reference id; the client sees only that id, never the exception text (which
can carry file paths, bucket names or data). The app-wide handler in
``fyp_data_hub.py`` does this for anything a route lets escape; a handler
that must answer with its own response shape calls :func:`log_unexpected`.
"""

import uuid

from fyp.core.logging_setup import get_logger

_logger = get_logger("web_interface.errors")


def log_unexpected(error: BaseException, context: str) -> str:
    """Log ``error`` with its traceback and return the reference id shown to the client.

    Args:
        error: The exception being handled.
        context: Where it happened, e.g. ``"on POST /api/manage/..."``.

    Returns:
        An 8-character reference id, also written in the log line.
    """
    ref = uuid.uuid4().hex[:8]
    _logger.error(f"Unhandled error (ref {ref}) {context}", exc_info=error)
    return ref


def internal_error_message(ref: str) -> str:
    """The client-facing text for an unexpected error with reference ``ref``."""
    return f"Internal error (ref {ref})"
