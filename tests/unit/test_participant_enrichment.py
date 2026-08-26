"""First-batch enrichment for participant collections (recruitment funnel).

All storage and queue calls are monkeypatched — nothing here touches disk,
GCS, SMTP or the real queues. Pins:

1. The master switch ships OFF: ingest auto-queues nothing until an operator
   flips ``AUTO_ENQUEUE_ENABLED``.
2. When enabled, ``enqueue_first_batches`` queues only OWNED collections,
   most-recent view items first, capped at ``FIRST_BATCH_SIZE`` — into the
   SCRAPE queue only (never straight into the annotation queue, whose worker
   would burn unscraped items as "file not found" failures).
3. It is idempotent: a collection already in the ledger is never re-queued.
4. ``check_first_batch_completions`` hands scraped-but-unannotated ledger
   items to ``to_annotate.json`` exactly once (the scrape → annotate handoff),
   then notifies once when the completion thresholds are met, honouring the
   ``consent_to_contact`` switch; the ledger entry closes even when no email
   goes out (so the tour re-offer still arms).
"""

import pandas as pd
import pytest

import web_interface.services.participant_enrichment as pe


@pytest.fixture
def store(monkeypatch):
    """In-memory stand-ins for data_io json files + the scrape queue."""
    files: dict[str, object] = {}
    queues: dict[str, list] = {}

    def load_json(storage_location="cache", filename="", **kwargs):
        return files.get(filename)

    def update_json(storage_location="cache", filename="", mutate=None, default=None):
        files[filename] = mutate(files.get(filename, default))
        return files[filename]

    monkeypatch.setattr(pe.data_io, "load_json", load_json)
    monkeypatch.setattr(pe.data_io, "update_json", update_json)

    class FakeQueues:
        @staticmethod
        def registered_platforms():
            return ["tiktok", "instagram", "youtube"]

        @staticmethod
        def append_to_scrape_queue(platform, items):
            queues.setdefault(platform, []).extend(items)
            return len(items)

    import fyp.scrape.scrape_queues as scrape_queues
    monkeypatch.setattr(scrape_queues, "registered_platforms", FakeQueues.registered_platforms)
    monkeypatch.setattr(scrape_queues, "append_to_scrape_queue", FakeQueues.append_to_scrape_queue)

    return {"files": files, "queues": queues}


def _recoded_frame(cid, n_items, platform="tiktok"):
    return pd.DataFrame({
        "collection_id": [cid] * n_items,
        "source_platform": [platform] * n_items,
        "item_id": [f"{cid}-item-{i}" for i in range(n_items)],
        "activity_type": ["play"] * n_items,
        "utc_timestamp": pd.date_range("2026-01-01", periods=n_items, freq="h"),
    })


@pytest.fixture
def owned(monkeypatch):
    import web_interface.collection_accounts as ca
    monkeypatch.setattr(ca, "load_owner_map",
                        lambda fresh=False: {"c1": "donor@example.org"})


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(pe, "AUTO_ENQUEUE_ENABLED", True)


def test_master_switch_ships_off_and_disables_enqueue(store, owned, monkeypatch):
    assert pe.AUTO_ENQUEUE_ENABLED is False
    called = []
    monkeypatch.setattr(pe.data_io, "load_parquet_selective",
                        lambda **kw: called.append(kw) or _recoded_frame("c1", 5))

    assert pe.enqueue_first_batches(["c1"], log=lambda *_: None) == {}
    assert not called                      # never even scanned the parquet
    assert store["queues"] == {}           # nothing queued anywhere
    assert pe.LEDGER_FILENAME not in store["files"]


def test_enqueue_caps_prefers_recent_and_is_scrape_only(store, owned, enabled, monkeypatch):
    frame = _recoded_frame("c1", 80)
    monkeypatch.setattr(pe.data_io, "load_parquet_selective", lambda **kw: frame)
    monkeypatch.setattr(pe, "_status_lookup", lambda items: None)

    queued = pe.enqueue_first_batches(["c1"], log=lambda *_: None)

    assert queued == {"c1": pe.FIRST_BATCH_SIZE}
    items = store["queues"]["tiktok"]
    assert len(items) == pe.FIRST_BATCH_SIZE
    # Most recent first: the highest-numbered (latest-timestamp) item leads.
    assert items[0] == "c1-item-79"
    # Scrape queue ONLY — annotation happens via the consolidation handoff.
    assert "to_annotate.json" not in store["files"]
    entry = store["files"][pe.LEDGER_FILENAME]["c1"]
    assert entry["owner"] == "donor@example.org"
    assert entry["notified"] is False
    assert entry["annotate_queued"] == []
    assert len(entry["item_ids"]) == pe.FIRST_BATCH_SIZE


