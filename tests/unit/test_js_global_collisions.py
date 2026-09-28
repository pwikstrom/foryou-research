"""No two static JS files may define the same top-level global.

The app's scripts are classic scripts sharing one global scope. A function
defined in two files silently resolves to whichever file loaded last, and which
files load depends on the viewer's permissions (``templates/index.html`` gates
several scripts on ``user.can_access``). A duplicate top-level ``let`` or
``const`` is worse: the second script fails to load at all. Shared helpers
belong in ``static/js/core/``, defined once.
"""

import re
from collections import defaultdict
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "web_interface" / "static"

# Column-0 declarations only: nested functions and block-scoped bindings are
# indented and therefore not globals.
_DECL = re.compile(
    r"^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("
    r"|^(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*="
)


def _app_scripts():
    for path in sorted(STATIC.rglob("*.js")):
        if "vendor" in path.parts or path.name.endswith(".min.js"):
            continue
        yield path


def test_no_top_level_global_is_defined_in_two_files():
    owners = defaultdict(set)
    for path in _app_scripts():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _DECL.match(line)
            if m:
                owners[m.group(1) or m.group(2)].add(str(path.relative_to(STATIC)))
    clashes = {name: sorted(files) for name, files in owners.items() if len(files) > 1}
    assert not clashes, f"top-level globals defined in more than one file: {clashes}"


def test_shared_helpers_live_in_core():
    core = (STATIC / "js" / "core" / "dom_utils.js").read_text(encoding="utf-8")
    for name in ("escapeHtml", "showToast"):
        assert re.search(rf"^function {name}\(", core, re.M), f"{name} missing from dom_utils.js"


def test_only_the_fetch_wrapper_sets_the_csrf_header():
    """main.js wraps window.fetch and adds X-CSRFToken to every write request.

    A hand-set header is redundant, and has hidden a cross-file dependency
    before (main.js reading a csrfToken global declared by another script).
    """
    offenders = [
        str(p.relative_to(STATIC))
        for p in _app_scripts()
        if p.name != "main.js" and "X-CSRFToken" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"set X-CSRFToken via the main.js fetch wrapper, not by hand: {offenders}"
