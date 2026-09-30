"""App modals are built with the ``modal()`` macro and close with a real button.

The macro (``templates/_macros.html``) gives every ``.modal`` the same
backdrop, box and ``<button class="modal-close">``, which main.js clicks on
Escape. A ``<span>`` close control is not focusable or keyboard-operable.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "web_interface" / "templates"

# Modals written out by hand, and why.
HAND_WRITTEN = {
    "ace-modal": "the contract editor: no x or backdrop close, it holds unsaved work",
    "editCollectionModal": "its x sits in a sticky header bar (a .modal-close button all the same)",
}


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_span_close_controls():
    offenders = [
        str(p.relative_to(TEMPLATES))
        for p in _templates()
        if re.search(r'<span[^>]*class="[^"]*\bclose(-button)?\b', p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"use a <button> close control: {offenders}"


def test_modals_come_from_the_macro():
    literal = set()
    for p in _templates():
        literal |= set(
            re.findall(r'<div id="([\w-]+)" class="modal"', p.read_text(encoding="utf-8"))
        )
    assert literal == set(HAND_WRITTEN), f"build these with modal(): {literal - set(HAND_WRITTEN)}"


def test_hand_written_modals_that_close_have_a_modal_close_button():
    text = (TEMPLATES / "tabs" / "dm" / "edit_collections.html").read_text(encoding="utf-8")
    assert re.search(r'<button[^>]*class="close modal-close"', text)
