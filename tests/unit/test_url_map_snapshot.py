"""URL-map regression guard for the Phase 7 web-layer restructuring.

Snapshots every (rule, endpoint, methods) triple of the full web app and
compares it against a committed fixture. Any route split/move that changes a
URL rule, endpoint name, or method set fails this test.

Regenerate the fixture (only for an intentional, reviewed route change):

    FYP_REGEN_URL_MAP=1 python -m pytest tests/unit/test_url_map_snapshot.py
"""

import json
import os
from pathlib import Path

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "url_map_snapshot.json"


def _live_url_map():
    # The task-runner service registers only the internal blueprint; make sure
    # the full web UI registers before the app module is (first) imported.
    os.environ.pop("K_SERVICE", None)
    import web_interface.fyp_data_hub as hub

    entries = []
    for rule in hub.app.url_map.iter_rules():
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        entries.append([rule.rule, rule.endpoint, methods])
    return sorted(entries)


def test_url_map_matches_snapshot():
    live = _live_url_map()

    if os.environ.get("FYP_REGEN_URL_MAP") == "1":
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(json.dumps(live, indent=1) + "\n")

    expected = json.loads(FIXTURE_PATH.read_text())

    live_set = {tuple((r, e, tuple(m))) for r, e, m in live}
    expected_set = {tuple((r, e, tuple(m))) for r, e, m in expected}
    missing = sorted(expected_set - live_set)
    added = sorted(live_set - expected_set)
    assert live == expected, (
        f"URL map drifted from snapshot. Missing: {missing} Added: {added}"
    )