def test_enqueue_skips_unowned_and_already_ledgered(store, owned, enabled, monkeypatch):
    store["files"][pe.LEDGER_FILENAME] = {"c1": {"owner": "donor@example.org",
                                                 "item_ids": ["x"], "notified": False}}
    called = []
    monkeypatch.setattr(pe.data_io, "load_parquet_selective",
                        lambda **kw: called.append(kw) or _recoded_frame("c1", 5))

    # c1 is ledgered, c2 is unowned -> no candidates, no parquet scan at all.
    assert pe.enqueue_first_batches(["c1", "c2"], log=lambda *_: None) == {}
    assert not called
    assert store["queues"] == {}


def _completion_env(store, monkeypatch, *, consent, scraped_count, annotated_count,
                    annotate_queued=None):
    store["files"][pe.LEDGER_FILENAME] = {
        "c1": {"owner": "donor@example.org",
               "item_ids": [f"i{n}" for n in range(10)],
               "annotate_queued": list(annotate_queued or []),
               "notified": False},
    }
    ids = [f"i{n}" for n in range(10)]
    flags = {
        "scraped": pd.Series([True] * scraped_count + [False] * (10 - scraped_count), index=ids),
        "annotated": pd.Series([True] * annotated_count + [False] * (10 - annotated_count), index=ids),
    }
    monkeypatch.setattr(pe, "_status_lookup", lambda items: flags)

    sent = []
    from web_interface import mail_utils
    monkeypatch.setattr(mail_utils, "send_first_batch_ready_email_async",
                        lambda to, cid, n: sent.append((to, cid, n)))

    settings = {}

    class FakeUser:
        profile = {"consent_to_contact": consent}

    from web_interface.security import user_manager
    monkeypatch.setattr(user_manager, "get_user",
                        lambda username: FakeUser() if username == "donor@example.org" else None)
    monkeypatch.setattr(user_manager, "update_user_settings",
                        lambda username, s: settings.update(s) or (True, "ok"))
    return sent, settings


def test_handoff_queues_scraped_unannotated_items_once(store, monkeypatch):
    # 6 scraped, 2 of them already annotated, 1 already handed off earlier.
    sent, _ = _completion_env(store, monkeypatch, consent=True,
                              scraped_count=6, annotated_count=2,
                              annotate_queued=["i2"])

    assert pe.check_first_batch_completions() == []  # below completion bar
    # Handed off: scraped (i0..i5) minus annotated (i0, i1) minus already (i2).
    assert set(store["files"]["to_annotate.json"]) == {"i3", "i4", "i5"}
    entry = store["files"][pe.LEDGER_FILENAME]["c1"]
    assert set(entry["annotate_queued"]) == {"i2", "i3", "i4", "i5"}

    # A second run with unchanged status adds nothing new.
    store["files"]["to_annotate.json"] = []
    pe.check_first_batch_completions()
    assert store["files"]["to_annotate.json"] == []


def test_completion_emails_consenting_owner_once(store, monkeypatch):
    sent, settings = _completion_env(store, monkeypatch, consent=True,
                                     scraped_count=10, annotated_count=8,
                                     annotate_queued=[f"i{n}" for n in range(10)])

    assert pe.check_first_batch_completions() == ["c1"]
    assert sent == [("donor@example.org", "c1", 8)]
    assert settings.get("hub_tour_real_data_pending") is True
    assert store["files"][pe.LEDGER_FILENAME]["c1"]["notified"] is True

    # Second call: the entry is closed, nothing fires again.
    sent.clear()
    assert pe.check_first_batch_completions() == []
    assert sent == []


def test_completion_below_threshold_waits(store, monkeypatch):
    sent, _ = _completion_env(store, monkeypatch, consent=True,
                              scraped_count=10, annotated_count=4,
                              annotate_queued=[f"i{n}" for n in range(10)])
    assert pe.check_first_batch_completions() == []
    assert sent == []
    assert store["files"][pe.LEDGER_FILENAME]["c1"]["notified"] is False


def test_completion_without_consent_closes_but_never_emails(store, monkeypatch):
    sent, settings = _completion_env(store, monkeypatch, consent=False,
                                     scraped_count=10, annotated_count=10,
                                     annotate_queued=[f"i{n}" for n in range(10)])
    assert pe.check_first_batch_completions() == ["c1"]
    assert sent == []
    # The tour re-offer still arms — it happens in the app, not the inbox.
    assert settings.get("hub_tour_real_data_pending") is True
    entry = store["files"][pe.LEDGER_FILENAME]["c1"]
    assert entry["notified"] is True
    assert entry["emailed"] is False
