"""Tests for Instagram image-only posts → slideshow flow (no network).

Covers the media-info payload parsing (image-URL extraction, raw-row
construction), the ``fetch()`` image-post branch (happy path, save_media=False,
transient/permanent failure routing), the image downloader's NN-naming and
partial-set cleanup, and the slideshow hooks (``slideshow_image_column``,
``image_count``, ``prepare_raw_batch`` count/duration conversion).
"""

import pandas as pd
import pytest

from fyp.scrape import instagram_dl
from fyp.scrape.instagram_dl import (
    InstagramScraper,
    _extract_image_urls,
    _row_from_media_info,
)
from fyp.scrape.platform_scraper import SLIDESHOW_SECONDS_PER_IMAGE, empty_fail


def _candidates(url: str) -> dict:
    return {"candidates": [{"url": url, "width": 1080, "height": 1350},
                           {"url": url + "?small", "width": 240, "height": 300}]}


_SINGLE_IMAGE_PAYLOAD = {
    "items": [{
        "pk": 3521098765432109876,
        "media_type": 1,
        "taken_at": 1750000000,
        "caption": {"text": "A photo caption #tag"},
        "user": {"pk": 12345, "username": "someuser", "full_name": "Some User"},
        "like_count": 321,
        "comment_count": 12,
        "image_versions2": _candidates("https://cdn.example/img1.jpg"),
    }]
}

_CAROUSEL_PAYLOAD = {
    "items": [{
        "media_type": 8,
        "taken_at": 1750000000,
        "caption": {"text": "Carousel"},
        "user": {"pk": 12345, "username": "someuser", "full_name": ""},
        "like_count": 5,
        "carousel_media": [
            {"media_type": 1, "image_versions2": _candidates("https://cdn.example/c1.jpg")},
            {"media_type": 1, "image_versions2": _candidates("https://cdn.example/c2.jpg")},
            {"media_type": 1, "image_versions2": _candidates("https://cdn.example/c3.jpg")},
        ],
    }]
}


def _no_video_fail(url, item_id, verbose=False):
    return None, empty_fail("no_video", "No video formats found!")






def test_extract_image_urls_single():
    assert _extract_image_urls(_SINGLE_IMAGE_PAYLOAD) == ["https://cdn.example/img1.jpg"]






def test_extract_image_urls_carousel_preserves_order():
    assert _extract_image_urls(_CAROUSEL_PAYLOAD) == [
        "https://cdn.example/c1.jpg",
        "https://cdn.example/c2.jpg",
        "https://cdn.example/c3.jpg",
    ]






def test_extract_image_urls_mixed_carousel_skips_video():
    payload = {"items": [{
        "media_type": 8,
        "carousel_media": [
            {"media_type": 1, "image_versions2": _candidates("https://cdn.example/c1.jpg")},
            {"media_type": 2, "image_versions2": _candidates("https://cdn.example/vidthumb.jpg")},
            {"media_type": 1, "image_versions2": _candidates("https://cdn.example/c3.jpg")},
        ],
    }]}
    assert _extract_image_urls(payload) == [
        "https://cdn.example/c1.jpg", "https://cdn.example/c3.jpg"]






def test_extract_image_urls_empty_for_video_or_missing():
    assert _extract_image_urls({"items": [{"media_type": 2}]}) == []
    assert _extract_image_urls({"items": [{"media_type": 1}]}) == []
    assert _extract_image_urls({"items": []}) == []
    assert _extract_image_urls({}) == []






def test_row_from_media_info_shape_and_values():
    row = _row_from_media_info(_SINGLE_IMAGE_PAYLOAD, "DY1zHU_xQM2")
    info_row = instagram_dl._info_to_row({"timestamp": 1750000000}, "x")
    # Same column set as the yt-dlp path (map_to_canonical + the orchestrator's
    # >10-column success predicate rely on it).
    assert list(row.columns) == list(info_row.columns)
    assert row.shape == (1, 12)
    assert row.loc[0, 'item_id'] == "DY1zHU_xQM2"  # requested shortcode, not pk
    assert row.loc[0, 'desc'] == "A photo caption #tag"
    assert row.loc[0, 'author_id'] == "12345"
    assert row.loc[0, 'ig_author_handle'] == "someuser"
    assert row.loc[0, 'author_name_raw'] == "Some User"
    assert row.loc[0, 'duration_raw'] == -1
    assert row.loc[0, 'play_count_raw'] == -1  # image posts carry no play count
    assert row.loc[0, 'ig_like_count'] == 321
    assert row.loc[0, 'ig_comment_count'] == 12
    assert row.loc[0, 'video_downloaded'] == False  # noqa: E712






