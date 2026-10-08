"""Finished media moves out of the temp folder even across filesystems.

The temp folder is ``tempfile.gettempdir()/fyp``. On a fresh Ubuntu install
``/tmp`` is a tmpfs, so it sits on another filesystem than the local media
folder under the home directory, and a bare ``os.replace`` fails there with
``EXDEV`` ("Invalid cross-device link"). Both media moves (yt-dlp downloads
and assembled carousel slideshows) go through ``data_io.move_local_file``,
which falls back to copy-then-swap in that case.

These tests simulate the cross-device error: ``os.replace`` raises ``EXDEV``
whenever source and target are in different directories, which is how the
real failure looks to the code.
"""

import errno
import os
from unittest.mock import patch

import pandas as pd
import pytest

import fyp.core.data_io as data_io
from fyp.scrape import scrape
from fyp.scrape.platform_scraper import store_media_file

_real_replace = os.replace


def _cross_device_replace(src, dst):
    if os.path.dirname(os.path.abspath(src)) != os.path.dirname(os.path.abspath(dst)):
        raise OSError(errno.EXDEV, "Invalid cross-device link", src, None, dst)
    return _real_replace(src, dst)


@pytest.fixture
def cross_device():
    with patch.object(os, "replace", _cross_device_replace):
        yield


@pytest.fixture
def dirs(tmp_path):
    temp, media = tmp_path / "temp", tmp_path / "media"
    temp.mkdir()
    media.mkdir()
    return temp, media


def _leftovers(directory):
    return [n for n in os.listdir(directory) if n.endswith(".tmp")]


def test_same_filesystem_move_replaces_the_target(dirs):
    temp, media = dirs
    src, dst = temp / "a.mp4", media / "a.mp4"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")

    data_io.move_local_file(str(src), str(dst))

    assert dst.read_bytes() == b"new"
    assert not src.exists()


def test_cross_filesystem_move_copies_swaps_and_removes_source(dirs, cross_device):
    temp, media = dirs
    src, dst = temp / "a.mp4", media / "a.mp4"
    src.write_bytes(b"x" * 100_000)
    dst.write_bytes(b"old")

    data_io.move_local_file(str(src), str(dst))

    assert dst.read_bytes() == b"x" * 100_000
    assert not src.exists()
    assert _leftovers(media) == []


def test_failed_cross_filesystem_copy_leaves_both_files(dirs, cross_device):
    temp, media = dirs
    src, dst = temp / "a.mp4", media / "a.mp4"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")

    with (
        patch.object(data_io.shutil, "copy2", side_effect=OSError(errno.ENOSPC, "No space")),
        pytest.raises(OSError, match="No space"),
    ):
        data_io.move_local_file(str(src), str(dst))

    assert dst.read_bytes() == b"old"
    assert src.read_bytes() == b"new"
    assert _leftovers(media) == []


def test_other_replace_errors_are_not_swallowed(dirs):
    temp, media = dirs
    with pytest.raises(FileNotFoundError):
        data_io.move_local_file(str(temp / "missing.mp4"), str(media / "missing.mp4"))


def test_store_media_file_moves_a_download_across_filesystems(dirs, cross_device):
    temp, media = dirs
    downloaded = temp / "123.mp4"
    downloaded.write_bytes(b"video")

    store_media_file(str(downloaded), str(media), "123.mp4", stream_to_bucket=None)

    assert (media / "123.mp4").read_bytes() == b"video"
    assert not downloaded.exists()


class _CarouselScraper:
    platform = "tiktok"

    def fetch(self, video_id, **_kwargs):
        return pd.DataFrame([{"item_id": video_id, "video_downloaded": True}])

    def image_count(self, _row):
        return 2

    def fetch_slideshow_audio(self, _video_id, _temp_dir):
        return None


def test_local_slideshow_moves_across_filesystems(dirs, cross_device):
    temp, media = dirs
    for n in (1, 2):
        (media / f"777_{n:02}.jpeg").write_bytes(b"j" * 500)

    def _fake_slideshow(image_files, output, **_kwargs):
        assert len(image_files) == 2
        with open(output, "wb") as fh:
            fh.write(b"s" * 500)

    cfg = {
        "data_io": {"use_gcs_for_media": False, "bucket": None, "gcs_media_prefix": "media"},
        "misc": {"min_media_object_size": 100},
        "paths": {"temp": str(temp)},
    }
    with (
        patch.object(scrape, "_cf", return_value=cfg),
        patch.object(scrape.media_paths, "ensure_local_platform_dir", return_value=str(media)),
        patch.object(scrape, "make_slideshow", _fake_slideshow),
    ):
        result = scrape.download_single_video(
            "777", verbose=False, scraper=_CarouselScraper(), platform="tiktok"
        )

    assert isinstance(result, pd.DataFrame), f"download failed, returned {result!r}"
    assert bool(result.loc[0, "video_downloaded"]) is True
    assert (media / "777.mp4").read_bytes() == b"s" * 500
    assert not (temp / "777.mp4").exists()
    assert sorted(os.listdir(media)) == ["777.mp4"]
