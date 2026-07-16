"""Tests for Instagram image-only posts → slideshow flow (no network).

Covers image-URL extraction from yt-dlp info dicts (single image, carousel,
mixed carousel), the ``fetch()`` image-post branch (happy path,
save_media=False, transient/permanent failure routing), the image
downloader's NN-naming and partial-set cleanup, and the slideshow hooks
(``slideshow_image_column``, ``image_count``, ``prepare_raw_batch``
count/duration conversion).
"""

import pandas as pd
import pytest

from fyp.scrape import instagram_dl
from fyp.scrape.instagram_dl import (
    InstagramScraper,
    _best_thumbnail_url,
    _image_urls_from_info,
)
from fyp.scrape.platform_scraper import SLIDESHOW_SECONDS_PER_IMAGE


def _thumbs(url: str) -> list[dict]:
    # yt-dlp emits reversed candidates: last is the largest rendition.
    return [{"url": url + "?small"}, {"url": url + "?mid"}, {"url": url}]


_COMMON = {
    'description': 'A photo caption #tag',
    'timestamp': 1750000000,
    'uploader_id': '12345',
    'channel': 'someuser',
    'uploader': 'Some User',
    'like_count': 321,
    'comment_count': 12,
}

_SINGLE_IMAGE_INFO = {
    'id': '3521098765432109876',
    **_COMMON,
    'formats': [],
    'thumbnails': _thumbs("https://cdn.example/img1.jpg"),
}

_CAROUSEL_INFO = {
    'id': '3521098765432109876',
    '_type': 'playlist',
    **_COMMON,
    'entries': [
        {'id': 'e1', 'formats': [], 'thumbnails': _thumbs("https://cdn.example/c1.jpg")},
        {'id': 'e2', 'formats': [], 'thumbnails': _thumbs("https://cdn.example/c2.jpg")},
        {'id': 'e3', 'formats': [], 'thumbnails': _thumbs("https://cdn.example/c3.jpg")},
    ],
}

_VIDEO_INFO = {
    'id': '3521098765432109876',
    **_COMMON,
    'duration': 17.4,
    'view_count': 100,
    'formats': [{'url': 'https://cdn.example/v.mp4', 'format_id': '0'}],
    'thumbnails': _thumbs("https://cdn.example/poster.jpg"),
}






def test_best_thumbnail_prefers_width_then_order():
    # Widths present → largest width wins regardless of order.
    media = {'thumbnails': [{'url': 'a', 'width': 1080}, {'url': 'b', 'width': 240}]}
    assert _best_thumbnail_url(media) == 'a'
    # No widths → last entry (yt-dlp reverses candidates; largest is last).
    assert _best_thumbnail_url(_SINGLE_IMAGE_INFO) == "https://cdn.example/img1.jpg"
    assert _best_thumbnail_url({'thumbnails': []}) is None
    assert _best_thumbnail_url({}) is None






def test_image_urls_single():
    assert _image_urls_from_info(_SINGLE_IMAGE_INFO) == ["https://cdn.example/img1.jpg"]






def test_image_urls_carousel_preserves_order():
    assert _image_urls_from_info(_CAROUSEL_INFO) == [
        "https://cdn.example/c1.jpg",
        "https://cdn.example/c2.jpg",
        "https://cdn.example/c3.jpg",
    ]






def test_image_urls_mixed_carousel_skips_video_segments():
    info = dict(_CAROUSEL_INFO)
    info['entries'] = [
        _CAROUSEL_INFO['entries'][0],
        {'id': 'ev', 'formats': [{'url': 'v.mp4'}], 'duration': 9.0,
         'thumbnails': _thumbs("https://cdn.example/vidthumb.jpg")},
        _CAROUSEL_INFO['entries'][2],
    ]
    assert _image_urls_from_info(info) == [
        "https://cdn.example/c1.jpg", "https://cdn.example/c3.jpg"]






def test_image_urls_empty_for_video_posts():
    # A plain video (formats present) never yields image URLs, even though it
    # carries poster thumbnails.
    assert _image_urls_from_info(_VIDEO_INFO) == []
    # Defensive: a format-less video (known duration) is still not an image.
    broken_video = {**_VIDEO_INFO, 'formats': []}
    assert _image_urls_from_info(broken_video) == []
    assert _image_urls_from_info({}) == []






