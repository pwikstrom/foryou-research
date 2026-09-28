"""Content-hashed URLs for the app's static scripts and stylesheets.

Templates reference JS and CSS through ``asset_url('main.js')`` rather than
``url_for('static', ...)`` plus a hand-maintained ``?v=N``. The query string is
a short hash of the file's bytes, so a changed file gets a new URL (browsers
refetch it) and an unchanged one keeps its cached copy — with nothing to
remember to bump. Guard: ``tests/unit/test_static_asset_urls.py``.
"""

import hashlib
import logging
import os

from flask import current_app, url_for

logger = logging.getLogger(__name__)

# path -> ((mtime_ns, size), digest). Re-hashed only when the file changes, so
# an edited file busts the cache in local dev without a server restart.
_DIGESTS: dict[str, tuple[tuple[int, int], str]] = {}


def asset_url(filename: str) -> str:
    """Return the static URL for ``filename`` with a ``v=<content hash>`` query."""
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
        # A missing asset must not take the page down with it; the browser
        # reports the 404 for that one file.
        logger.warning("asset_url: cannot read static file %s", filename)
        return url_for("static", filename=filename)
    return url_for("static", filename=filename, v=cached[1])