def test_row_from_media_info_username_fallback_and_missing_counts():
    row = _row_from_media_info(_CAROUSEL_PAYLOAD, "DY1zHU_xQM2")
    assert row.loc[0, 'author_name_raw'] == "someuser"  # full_name empty
    assert row.loc[0, 'ig_comment_count'] == -1  # absent → sentinel






def test_fetch_image_post_happy_path(monkeypatch):
    scraper = InstagramScraper()
    downloads = []
    monkeypatch.setattr(instagram_dl, "_extract_metadata", _no_video_fail)
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: (_CAROUSEL_PAYLOAD, None))
    monkeypatch.setattr(instagram_dl, "_download_images",
                        lambda urls, item_id, save_path, stream_to_bucket=None,
                        verbose=False: downloads.append(urls) or True)

    row = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert row.shape[0] == 1 and row.shape[1] > 10
    assert row.loc[0, 'video_downloaded'] == True  # noqa: E712
    assert row.loc[0, 'image_list'] == ("https://cdn.example/c1.jpg | "
                                        "https://cdn.example/c2.jpg | "
                                        "https://cdn.example/c3.jpg")
    assert 'media_error_type' not in row.attrs
    assert downloads == [[f"https://cdn.example/c{i}.jpg" for i in (1, 2, 3)]]
    # The orchestrator's slideshow gate: image_count reads the raw URL string.
    assert scraper.image_count(row.iloc[0]) == 3






def test_fetch_image_post_save_media_false(monkeypatch):
    scraper = InstagramScraper()
    monkeypatch.setattr(instagram_dl, "_extract_metadata", _no_video_fail)
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: (_SINGLE_IMAGE_PAYLOAD, None))
    monkeypatch.setattr(instagram_dl, "_download_images",
                        lambda *a, **kw: pytest.fail("must not download media"))

    row = scraper.fetch("DY1zHU_xQM2", save_media=False, save_path="")
    assert row.loc[0, 'video_downloaded'] == False  # noqa: E712
    assert row.loc[0, 'image_list'] == "https://cdn.example/img1.jpg"






def test_fetch_image_post_download_fail_is_transient_carousel(monkeypatch):
    scraper = InstagramScraper()
    monkeypatch.setattr(instagram_dl, "_extract_metadata", _no_video_fail)
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: (_CAROUSEL_PAYLOAD, None))
    monkeypatch.setattr(instagram_dl, "_download_images", lambda *a, **kw: False)

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "carousel"
    assert scraper.classify_error("carousel") == "transient:carousel"






def test_fetch_image_post_media_info_blocked_is_transient(monkeypatch):
    scraper = InstagramScraper()
    monkeypatch.setattr(instagram_dl, "_extract_metadata", _no_video_fail)
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: (None, "rate_limited"))

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "rate_limited"
    assert scraper.classify_error("rate_limited") == "transient:rate_limited"






def test_fetch_image_post_no_candidates_is_permanent_no_video(monkeypatch):
    scraper = InstagramScraper()
    monkeypatch.setattr(instagram_dl, "_extract_metadata", _no_video_fail)
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: ({"items": [{"media_type": 1}]}, None))

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "no_video"
    assert scraper.classify_error("no_video") == "permanent:no_video"






def test_fetch_image_post_endpoint_dead_is_permanent_no_video(monkeypatch):
    scraper = InstagramScraper()
    monkeypatch.setattr(instagram_dl, "_extract_metadata", _no_video_fail)
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: (None, "no_video"))

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "no_video"






