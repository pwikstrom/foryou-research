"""The activity-type vocabulary is declared once and every platform keeps to it.

``fyp.core.utils`` owns the canonical sets (viewing / engagement / standalone),
the UI label map, and the fold map. Each ingester declares the types it can
emit in ``emitted_activity_types``. These tests stop a platform class from
inventing a value the rest of the Hub has never heard of, and stop the
label map and the vocabulary drifting apart.
"""

import fyp.ingest  # noqa: F401  (registers every platform class)
from fyp.core.utils import (
    ACTIVITY_TYPE_MAP,
    ENGAGEMENT_LABELS,
    ENGAGEMENT_TYPES,
    KNOWN_ACTIVITY_TYPES,
    STANDALONE_ACTIVITY_TYPES,
    STANDALONE_ENGAGEMENT_LABELS,
    VIEWING_ACTIVITY_TYPES,
    engagement_label,
    parse_extra_data_tokens,
)
from fyp.ingest.base import ForYouBaseCollection
from fyp.ingest.instagram import InstagramDDPCollection
from fyp.ingest.tiktok import TikTokDDPCollection
from fyp.ingest.youtube import YouTubeDDPCollection


def test_vocabulary_sets_are_disjoint_and_cover_known():
    v, e, s = set(VIEWING_ACTIVITY_TYPES), set(ENGAGEMENT_TYPES), set(STANDALONE_ACTIVITY_TYPES)
    assert not (v & e) and not (v & s) and not (e & s)
    assert KNOWN_ACTIVITY_TYPES == v | e | s


def test_only_engagement_types_fold():
    assert set(ACTIVITY_TYPE_MAP) == set(ENGAGEMENT_TYPES)
    assert set(ACTIVITY_TYPE_MAP.values()) == set(ENGAGEMENT_TYPES)
    # A follow has no item to fold onto: it is not a fold token.
    assert "follow" not in ACTIVITY_TYPE_MAP
    assert parse_extra_data_tokens("fave,share:copy_link,follow:someone") == {"fave", "share"}


def test_labels_cover_the_vocabulary():
    assert set(ENGAGEMENT_LABELS) == set(ENGAGEMENT_TYPES)
    assert set(STANDALONE_ENGAGEMENT_LABELS) == set(ENGAGEMENT_TYPES) | {"follow"}
    assert engagement_label("fave") == "Like"
    assert engagement_label("save") == "Save"
    assert engagement_label("unknown_thing") == "Unknown_Thing"


def test_every_registered_platform_declares_known_types():
    classes = [c for c in ForYouBaseCollection._registry if c.__name__ != "ForYouCollection"]
    assert classes, "the registry is empty — fyp.ingest did not import the platforms"
    for cls in classes:
        declared = set(cls.emitted_activity_types)
        assert declared, f"{cls.__name__} declares no emitted_activity_types"
        assert declared <= KNOWN_ACTIVITY_TYPES, \
            f"{cls.__name__} emits {declared - KNOWN_ACTIVITY_TYPES} — add it to fyp.core.utils first"


def test_section_maps_are_within_each_platforms_declaration():
    tiktok_values = set(TikTokDDPCollection._ACTIVITY_TYPE_MAP.values()) | {"login"}
    assert tiktok_values <= TikTokDDPCollection.emitted_activity_types
    assert {a for _, a in InstagramDDPCollection._STREAMS} <= InstagramDDPCollection.emitted_activity_types
    assert {a for _, a, _ in YouTubeDDPCollection._ENGAGEMENT_MEMBERS} | {"play", "ad_play"} \
        <= YouTubeDDPCollection.emitted_activity_types


def test_every_whitelisted_section_has_a_review_title():
    """The review manifest is the donor's consent surface: no section without a title."""
    for key in TikTokDDPCollection._ACTIVITY_TYPE_MAP:
        assert key in TikTokDDPCollection._REVIEW_TITLES, key
    for suffix, _ in InstagramDDPCollection._STREAMS:
        assert suffix in InstagramDDPCollection._STREAM_TITLES, suffix
    for suffix, _, _ in YouTubeDDPCollection._ENGAGEMENT_MEMBERS:
        assert suffix in YouTubeDDPCollection._REVIEW_CSV_TITLES, suffix
