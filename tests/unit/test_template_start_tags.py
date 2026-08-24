"""Guard against unterminated HTML start tags in the Jinja templates.

Commit a8e10e5d shipped ``tabs/my_stuff.html`` with a script tag whose ``>``
was missing (``<script src="...my_stuff_tab.js?v=3"</script>``). The HTML
parser read ``<`` and ``/script`` as attribute names, never closed the element,
and swallowed every include that follows ``my_stuff.html`` in ``index.html``
— the ``data_management`` pane, the study modal and the ``admin`` pane — as
~118 KB of inert script text. The Data Pipeline and Admin tabs were dead in
production for a day: the server sent correct HTML and the nav buttons
rendered, but the panes never existed in the DOM, so ``openTab()`` had nothing
to show. No console error, no server error, no failing test. Fixed in 3796fe2a.

The scan below walks every template character-by-character, quote-aware, and
flags any start tag that hits the next ``<`` before its own ``>``. It is
deliberately not a regex: the obvious pattern
``<([a-zA-Z][a-zA-Z0-9-]*)((?:[^<>]|"[^"]*"|'[^']*')*)(?=<)`` backtracks
catastrophically and hangs for minutes on these files.
"""

import pathlib

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[2] / "web_interface" / "templates"

# Elements whose content the HTML parser does not scan for tags. Skipping them
# keeps JavaScript comparisons (`for (let i = 0; i<n; i++)`) from looking like
# start tags. A malformed start tag is caught *before* the skip begins, so the
# a8e10e5d shape is still reported.
RAW_TEXT_ELEMENTS = {"script", "style", "textarea", "title"}

JINJA_CLOSERS = {"{": "}}", "%": "%}", "#": "#}"}

_NAME_CHARS = "-_:."


def _skip_jinja(text: str, i: int) -> int:
    """Index just past the ``{{ }}`` / ``{% %}`` / ``{# #}`` block at ``i``."""
    closer = JINJA_CLOSERS[text[i + 1]]
    end = text.find(closer, i + 2)
    return len(text) if end < 0 else end + len(closer)


def _at_jinja(text: str, i: int) -> bool:
    return text[i] == "{" and i + 1 < len(text) and text[i + 1] in JINJA_CLOSERS


def unterminated_start_tags(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, tag name)`` for every tag missing its ``>``."""
    lowered = text.lower()
    n = len(text)
    findings: list[tuple[int, str]] = []
    i = 0

    while i < n:
        if _at_jinja(text, i):
            i = _skip_jinja(text, i)
            continue
        if text[i] != "<":
            i += 1
            continue
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            i = n if end < 0 else end + 3
            continue

        # `<` starts a tag only when followed by a letter, or by `/` + letter.
        j = i + 1
        is_end_tag = j < n and text[j] == "/"
        if is_end_tag:
            j += 1
        if j >= n or not (text[j].isascii() and text[j].isalpha()):
            i += 1
            continue

        name_start = j
        while j < n and ((text[j].isascii() and text[j].isalnum()) or text[j] in _NAME_CHARS):
            j += 1
        name = lowered[name_start:j]

        # Walk the attributes, tracking quoting, until the tag closes.
        quote = ""
        terminated = False
        while j < n:
            ch = text[j]
            if quote:
                if ch == quote:
                    quote = ""
                j += 1
            elif ch in "\"'":
                quote = ch
                j += 1
            elif _at_jinja(text, j):
                # `{% if x > y %}` inside a tag is not the tag's own `>`.
                j = _skip_jinja(text, j)
            elif ch == ">":
                terminated = True
                j += 1
                break
            elif ch == "<":
                break  # the next tag started before this one closed
            else:
                j += 1

        if not terminated:
            findings.append((text.count("\n", 0, i) + 1, name))
            i = max(j, i + 1)  # resume at the `<` that gave it away
            continue

        i = j
        if not is_end_tag and name in RAW_TEXT_ELEMENTS and text[j - 2] != "/":
            end = lowered.find(f"</{name}", i)
            # An unclosed raw-text element eats the rest of the document, which
            # is exactly what the browser does with it.
            i = n if end < 0 else end

    return findings


def _template_files() -> list[pathlib.Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_the_scan_actually_reaches_the_templates():
    """A wrong path would make the guard below pass by scanning nothing."""
    assert TEMPLATES.is_dir(), TEMPLATES
    assert len(_template_files()) > 20


@pytest.mark.parametrize("template", _template_files(), ids=lambda p: p.name)
def test_every_start_tag_is_terminated(template):
    text = template.read_text(encoding="utf-8")
    findings = unterminated_start_tags(text)
    assert not findings, "\n".join(
        f"{template}:{line}: <{name}> is missing its '>'" for line, name in findings
    )


def test_detector_catches_the_a8e10e5d_shape():
    """The regression verbatim: a script tag whose start tag never closes.

    Run against ``git show a8e10e5d:web_interface/templates/tabs/my_stuff.html``
    this reports line 323 — the tag that killed the two tabs.
    """
    broken = (
        "<script src=\"{{ url_for('static', filename='js/my_stuff_tab.js') }}?v=3\""
        "</script>\n<div id=\"admin\"></div>"
    )
    assert unterminated_start_tags(broken) == [(1, "script")]


def test_detector_reports_the_line_of_a_broken_tag_mid_file():
    broken = '<div>\n  <p>ok</p>\n  <span class="x"\n</div>'
    assert unterminated_start_tags(broken) == [(3, "span")]


@pytest.mark.parametrize(
    "sound",
    [
        '<div class="a > b">text</div>',
        "<input value='a > b'>",
        '<script>for (let i = 0; i<n; i++) { a(i); }</script>',
        '<script src="/static/js/x.js?v=3"></script>',
        "<style>.a{content:'<'}</style>",
        "<option {% if a > b %}selected{% endif %}>x</option>",
        '<div data-x="{{ a if a > b else c }}"></div>',
        "<!-- <div class=unterminated -->",
        "<!DOCTYPE html>",
        "<br/><img src='x'/>",
        "<p>1 < 2 and 3 > 2</p>",
        "{# <span class=commented #}",
    ],
)
def test_sound_markup_is_not_flagged(sound):
    assert unterminated_start_tags(sound) == []