def test_fetch_non_no_video_failures_pass_through(monkeypatch):
    """Only the no_video category routes to the image path."""
    scraper = InstagramScraper()
    monkeypatch.setattr(
        instagram_dl, "_extract_metadata",
        lambda url, item_id, verbose=False: (None, empty_fail("rate_limited", "429")))
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: pytest.fail("image path must not fire"))

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "rate_limited"






def test_fetch_video_path_untouched(monkeypatch):
    """A successful yt-dlp extraction never touches the image-post machinery."""
    scraper = InstagramScraper()
    info = {
        'id': '3521098765432109876', 'description': 'A reel', 'timestamp': 1750000000,
        'duration': 17.4, 'uploader_id': 'someuser', 'uploader': 'Some User',
        'view_count': 100, 'like_count': 10, 'comment_count': 1,
    }
    monkeypatch.setattr(instagram_dl, "_extract_metadata",
                        lambda url, item_id, verbose=False: (info, None))
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: pytest.fail("image path must not fire"))

    row = scraper.fetch("DY1zHU_xQM2", save_media=False, save_path="")
    assert row.loc[0, 'duration_raw'] == 17.4
    assert 'image_list' not in row.columns






class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content


def test_download_images_nn_names_and_partial_cleanup(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(instagram_dl.scraper_cookies, "requests_cookiejar",
                        lambda platform: None)
    monkeypatch.setattr(instagram_dl, "sleep", lambda s: None)

    # Happy path: 1-based, zero-padded consecutive NN names.
    monkeypatch.setattr(requests, "get",
                        lambda url, **kw: _FakeResponse(b"x" * 10))
    ok = instagram_dl._download_images(
        ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        "ITEM1", str(tmp_path))
    assert ok is True
    assert (tmp_path / "ITEM1_01.jpeg").exists()
    assert (tmp_path / "ITEM1_02.jpeg").exists()

    # Failure mid-set: the partial images written so far are removed.
    calls = []

    def _flaky_get(url, **kw):
        calls.append(url)
        if len(calls) > 1:
            raise OSError("connection reset")
        return _FakeResponse(b"y" * 10)

    monkeypatch.setattr(requests, "get", _flaky_get)
    ok = instagram_dl._download_images(
        ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        "ITEM2", str(tmp_path))
    assert ok is False
    assert not (tmp_path / "ITEM2_01.jpeg").exists()
    assert not (tmp_path / "ITEM2_02.jpeg").exists()






def test_instagram_slideshow_hooks_and_prepare_raw_batch():
    scraper = InstagramScraper()
    assert scraper.slideshow_image_column == "image_list"
    assert scraper.image_count(pd.Series({'image_list': "a | b"})) == 2
    assert scraper.image_count(pd.Series({'image_list': None})) == 0
    assert scraper.image_count(pd.Series({'item_id': "x"})) == 0

    df = pd.DataFrame({
        'image_list': ["a | b | c", None, None],
        'duration_raw': [-1.0, 17.4, -1.0],
    })
    out = scraper.prepare_raw_batch(df)
    # Image post: URL string → count, duration → count × 2s.
    assert out.loc[0, 'image_list'] == 3
    assert out.loc[0, 'duration_raw'] == 3 * SLIDESHOW_SECONDS_PER_IMAGE
    # Video rows: image_list 0, duration untouched / sentinel → NA.
    assert out.loc[1, 'image_list'] == 0
    assert out.loc[1, 'duration_raw'] == 17.4
    assert pd.isna(out.loc[2, 'duration_raw'])
    assert str(out['image_list'].dtype) == "int64[pyarrow]"






def test_media_info_counts_wrapper_still_gated(monkeypatch):
    from fyp.fyp_config import fyp_cf

    called = []
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: called.append(1) or (_SINGLE_IMAGE_PAYLOAD, None))

    fyp_cf['misc']['ig_fetch_view_counts'] = False
    try:
        assert instagram_dl._fetch_media_info_counts("DW9rrgZy6nH") is None
        assert not called  # the gate short-circuits before the endpoint
    finally:
        del fyp_cf['misc']['ig_fetch_view_counts']

    counts = instagram_dl._fetch_media_info_counts("DW9rrgZy6nH")
    assert called == [1]
    assert counts == {'play_count': None, 'like_count': 321, 'comment_count': 12}
