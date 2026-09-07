"""What a donor leaves out is their choice; what the platform changed is drift.

Two uploads on 2026-09-07 were quarantined for "missing core paths" that
were simply sections the donor had not donated (categories unticked in
TikTok's export; sections pruned in the browser review). The structure layer
now quarantines only changes INSIDE sections the file contains — a known
field gone from a present record, a path back with another type — and
records withheld sections as a note.
"""

import pandas as pd
import pytest

import fyp.core.structure_sentinel as ss

FULL = [
    "Your Activity.Watch History.VideoList[].Date|str",
    "Your Activity.Watch History.VideoList[].Link|str",
    "Your Activity.Searches.SearchList[].Date|str",
    "Your Activity.Searches.SearchList[].SearchTerm|str",
    "Likes and Favorites.Like List.ItemFavoriteList[].Date|str",
    "Likes and Favorites.Like List.ItemFavoriteList[].Link|str",
    "Likes and Favorites.Collection|dict",
    "Income+ Wallet.Coin Purchase History.CoinPurchaseHistoryList|null",
    "Profile And Settings.Profile Info.ProfileMap.userName|str",
    "Profile And Settings.Profile Info.ProfileMap.bioDescription|str",
]


def _fp(paths):
    return {"kind": "json", "member_paths": [], "key_paths": sorted(paths), "stats": {}}


def _baseline(paths=FULL, n=20, members=()):
    baseline = ss._empty_baseline()
    fp = _fp(paths)
    fp["member_paths"] = list(members)
    for i in range(n):
        ss.learn_file(baseline, fp, {"raw_rows": 100}, None, f"f-{i}.json")
    return baseline


def _codes(findings):
    return {(f["severity"], f["code"]) for f in findings}


def test_watch_history_only_donation_is_ok_with_the_rest_noted():
    findings = ss.evaluate_structure(_fp(FULL[:2]), _baseline())
    assert _codes(findings) == {("note", "withheld_sections")}
    assert ss.status_from_findings(findings, 20) == "ok"
    assert ss.withheld_sections(findings) == [
        "Income+ Wallet", "Likes and Favorites", "Profile And Settings", "Your Activity.Searches"]


def test_renamed_field_inside_a_present_section_quarantines():
    renamed = [p.replace("VideoList[].Date", "VideoList[].Timestamp") for p in FULL]
    findings = ss.evaluate_structure(_fp(renamed), _baseline())
    assert ("quarantine", "missing_core_paths") in _codes(findings)
    missing = next(f for f in findings if f["code"] == "missing_core_paths")
    assert missing["items"] == ["Your Activity.Watch History.VideoList[].Date|str"]
    assert ss.status_from_findings(findings, 20) == "quarantined"


def test_field_gone_from_a_present_record_quarantines_but_pruned_subsection_does_not():
    paths = [p for p in FULL if "bioDescription" not in p and "Collection|dict" not in p]
    findings = ss.evaluate_structure(_fp(paths), _baseline())
    missing = next(f for f in findings if f["code"] == "missing_core_paths")
    assert missing["items"] == ["Profile And Settings.Profile Info.ProfileMap.bioDescription|str"]
    assert "Likes and Favorites.Collection" in ss.withheld_sections(findings)


def test_type_change_of_a_present_path_quarantines():
    retyped = [p.replace("VideoList[].Link|str", "VideoList[].Link|int") for p in FULL]
    findings = ss.evaluate_structure(_fp(retyped), _baseline())
    assert ("quarantine", "type_changed") in _codes(findings)


def test_empty_container_forms_are_not_drift():
    # Baseline: nobody bought coins (null). This donor did (populated list),
    # another donor has an empty list. Neither is a type change.
    populated = [p for p in FULL if "CoinPurchase" not in p] + [
        "Income+ Wallet.Coin Purchase History.CoinPurchaseHistoryList[].Date|str"]
    assert ss.status_from_findings(ss.evaluate_structure(_fp(populated), _baseline()), 20) == "warn"  # new paths only
    assert ("quarantine", "type_changed") not in _codes(ss.evaluate_structure(_fp(populated), _baseline()))
    empty = [p.replace("CoinPurchaseHistoryList|null", "CoinPurchaseHistoryList|list") for p in FULL]
    assert ss.status_from_findings(ss.evaluate_structure(_fp(empty), _baseline()), 20) == "ok"


def test_empty_list_in_the_file_is_a_withheld_section_not_missing_fields():
    # The donor has no searches: the list is [] so its record fields are absent.
    paths = [p for p in FULL if "SearchList[]" not in p] + ["Your Activity.Searches.SearchList|list"]
    findings = ss.evaluate_structure(_fp(paths), _baseline())
    assert ("quarantine", "missing_core_paths") not in _codes(findings)
    assert ss.withheld_sections(findings) == ["Your Activity.Searches.SearchList[]"]


def test_missing_zip_member_is_withheld_not_quarantined():
    members = ["connections/followers.json", "activity/liked_posts.json"]
    baseline = _baseline(paths=["activity/liked_posts.json::likes[].ts|int"], members=members)
    fp = _fp(["activity/liked_posts.json::likes[].ts|int"])
    fp["member_paths"] = ["activity/liked_posts.json"]
    findings = ss.evaluate_structure(fp, baseline)
    assert ("quarantine", "missing_member") not in _codes(findings)
    assert ss.withheld_sections(findings) == ["connections/followers.json"]


def test_container_and_withheld_root_helpers():
    assert ss.container_of("a.b.L[].f") == "a.b.L[]"
    assert ss.container_of("a.b.L[]") == "a.b.L"
    assert ss.container_of("a.b") == "a"
    assert ss.container_of("a") is None
    present = ss._ancestors_present({"Your Activity.Watch History.VideoList[].Date"})
    assert {"Your Activity", "Your Activity.Watch History", "Your Activity.Watch History.VideoList[]"} <= present
    assert ss.withheld_root("Profile And Settings.Follower.FansList[].Date", present) == "Profile And Settings"
    assert ss.withheld_root("Your Activity.Searches.SearchList[].Date", present) == "Your Activity.Searches"
    assert ss.withheld_root("Your Activity.Watch History.VideoList[].Link", present) is None


def test_verdict_and_ledger_note_carry_withheld_sections(monkeypatch):
    baselines = {"schema_version": 1, "baselines": {ss.baseline_key("tiktok", "ddp"): _baseline()}}
    monkeypatch.setattr(ss, "load_baselines", lambda: baselines)
    monkeypatch.setattr(ss, "load_verdicts", lambda: {"schema_version": 1, "files": {}})
    monkeypatch.setattr(ss.data_io, "getsize", lambda **kw: 1024)

    class Col:
        source_platform, data_source, raw_path = "tiktok", "ddp", "ddp_raw"

        def fingerprint_raw(self, filename):
            return _fp(FULL[:2])

    verdict = ss.StructureSentinel().check_raw(Col(), "wh.json", pd.DataFrame({"a": range(20)}))
    assert verdict["status"] == "ok"
    assert verdict["withheld_sections"][0] == "Income+ Wallet"

    from web_interface.run_ingest_refresh import _withheld_note
    assert _withheld_note({"withheld_sections": ["Post", "TikTok Live"]}) == "Uploader withheld: Post, TikTok Live"
    assert _withheld_note({}) is None


@pytest.mark.parametrize("n", [0, 2])
def test_immature_baseline_still_learns_only(n):
    findings = ss.evaluate_structure(_fp(FULL[:2]), _baseline(n=n))
    assert findings == []
