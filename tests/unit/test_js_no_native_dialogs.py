"""App scripts use the app's own dialogs, never the browser's alert/confirm.

``showAppAlert`` / ``showAppConfirm`` (main.js) match the Hub's styling and
stack above its modals; the browser's native boxes block the page and look
foreign. main.js keeps them only as the fallback when the dialog markup is
missing. ``prompt()`` has no app equivalent yet, so it is not covered.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "web_interface" / "static"
_NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(alert|confirm)\s*\(")
_ALLOWED = {"main.js"}  # _showAppDialog's fallback


def _code_lines(text: str):
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if not stripped.startswith(("//", "*", "/*")):
            yield n, line


def test_no_native_alert_or_confirm():
    offenders = []
    for path in sorted(STATIC.rglob("*.js")):
        rel = path.relative_to(STATIC).as_posix()
        if "vendor" in path.parts or rel in _ALLOWED:
            continue
        for n, line in _code_lines(path.read_text(encoding="utf-8")):
            if _NATIVE.search(line):
                offenders.append(f"{rel}:{n}")
    assert offenders == [], "use showAppAlert/showAppConfirm: " + ", ".join(offenders)
