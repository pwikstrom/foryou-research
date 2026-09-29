"""The Data Pipeline scripts are all loaded, in one declared order.

``static/js/data_management/*.js`` share one global scope, so a file missing
from the page's load list leaves its functions undefined for every inline
handler that calls them.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web_interface"


def _load_order() -> list[str]:
    index = (WEB / "templates" / "index.html").read_text(encoding="utf-8")
    match = re.search(r"{% for dm_script in \[([^\]]*)\] %}", index)
    assert match, "index.html no longer declares the data_management load order"
    return re.findall(r"'([a-z_]+)'", match.group(1))


def test_every_data_management_script_is_loaded_once():
    on_disk = sorted(p.stem for p in (WEB / "static" / "js" / "data_management").glob("*.js"))
    order = _load_order()
    assert sorted(order) == on_disk
    assert len(order) == len(set(order))


def test_shared_state_loads_first():
    assert _load_order()[0] == "core"
