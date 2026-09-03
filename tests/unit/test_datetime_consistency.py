"""Guards for the UI's date-time convention.

The web layer distinguishes two kinds of timestamp:

* **Instants** — a moment in system time (a task ran, a user logged in). These
  travel as offset-aware ISO-8601 and are rendered in the viewer's timezone by
  ``static/js/datetime_format.js``.
* **Wall-clock stamps** — participant activity times, already expressed on the
  donor's clock. These travel zone-less and are rendered verbatim.

The rule only holds while the server keeps producing offset-aware values for
instants, so these tests fail loudly if a naive ``datetime.now()`` or
``fromtimestamp()`` creeps back into a path that reaches the browser.
"""

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB = PROJECT_ROOT / "web_interface"
STATIC = WEB / "static"

# Naive ``datetime.now()`` is legitimate for measuring elapsed time and for
# building opaque ids; it is only a problem when the value is serialised for
# display. These are the audited exceptions.
NAIVE_NOW_ALLOWED = {
    "explorer_backend.py",        # elapsed-time deltas in progress logging
    "run_queue_annotator_batch.py",  # batch-id digits + a config-TZ log clock
}

# fyp/_archive/ is gitignored, locally-retained dead code — never imported and
# never deployed, so it is not held to the shipped-code rule. It exists only in
# a full checkout, which is why a worktree does not see it.
SKIP_DIRS = {"__pycache__", "_archive"}


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if not SKIP_DIRS.intersection(p.parts))






def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]






def _func_name(call: ast.Call) -> str:
    """Dotted name of the callee, e.g. ``datetime.datetime.now``."""
    parts: list[str] = []
    node: ast.expr = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))






def test_no_naive_now_in_web_layer():
    """``datetime.now()`` without a timezone must not reach the API boundary."""
    offenders: list[str] = []
    for path in _py_files(WEB):
        if path.name in NAIVE_NOW_ALLOWED:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for call in _calls(tree):
            name = _func_name(call)
            if name.split(".")[-1] == "utcnow":
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{call.lineno} utcnow()")
            elif name.endswith("datetime.now") or name == "now":
                if not call.args and not call.keywords:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{call.lineno} {name}()")
    assert not offenders, (
        "Naive datetime.now()/utcnow() in the web layer — the browser reads a "
        "zone-less string as participant wall-clock and will not convert it:\n  "
        + "\n  ".join(offenders)
    )






def test_fromtimestamp_always_passes_a_timezone():
    """An epoch is UTC; parsing it without ``tz=`` yields the machine's local time."""
    offenders: list[str] = []
    for root in (WEB, PROJECT_ROOT / "fyp"):
        for path in _py_files(root):
            tree = ast.parse(path.read_text(), filename=str(path))
            for call in _calls(tree):
                if _func_name(call).split(".")[-1] != "fromtimestamp":
                    continue
                has_tz = any(kw.arg == "tz" for kw in call.keywords) or len(call.args) > 1
                if not has_tz:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{call.lineno}")
    assert not offenders, (
        "datetime.fromtimestamp() without tz= returns the *server's* local time, "
        "so the same epoch resolves differently on Cloud Run than on a laptop:\n  "
        + "\n  ".join(offenders)
    )






@pytest.mark.parametrize(
    "helper",
    [
        "fypParseInstant", "fypIsWallClock", "fypFmtDateTime", "fypFmtDateTimeShort",
        "fypFmtDate", "fypFmtDateShort", "fypFmtDateTimeFull", "fypFmtTime", "fypFmtRelative",
        "fypFmtAuto", "fypTimeZoneLabel", "fypWallDateTime", "fypWallDate",
        "fypWallDateShort", "fypWallIsoDate",
    ],
)
def test_datetime_helper_is_exported(helper):
    """Every helper the tabs call has to be on ``window``; there is no bundler."""
    source = (STATIC / "js" / "datetime_format.js").read_text()
    assert f"global.{helper} = {helper};" in source, f"{helper} is not exported"






def test_datetime_helper_loads_before_every_tab_script():
    """It lives in <head> of the base layout, so deferred tab scripts can use it."""
    base = (WEB / "templates" / "base.html").read_text()
    assert "js/datetime_format.js" in base, "helper not included in base.html"
    head_end = base.index("</head>")
    assert base.index("js/datetime_format.js") < head_end, (
        "datetime_format.js must load in <head> — tab templates emit non-deferred "
        "scripts in <body> that run before the {% block scripts %} bundle."
    )






def test_no_adhoc_datetime_formatting_in_tab_scripts():
    """Tab scripts format through the shared helper, not their own ``new Date``."""
    banned = re.compile(r"\.toLocale(Date|Time)String\(|new Date\([^)]*\)\.toISOString\(\)")
    offenders: list[str] = []
    for path in sorted(STATIC.rglob("*.js")):
        if path.name in {"datetime_format.js"} or "vendor" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            # A Date built from a millisecond delta is a duration, not an instant.
            if "dayMs" in line or "diff" in line:
                continue
            if banned.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno} {line.strip()}")
    assert not offenders, (
        "Ad-hoc date formatting — use the fypFmt*/fypWall* helpers so every "
        "surface agrees on the timezone:\n  " + "\n  ".join(offenders)
    )