def test_fetch_image_post_happy_path(monkeypatch):
    scraper = InstagramScraper()
    downloads = []
    monkeypatch.setattr(instagram_dl, "_extract_metadata",
                        lambda url, item_id, verbose=False: (_CAROUSEL_INFO, None))
    monkeypatch.setattr(instagram_dl, "_download_images",
                        lambda urls, item_id, save_path, stream_to_bucket=None,
                        verbose=False: downloads.append(urls) or True)

    row = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert row.shape[0] == 1 and row.shape[1] > 10
    assert row.loc[0, 'item_id'] == "DY1zHU_xQM2"  # requested shortcode, not pk
    assert row.loc[0, 'desc'] == "A photo caption #tag"
    assert row.loc[0, 'ig_like_count'] == 321
    assert row.loc[0, 'duration_raw'] == -1  # prepare_raw_batch overrides later
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
    monkeypatch.setattr(instagram_dl, "_extract_metadata",
                        lambda url, item_id, verbose=False: (_SINGLE_IMAGE_INFO, None))
    monkeypatch.setattr(instagram_dl, "_download_images",
                        lambda *a, **kw: pytest.fail("must not download media"))

    row = scraper.fetch("DY1zHU_xQM2", save_media=False, save_path="")
    assert row.loc[0, 'video_downloaded'] == False  # noqa: E712
    assert row.loc[0, 'image_list'] == "https://cdn.example/img1.jpg"






def test_fetch_image_post_download_fail_is_transient_carousel(monkeypatch):
    scraper = InstagramScraper()
    monkeypatch.setattr(instagram_dl, "_extract_metadata",
                        lambda url, item_id, verbose=False: (_CAROUSEL_INFO, None))
    monkeypatch.setattr(instagram_dl, "_download_images", lambda *a, **kw: False)

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "carousel"
    assert scraper.classify_error("carousel") == "transient:carousel"






def test_fetch_all_video_carousel_is_permanent_no_video(monkeypatch):
    scraper = InstagramScraper()
    info = dict(_CAROUSEL_INFO)
    info['entries'] = [{'id': 'ev', 'formats': [{'url': 'v.mp4'}], 'duration': 9.0,
                        'thumbnails': _thumbs("https://cdn.example/vt.jpg")}]
    monkeypatch.setattr(instagram_dl, "_extract_metadata",
                        lambda url, item_id, verbose=False: (info, None))

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "no_video"
    assert scraper.classify_error("no_video") == "permanent:no_video"






def test_fetch_metadata_failures_pass_through(monkeypatch):
    from fyp.scrape.platform_scraper import empty_fail

    scraper = InstagramScraper()
    monkeypatch.setattr(
        instagram_dl, "_extract_metadata",
        lambda url, item_id, verbose=False: (None, empty_fail("rate_limited", "429")))

    res = scraper.fetch("DY1zHU_xQM2", save_media=True, save_path="/tmp/x")
    assert res.empty
    assert res.attrs['error_type'] == "rate_limited"






def test_fetch_video_path_untouched(monkeypatch):
    """A video post (formats present) never touches the image-post machinery."""
    scraper = InstagramScraper()
    monkeypatch.setattr(instagram_dl, "_extract_metadata",
                        lambda url, item_id, verbose=False: (_VIDEO_INFO, None))
    monkeypatch.setattr(instagram_dl, "_download_images",
                        lambda *a, **kw: pytest.fail("image path must not fire"))

    row = scraper.fetch("DY1zHU_xQM2", save_media=False, save_path="")
    assert row.loc[0, 'duration_raw'] == 17.4
    assert 'image_list' not in row.columns






def test_follow_gated_classifies_permanent_private():
    exc = instagram_dl.ExtractorError(
        "This content is only available for registered users who follow this account")
    category, _ = instagram_dl._classify_error(exc)
    assert category == "private"
    assert InstagramScraper().classify_error(category) == "permanent:private"






def test_anonymous_rate_limit_redirect_classifies_rate_limited():
    exc = instagram_dl.ExtractorError(
        "The webpage request was redirected to the login page. You have "
        "exceeded the rate-limit for accessing posts anonymously")
    category, _ = instagram_dl._classify_error(exc)
    assert category == "rate_limited"






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

    payload = {"items": [{"media_type": 1, "like_count": 321, "comment_count": 12}]}
    called = []
    monkeypatch.setattr(instagram_dl, "_fetch_media_info_payload",
                        lambda item_id: called.append(1) or (payload, None))

    fyp_cf['misc']['ig_fetch_view_counts'] = False
    try:
        assert instagram_dl._fetch_media_info_counts("DW9rrgZy6nH") is None
        assert not called  # the gate short-circuits before the endpoint
    finally:
        del fyp_cf['misc']['ig_fetch_view_counts']

    counts = instagram_dl._fetch_media_info_counts("DW9rrgZy6nH")
    assert called == [1]
    assert counts == {'play_count': None, 'like_count': 321, 'comment_count': 12}
