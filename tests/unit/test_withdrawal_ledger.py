"""The participant withdrawal ledger's restore-window arithmetic.

``restorable_until`` reaches the browser (My Collections renders it) and the
admin notification email, so it travels as an offset-aware UTC instant per the
web layer's date-time convention (tests/unit/test_datetime_consistency.py).
Entries written before that convention are zone-less, so every read has to
tolerate a naive stamp rather than blow up comparing it to an aware "now".
"""

import pandas as pd
import pytest

from web_interface.services import my_collections_service as mcs


@pytest.fixture
def ledger(monkeypatch):
    """An in-memory withdrawals.json; no storage is touched."""
    store: dict = {}
    monkeypatch.setattr(mcs, "_load_withdrawals_raw", lambda: dict(store))

    def _save(w):
        store.clear()
        store.update(w)

    monkeypatch.setattr(mcs, "_save_withdrawals", _save)
    return store


def test_record_withdrawal_stamps_are_offset_aware(ledger):
    entry = mcs.record_withdrawal(
        "c1", "p@x.com", ["c1.json"], "raw/c1.json", "display1", "tiktok")

    for field in ("deleted_at", "restorable_until"):
        ts = pd.Timestamp(entry[field])
        assert ts.tz is not None, f"{field} must carry an offset: {entry[field]!r}"

    window = pd.Timestamp(entry["restorable_until"]) - pd.Timestamp(entry["deleted_at"])
    assert window == pd.Timedelta(days=mcs.WITHDRAWAL_RETENTION_DAYS)
    assert ledger["c1"]["files"] == ["c1.json"]


def test_load_withdrawals_purges_only_expired_entries(ledger, monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    ledger.update({
        "live": {"restorable_until": (now + pd.Timedelta(days=5)).isoformat(),
                 "files": ["live.json"]},
        "expired": {"restorable_until": (now - pd.Timedelta(days=1)).isoformat(),
                    "files": ["expired.json"]},
        # Written before the stamps became offset-aware: naive UTC.
        "legacy_expired": {
            "restorable_until": (now - pd.Timedelta(days=1)).tz_localize(None).isoformat(),
            "files": ["legacy.json"]},
        "legacy_live": {
            "restorable_until": (now + pd.Timedelta(days=5)).tz_localize(None).isoformat(),
            "files": ["legacy_live.json"]},
        "unparseable": {"restorable_until": "not a date", "files": []},
    })
    removed: list[str] = []
    monkeypatch.setattr(mcs.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(mcs.data_io, "remove",
                        lambda **kw: removed.append(kw["filename"]))

    remaining = mcs.load_withdrawals()

    assert set(remaining) == {"live", "legacy_live", "unparseable"}
    assert sorted(removed) == ["expired.json", "legacy.json"]


def test_load_withdrawals_without_purge_leaves_the_ledger_alone(ledger):
    stale = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)).isoformat()
    ledger["expired"] = {"restorable_until": stale, "files": ["expired.json"]}

    assert set(mcs.load_withdrawals(purge=False)) == {"expired"}


@pytest.mark.parametrize("aware", [True, False])
def test_restore_past_the_window_is_refused(ledger, aware):
    """A naive legacy stamp must be refused, not raise on the comparison."""
    closed = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)
    if not aware:
        closed = closed.tz_localize(None)
    ledger["c1"] = {"restorable_until": closed.isoformat(),
                    "raw_path": "raw/c1.json", "files": ["c1.json"]}

    with pytest.raises(mcs.RestoreError, match="restore window"):
        mcs.restore_withdrawal("c1")


def test_restore_without_a_usable_stamp_is_refused(ledger):
    ledger["c1"] = {"restorable_until": "not a date", "files": ["c1.json"]}

    with pytest.raises(mcs.RestoreError, match="restore window"):
        mcs.restore_withdrawal("c1")
