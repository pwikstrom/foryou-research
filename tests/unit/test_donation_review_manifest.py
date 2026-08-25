"""Consistency guards for the pre-upload review manifests.

The browser review UI (donation_review.js) is driven by each DDP ingester's
``review_manifest()``. These tests pin the manifest to the same class
constants the parsers read, so a parser change that adds/renames a section
cannot silently drift away from what the review shows (and prunes).
"""

from fyp.ingest.base import ForYouBaseCollection
from fyp.ingest.instagram import InstagramDDPCollection
from fyp.ingest.tiktok import TikTokDDPCollection, TikTokZeeschuimerCollection
from fyp.ingest.youtube import YouTubeDDPCollection


def test_base_default_is_none():
    assert ForYouBaseCollection.review_manifest() is None
    # Non-DDP classes inherit the default: no review step, plain upload.
    assert TikTokZeeschuimerCollection.review_manifest() is None


def test_tiktok_manifest_matches_whitelist():
    manifest = TikTokDDPCollection.review_manifest()
    assert manifest["kind"] == "json_sections"
    assert manifest["unmapped_policy"] == "strip"

    section_ids = {s["id"] for s in manifest["sections"]}
    # Every whitelisted export section the parser keeps must be reviewable.
    assert section_ids >= set(TikTokDDPCollection._ACTIVITY_TYPE_MAP)
    # Login history is matched by rule (no stable parent key), not by id.
    rules = [s for s in manifest["sections"] if s.get("id_rule")]
    assert [s["id_rule"] for s in rules] == ["second_key_ip"]

    # Viability mirrors load_single_raw's discard gate (> 10 videolist rows).
    v = manifest["viability"]
    assert v["section"] == "videolist"
    assert v["min_rows"] == TikTokDDPCollection(verbose=False).min_required_rows_per_raw_file + 1

    # process_single must use the shared map (not a re-inlined literal).
    assert TikTokDDPCollection._ACTIVITY_TYPE_MAP["videolist"] == "play"


def test_instagram_manifest_matches_streams():
    manifest = InstagramDDPCollection.review_manifest()
    assert manifest["kind"] == "zip_members"
    section_ids = [s["id"] for s in manifest["sections"]]
    assert section_ids == InstagramDDPCollection.zip_member_suffixes()
    assert all(s["parser"] == "instagram_records" for s in manifest["sections"])
    assert all(s.get("row_delete", True) for s in manifest["sections"])
    assert manifest["viability"]["min_total_rows"] == 10


def test_youtube_manifest_matches_members():
    manifest = YouTubeDDPCollection.review_manifest()
    assert manifest["kind"] == "zip_members"
    section_ids = [s["id"] for s in manifest["sections"]]
    assert set(section_ids) == set(YouTubeDDPCollection.zip_member_suffixes())

    by_id = {s["id"]: s for s in manifest["sections"]}
    assert by_id[YouTubeDDPCollection._MEMBER_SUFFIX_JSON]["parser"] == "youtube_watch_json"
    html = by_id[YouTubeDDPCollection._MEMBER_SUFFIX_HTML]
    # The HTML fallback cannot be row-rewritten: toggle-only, opaque.
    assert html["parser"] == "opaque"
    assert html["toggle_only"] is True
    assert html["row_delete"] is False
    for suffix, _, _ in YouTubeDDPCollection._ENGAGEMENT_MEMBERS:
        assert by_id[suffix]["parser"] == "csv"


def test_manifests_are_json_serializable():
    import json
    for cls in (TikTokDDPCollection, InstagramDDPCollection, YouTubeDDPCollection):
        json.dumps(cls.review_manifest())


def test_tiktok_pruned_document_round_trips():
    """A client-pruned TikTok export (whitelisted sections only, rows removed)
    still parses to exactly the kept rows — the review UI's output shape is a
    valid parser input."""
    import fyp.ingest.tiktok as tiktok_mod

    pruned_doc = {
        "Activity": {
            "Video Browsing History": {
                "VideoList": [
                    {"Date": f"2026-05-{(i % 28) + 1:02d} 10:{i % 60:02d}:00",
                     "Link": f"https://www.tiktokv.com/share/video/7{i:018d}/"}
                    for i in range(15)  # 20 in the export, donor removed 5
                ]
            },
        },
        "Comment": {
            "Comments": {
                "CommentsList": [
                    {"date": "2026-05-03 08:00:00", "comment": "nice"},
                ]
            }
        },
        # DMs / login / profile / settings: absent entirely (stripped client-side).
    }

    col = TikTokDDPCollection(verbose=False)
    orig_load_json = tiktok_mod.data_io.load_json
    tiktok_mod.data_io.load_json = lambda **kw: pruned_doc
    try:
        df = col.load_single_raw("pruned.json")
    finally:
        tiktok_mod.data_io.load_json = orig_load_json
    df["raw_file"] = "pruned.json"
    out = col.process_single(df)

    counts = out["activity_type"].value_counts().to_dict()
    assert counts.get("play") == 15
    assert counts.get("comment") == 1
    assert "login" not in counts
