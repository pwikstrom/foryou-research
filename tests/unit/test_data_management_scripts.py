"""The Data Pipeline scripts are all loaded, in one declared order.

``static/js/data_management/*.js`` share one global scope, so a file missing
from the page's load list leaves its functions undefined for every inline
handler that calls them. A My Studies viewer without the Data Pipeline tab
loads only ``core`` and ``studies`` (the read-only study list and modal), so
those two may call into the other files only on paths such a viewer never
reaches.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web_interface"
DM = WEB / "static" / "js" / "data_management"
INDEX = (WEB / "templates" / "index.html").read_text(encoding="utf-8")

MY_STUDIES_SCRIPTS = ["core", "studies"]

# Calls from the My Studies scripts into Data-Pipeline-only files, and why a
# My Studies viewer never makes them.
ALLOWED_CALLS = {
    ("core", "renderEditActivityTable"): "runs only when the Edit Collections list is on the page",
    ("core", "filterEditActivityCollections"): "same guard as renderEditActivityTable",
    **{
        ("studies", name): "editable collection picker only; My Studies opens studies read-only"
        for name in (
            "_dmDuplicateDisplayKeys",
            "_dmAccountCell",
            "_dmDisplayKey",
            "_dmHasUnlistedTwin",
            "_dmDuplicateFlag",
            "_dmSortTs",
        )
    },
}


def _load_order() -> list[str]:
    match = re.search(r"{% set dm_scripts = \[([^\]]*)\] %}", INDEX)
    assert match, "index.html no longer declares the data_management load order"
    return re.findall(r"'([a-z_]+)'", match.group(1))


def test_every_data_management_script_is_loaded_once():
    on_disk = sorted(p.stem for p in DM.glob("*.js"))
    order = _load_order()
    assert sorted(order) == on_disk
    assert len(order) == len(set(order))


def test_shared_state_loads_first():
    assert _load_order()[0] == "core"


def test_my_studies_viewers_load_only_core_and_studies():
    assert "dm_scripts if user.can_access('tab.data_management') else dm_scripts[:2]" in INDEX
    assert _load_order()[:2] == MY_STUDIES_SCRIPTS


def _top_level_functions(path: Path) -> set[str]:
    return set(re.findall(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", path.read_text(), re.M))


def test_my_studies_scripts_call_pipeline_only_code_only_where_allowed():
    pipeline_only = set()
    for path in DM.glob("*.js"):
        if path.stem not in MY_STUDIES_SCRIPTS:
            pipeline_only |= _top_level_functions(path)
    for stem in MY_STUDIES_SCRIPTS:
        pipeline_only -= _top_level_functions(DM / f"{stem}.js")
    calls = set()
    for stem in MY_STUDIES_SCRIPTS:
        code = "\n".join(
            line
            for line in (DM / f"{stem}.js").read_text().splitlines()
            if not line.lstrip().startswith("//")
        )
        for name in pipeline_only:
            if re.search(rf"(?<![\w$.]){re.escape(name)}\s*\(", code):
                calls.add((stem, name))
    assert calls - ALLOWED_CALLS.keys() == set(), "unguarded call from a My Studies script"
    assert ALLOWED_CALLS.keys() - calls == set(), "stale ALLOWED_CALLS entry"


def _dm_scripts_rendered(monkeypatch, role: str) -> list[str]:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _web import web_client

    with web_client(monkeypatch, {"u": role}, login_as="u") as client:
        html = client.get("/").get_data(as_text=True)
    return re.findall(r'<script src="[^"]*/js/data_management/([a-z_]+)\.js', html)


def test_the_rendered_page_loads_the_right_scripts_per_role(monkeypatch):
    assert _dm_scripts_rendered(monkeypatch, "admin") == _load_order()
    assert _dm_scripts_rendered(monkeypatch, "viewer") == MY_STUDIES_SCRIPTS
