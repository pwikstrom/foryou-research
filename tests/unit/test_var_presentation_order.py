"""Unit tests for the admins' default variable order in var_presentation.json.

Pins:

  * ``save_presentation`` round-trips ``order``; ``order=None`` keeps the
    stored order, ``[]`` resets a surface to the computed order; malformed
    orders are rejected.
  * ``compute_presentation_etag`` changes on a reorder-only edit (the schema
    fingerprint is built from it, so other containers must see the change) and
    is unchanged for a store without an order.
  * ``_migrate_retired_names`` maps retired names inside ``order`` too.

The store is an in-memory stand-in for data_io — no files, no GCS.

Usage:
    python tests/unit/test_var_presentation_order.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fyp.annotation import var_presentation as vp
from fyp.scrape.scrape_contract import RETIRED_TO_GENERIC


class _FakeStore:
    def __init__(self, payload=None):
        self.payload = payload

    def load_json_optional(self, storage_location, filename):
        return self.payload

    def save_json(self, data, storage_location, filename):
        self.payload = data


def _with_store(payload=None):
    store = _FakeStore(payload)
    orig = vp._data_io
    vp._data_io = lambda: store
    return store, orig


BASE = {"version": 1, "surfaces": {"filter": ["a", "b"], "timeline": [], "viz": [], "display": []}}


def test_order_round_trips_and_is_kept_or_reset() -> None:
    store, orig = _with_store(dict(BASE))
    try:
        vp.save_presentation({"filter": ["a", "b"]}, order={"filter": ["b", "a", "b"]})
        assert store.payload["order"] == {"filter": ["b", "a"]}, store.payload
        # Membership-only save (the admin table's checkboxes) keeps the order.
        vp.save_presentation({"filter": ["a"]})
        assert store.payload["order"] == {"filter": ["b", "a"]}, store.payload
        assert vp.presentation_order(store.payload) == {"filter": ["b", "a"]}
        # [] resets that surface; the key disappears once nothing is ordered.
        vp.save_presentation({}, order={"filter": []})
        assert "order" not in store.payload, store.payload
    finally:
        vp._data_io = orig


def test_malformed_order_rejected() -> None:
    store, orig = _with_store(dict(BASE))
    try:
        for bad in ([], {"nope": []}, {"filter": "a"}, {"filter": [1]}):
            try:
                vp.save_presentation({}, order=bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad!r}")
    finally:
        vp._data_io = orig


def test_etag_tracks_order_only_edits() -> None:
    plain = dict(BASE)
    ordered = {**BASE, "order": {"filter": ["b", "a"]}}
    reordered = {**BASE, "order": {"filter": ["a", "b"]}}
    e_plain = vp.compute_presentation_etag(plain)
    assert e_plain == vp.compute_presentation_etag({**BASE, "order": {}})
    assert e_plain != vp.compute_presentation_etag(ordered)
    assert vp.compute_presentation_etag(ordered) != vp.compute_presentation_etag(reordered)


def test_migration_maps_order_names() -> None:
    retired, generic = next(iter(RETIRED_TO_GENERIC.items()))
    store, orig = _with_store(None)
    try:
        payload = {
            "version": 1,
            "surfaces": {"filter": [generic], "timeline": [], "viz": [], "display": []},
            "order": {"filter": [retired, "x"]},
        }
        out = vp._migrate_retired_names(payload)
        assert out["order"] == {"filter": [generic, "x"]}, out
    finally:
        vp._data_io = orig


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
