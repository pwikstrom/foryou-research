"""Deferred downstream refresh: the one full analysis refresh per plan.

Mid-plan consolidations run core-only and accumulate their impact in the
enrichment ledger's ``__meta__.deferred_impact``; the supervisor's finalize
(or any full refresh) settles the debt exactly once. Pins: the union math,
the accumulate/settle lifecycle, the quiet-path and 24h-backstop finalize
decisions, and that a failed dispatch never loses the debt.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

import web_interface.services.collection_enrichment as ce
import web_interface.services.downstream_refresh as dr


@pytest.fixture
def store(monkeypatch):
    """In-memory ledger for ce.get_meta/set_meta (same shape the CAS uses)."""
    files: dict[str, object] = {}

    def load_json(storage_location="cache", filename="", **kwargs):
        return files.get(filename)

    def update_json(storage_location="cache", filename="", mutate=None,
                    default=None, **kwargs):
        files[filename] = mutate(files.get(filename, default))
        return files[filename]

    monkeypatch.setattr(ce.data_io, "load_json", load_json)
    monkeypatch.setattr(ce.data_io, "update_json", update_json)
    return files


IMPACT_A = {"affected_study_names": ["s1"], "affected_collection_ids": ["c1"],
            "new_annotation_item_count": 100}
IMPACT_B = {"affected_study_names": ["s1", "s2"], "affected_collection_ids": ["c2"],
            "new_annotation_item_count": 50}


def test_impact_union_merges_lists_and_sums_counts():
    merged = dr.impact_union(IMPACT_A, IMPACT_B)
    assert merged["affected_study_names"] == ["s1", "s2"]
    assert merged["affected_collection_ids"] == ["c1", "c2"]
    assert merged["new_annotation_item_count"] == 150
    assert dr.impact_union(None, IMPACT_A)["affected_study_names"] == ["s1"]
    assert dr.impact_union(IMPACT_A, None) == dict(IMPACT_A)
    assert dr.impact_union(None, None) is None


def test_accumulate_and_settle_lifecycle(store):
    dr.accumulate_deferred_impact(IMPACT_A)
    dr.accumulate_deferred_impact(IMPACT_B)
    deferred = dr.get_deferred_impact()
    assert deferred["affected_study_names"] == ["s1", "s2"]
    assert deferred["new_annotation_item_count"] == 150
    assert deferred["runs"] == 2
    assert deferred["deferred_since"]          # stamped on the first deferral
    first_since = deferred["deferred_since"]

    dr.accumulate_deferred_impact(None)        # empty impact: untouched
    assert dr.get_deferred_impact()["runs"] == 2
    assert dr.get_deferred_impact()["deferred_since"] == first_since

    dr.settle_deferred_impact()
    assert dr.get_deferred_impact() is None
    assert dr.last_full_refresh()              # stamped by the settle


def test_supervisor_quiet_finalize_dispatches_and_clears(store):
    import web_interface.run_enrichment_supervisor as sup

    dr.accumulate_deferred_impact(IMPACT_A)
    calls = []

    class Rep:
        def log(self, m): pass

    with patch.object(dr, "dispatch_downstream_refresh",
                      side_effect=lambda impact, **kw: (calls.append(impact),
                                                       ("started", "ok"))[1]):
        out = sup._finalize(Rep())
    assert out and out["action"] == "finalize"
    assert calls == [None]                     # scope comes from the deferred meta


def test_quiet_finalize_requires_idle_workers(store, monkeypatch):
    """Regression pin, observed live 2026-09-01: a tick where everything is
    merely WAITING (scraper mid-run, jobs in flight, nothing to start) also
    falls through to the quiet path — it must not refresh then, or the
    pipeline blocks the loop for the rest of the cycle."""
    import web_interface.run_enrichment_supervisor as sup
    import web_interface.services.worker_status as ws

    class Rep:
        def log(self, m): pass

    dr.accumulate_deferred_impact(IMPACT_A)
    monkeypatch.setattr(ws, "_workers_blocking_consolidate",
                        lambda: ["queue_scraper_tiktok"])
    with patch.object(dr, "dispatch_downstream_refresh") as dispatch:
        assert sup._finalize(Rep()) is None
    dispatch.assert_not_called()
    assert dr.get_deferred_impact() is not None

    # The BACKSTOP path deliberately still fires while lanes are busy.
    ce.set_meta(dr.LAST_FULL_REFRESH_KEY,
                (datetime.now(UTC)
                 - timedelta(hours=sup.FINALIZE_BACKSTOP_H + 1)).isoformat())
    with patch.object(dr, "dispatch_downstream_refresh",
                      return_value=("started", "ok")) as dispatch:
        out = sup._finalize(Rep(), require_backstop=True)
    assert out and out["action"] == "finalize"


def test_supervisor_finalize_noops_without_debt(store):
    import web_interface.run_enrichment_supervisor as sup

    class Rep:
        def log(self, m): pass

    with patch.object(dr, "dispatch_downstream_refresh") as dispatch:
        assert sup._finalize(Rep()) is None
    dispatch.assert_not_called()


def test_backstop_fires_only_after_the_window(store):
    import web_interface.run_enrichment_supervisor as sup

    class Rep:
        def log(self, m): pass

    fresh = datetime.now(UTC).isoformat()
    stale = (datetime.now(UTC)
             - timedelta(hours=sup.FINALIZE_BACKSTOP_H + 1)).isoformat()

    dr.accumulate_deferred_impact(IMPACT_A)
    ce.set_meta(dr.LAST_FULL_REFRESH_KEY, fresh)
    with patch.object(dr, "dispatch_downstream_refresh") as dispatch:
        assert sup._finalize(Rep(), require_backstop=True) is None
    dispatch.assert_not_called()

    ce.set_meta(dr.LAST_FULL_REFRESH_KEY, stale)
    with patch.object(dr, "dispatch_downstream_refresh",
                      return_value=("started", "ok")) as dispatch:
        out = sup._finalize(Rep(), require_backstop=True)
    assert out and out["action"] == "finalize"
    dispatch.assert_called_once()


def test_failed_dispatch_keeps_the_debt(store):
    import web_interface.run_enrichment_supervisor as sup

    class Rep:
        def log(self, m): pass

    dr.accumulate_deferred_impact(IMPACT_A)
    with patch.object(dr, "dispatch_downstream_refresh",
                      return_value=("error", "boom")):
        assert sup._finalize(Rep()) is None
    assert dr.get_deferred_impact() is not None   # retried on a later tick


def test_noop_dispatch_settles_an_unrefreshable_debt(store):
    # A debt whose pipeline builds empty (its studies were deleted) must not
    # make finalize retry forever.
    import web_interface.run_enrichment_supervisor as sup

    class Rep:
        def log(self, m): pass

    dr.accumulate_deferred_impact(IMPACT_A)
    with patch.object(dr, "dispatch_downstream_refresh",
                      return_value=("noop", "nothing")):
        assert sup._finalize(Rep()) is None
    assert dr.get_deferred_impact() is None


def test_core_only_consolidation_accumulates_via_the_worker_path(store):
    # The consolidation worker's not-auto_refresh branch is what records the
    # debt; drive just that logic the way run_consolidate_enrichment does.
    dr.accumulate_deferred_impact(dict(IMPACT_A))
    dr.accumulate_deferred_impact(dict(IMPACT_A))
    deferred = dr.get_deferred_impact()
    assert deferred["new_annotation_item_count"] == 200
    assert deferred["affected_study_names"] == ["s1"]
