"""Content-hashed URLs for the app's static scripts and stylesheets.

Templates reference JS and CSS through ``asset_url('main.js')`` rather than
``url_for('static', ...)`` plus a hand-maintained ``?v=N``. The query string is
a short hash of the file's bytes, so a changed file gets a new URL (browsers
refetch it) and an unchanged one keeps its cached copy — with nothing to
remember to bump. Guard: ``tests/unit/test_static_asset_urls.py``.

Because the URL changes whenever the bytes do, a response whose ``v`` matches
the file it serves can be cached as immutable (:func:`cache_hashed_static`):
the browser then reuses it without even a revalidation request.
"""

import hashlib
import logging
import os

from flask import current_app, request, url_for

logger = logging.getLogger(__name__)

# path -> ((mtime_ns, size), digest). Re-hashed only when the file changes, so
# an edited file busts the cache in local dev without a server restart.
_DIGESTS: dict[str, tuple[tuple[int, int], str]] = {}


# A year: the longest max-age browsers honour, for URLs whose bytes never change.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def content_hash(filename: str) -> str | None:
    """The short content hash of static file ``filename``, or None if it can't be read."""
    path = os.path.join(current_app.static_folder, filename)
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
        cached = _DIGESTS.get(path)
        if cached is None or cached[0] != stamp:
            with open(path, "rb") as fh:
                cached = (stamp, hashlib.sha256(fh.read()).hexdigest()[:10])
            _DIGESTS[path] = cached
    except OSError:
        return None
    return cached[1]


def asset_url(filename: str) -> str:
    """Return the static URL for ``filename`` with a ``v=<content hash>`` query."""
    digest = content_hash(filename)
    if digest is None:
        # A missing asset must not take the page down with it; the browser
        # reports the 404 for that one file.
        logger.warning("asset_url: cannot read static file %s", filename)
        return url_for("static", filename=filename)
    return url_for("static", filename=filename, v=digest)


def cache_hashed_static(response):
    """Mark a static response immutable when its ``v`` names the bytes it carries.

    Only a ``v`` equal to the served file's current hash qualifies. During a
    deploy a request for the new hash can reach an instance still serving the
    old file; caching that answer for a year would pin the stale bytes, so it
    keeps Flask's default revalidation instead. Static URLs without ``v``
    (``url_for('static', ...)`` images) are left alone too.

    Args:
        response: The outgoing response (an ``after_request`` hook).

    Returns:
        The same response, its ``Cache-Control`` replaced where it qualifies.
    """
    if request.endpoint != "static" or response.status_code not in (200, 304):
        return response
    v = request.args.get("v")
    filename = (request.view_args or {}).get("filename")
    if v and filename and v == content_hash(filename):
        response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
    return response
