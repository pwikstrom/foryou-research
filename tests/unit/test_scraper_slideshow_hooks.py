#!/usr/bin/env python3
"""Tests for the BaseScraper slideshow hooks and the TikTok implementations.

Covers image_count parsing, prepare_raw_batch (URL string → count, slideshow
duration override, zero-duration → NA), the retryable carousel error
classification, and the zero-effort defaults a carousel-less platform inherits.

Usage:
    python tests/unit/test_scraper_slideshow_hooks.py
    pytest tests/unit/test_scraper_slideshow_hooks.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from fyp.platform_scraper import SLIDESHOW_SECONDS_PER_IMAGE, BaseScraper
from fyp.tiktok_dl import _RETRYABLE, TikTokScraper




def test_tiktok_image_count():
    scraper = TikTokScraper()
    assert scraper.image_count(pd.Series({'image_list': 'u1 | u2 | u3'})) == 3
    assert scraper.image_count(pd.Series({'image_list': ''})) == 0
    assert scraper.image_count(pd.Series({'image_list': pd.NA})) == 0
    assert scraper.image_count(pd.Series({'other_col': 'x'})) == 0
    print("PASS: TikTok image_count")




def test_tiktok_prepare_raw_batch():
    scraper = TikTokScraper()
    df = pd.DataFrame({
        'item_id': ['vid', 'carousel', 'zero'],
        'image_list': ['', 'u1 | u2 | u3 | u4', ''],
        'video_duration': [30, 0, 0],
    })
    out = scraper.prepare_raw_batch(df)

    assert list(out['image_list']) == [0, 4, 0]
    assert str(out['image_list'].dtype) == 'int64[pyarrow]'
    assert out.loc[0, 'video_duration'] == 30
    assert out.loc[1, 'video_duration'] == 4 * SLIDESHOW_SECONDS_PER_IMAGE
    assert pd.isna(out.loc[2, 'video_duration'])
    print("PASS: TikTok prepare_raw_batch")




def test_carousel_error_is_transient():
    scraper = TikTokScraper()
    assert "carousel" in _RETRYABLE
    assert scraper.classify_error("carousel") == "transient:carousel"
    assert scraper.classify_error(None) == "ok"
    assert scraper.classify_error("removed") == "permanent:removed"
    print("PASS: carousel error classification")




def test_base_defaults_for_carousel_less_platform():
    """A platform without a carousel concept inherits safe no-op defaults."""

    class DummyScraper(BaseScraper):
        platform = None  # base fields only; not a registered real platform

        def item_url(self, item_id: str) -> str:
            return item_id

        def fetch(self, item_id, *, save_media, save_path,
                  stream_to_bucket=None, verbose=False):
            return pd.DataFrame()

        def map_to_canonical(self, raw):
            return raw

        def classify_error(self, error_type):
            return "ok" if error_type is None else f"transient:{error_type}"

        def repair_counts(self, df):
            return df

    try:
        dummy = DummyScraper()
        assert dummy.slideshow_image_column is None
        assert dummy.image_count(pd.Series({'image_list': 'u1 | u2'})) == 0
        df = pd.DataFrame({'a': [1, 2]})
        assert dummy.prepare_raw_batch(df) is df
        assert dummy.fetch_slideshow_audio("id", "/tmp") is None
        print("PASS: BaseScraper carousel defaults")
    finally:
        # __init_subclass__ auto-registers every subclass; keep the shared
        # registry clean for other tests / get_scraper callers.
        BaseScraper._registry.remove(DummyScraper)




if __name__ == "__main__":
    test_tiktok_image_count()
    test_tiktok_prepare_raw_batch()
    test_carousel_error_is_transient()
    test_base_defaults_for_carousel_less_platform()
    print("All slideshow hook tests passed.")
