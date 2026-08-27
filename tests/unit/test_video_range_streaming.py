"""Regression tests for the media stream's Range handling.

Prod incident 2026-08-27: four 500s on ``/api/video/standard_study/<id>`` for
a 33.1 MiB item. The route honoured whatever end offset the client named, so a
``Range: bytes=0-34711466`` header produced a 33.1 MiB fixed-length body —
past Cloud Run's 32 MiB non-chunked response cap, which the platform drops and
logs as a 500 without the app ever seeing an error. ~2.5% of the media corpus
is over that cap.

These tests pin the two properties that keep a reply under it: every 206 is
capped at ``MAX_RANGE_CHUNK`` regardless of what was asked for, and a
whole-object reply omits Content-Length (so it goes out chunked) once it would
cross the cap. They also pin the Range forms that previously raised — a suffix
range and a multi-range list both used to reach ``int('')``.
"""

import io

import pytest

_TEST_VIEWER = "__range_test_viewer__"

# The item from the incident, and its real size in GCS.
_BIG_ITEM = "7465847170017529134"
_BIG_SIZE = 34711467  # 33.1 MiB


# --------------------------------------------------------------- unit tests

def test_explicit_end_is_capped_to_the_chunk():
    """The incident's request: a client naming the whole file as its range."""
    from web_interface.routes.api_viewer_routes import (
        MAX_RANGE_CHUNK, RESPONSE_SIZE_CAP, _parse_byte_range)

    start, end = _parse_byte_range(f"bytes=0-{_BIG_SIZE - 1}", _BIG_SIZE)
    assert (start, end) == (0, MAX_RANGE_CHUNK - 1)
    assert end - start + 1 < RESPONSE_SIZE_CAP


def test_open_ended_and_mid_file_ranges():
    from web_interface.routes.api_viewer_routes import MAX_RANGE_CHUNK, _parse_byte_range

    assert _parse_byte_range("bytes=0-", _BIG_SIZE) == (0, MAX_RANGE_CHUNK - 1)
    assert _parse_byte_range("bytes=100-199", _BIG_SIZE) == (100, 199)
    # A range running past the end is clamped to the last byte, not rejected.
    assert _parse_byte_range(f"bytes={_BIG_SIZE - 10}-{_BIG_SIZE + 500}",
                             _BIG_SIZE) == (_BIG_SIZE - 10, _BIG_SIZE - 1)


def test_suffix_range_is_served_not_crashed():
    """``bytes=-N`` asks for the last N bytes — mp4 players use it for moov."""
    from web_interface.routes.api_viewer_routes import _parse_byte_range

    assert _parse_byte_range("bytes=-500", _BIG_SIZE) == (_BIG_SIZE - 500, _BIG_SIZE - 1)
    # A suffix longer than the object is the whole object, still chunk-capped.
    start, end = _parse_byte_range("bytes=-999999999", _BIG_SIZE)
    assert start == 0


def test_multi_range_takes_the_first_span():
    from web_interface.routes.api_viewer_routes import _parse_byte_range

    assert _parse_byte_range("bytes=0-99, 200-299", _BIG_SIZE) == (0, 99)


@pytest.mark.parametrize("header", [
    "bytes=abc-def",      # not numbers
    "bytes=-",            # no offsets at all
    "bytes=-0",           # zero-length suffix
    "items=0-100",        # unsupported unit
    "0-100",              # no unit
    "bytes=200-100",      # end before start
    f"bytes={_BIG_SIZE}-",  # first byte past the end
])
def test_unusable_headers_are_ignored_not_raised(header):
    """Previously these reached ``int('')`` and returned an unhandled 500."""
    from web_interface.routes.api_viewer_routes import _parse_byte_range

    assert _parse_byte_range(header, _BIG_SIZE) is None


# -------------------------------------------------------- route integration

class _FakeBlob:
    def __init__(self, size):
        self.size = size

    def download_as_bytes(self, start=0, end=None):
        return b"\0" * (end - start + 1)

    def open(self, mode="rb"):
        # The whole-object branch only decides on Content-Length from the
        # resolved size, so a short body keeps the test cheap.
        return io.BytesIO(b"\0" * 1024)


class _FakeBucket:
    def __init__(self, size):
        self._size = size

    def blob(self, name):
        return _FakeBlob(self._size)


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role=ROLE_VIEWER,
                        password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def _serve(monkeypatch, client, size):
    """Wire the eval stream path onto a fake blob of ``size`` bytes."""
    from web_interface import auth
    from web_interface.routes import api_viewer_routes as viewer

    monkeypatch.setattr(auth.role_manager, "get_role_permissions",
                        lambda role: ["tab.admin.ab_eval"])
    monkeypatch.setattr(viewer.media_paths, "resolve_media",
                        lambda item_id, platform=None: {
                            "kind": "gcs", "blob_name": f"media/{item_id}.mp4",
                            "size": size})
    monkeypatch.setattr(viewer, "fyp_cf",
                        {"data_io": {"use_gcs_for_media": True,
                                     "bucket": _FakeBucket(size)}})
    viewer._EVAL_ACCESS_CACHE.clear()
    with client.session_transaction() as sess:
        sess["_user_id"] = _TEST_VIEWER
        sess["_fresh"] = True


def test_oversized_range_request_returns_a_capped_206(client, monkeypatch):
    from web_interface.routes.api_viewer_routes import MAX_RANGE_CHUNK, RESPONSE_SIZE_CAP

    _serve(monkeypatch, client, _BIG_SIZE)
    res = client.get(f"/api/video/eval/{_BIG_ITEM}",
                     headers={"Range": f"bytes=0-{_BIG_SIZE - 1}"})

    assert res.status_code == 206
    assert int(res.headers["Content-Length"]) == MAX_RANGE_CHUNK
    assert int(res.headers["Content-Length"]) < RESPONSE_SIZE_CAP
    assert res.headers["Content-Range"] == f"bytes 0-{MAX_RANGE_CHUNK - 1}/{_BIG_SIZE}"


def test_whole_object_over_the_cap_goes_out_chunked(client, monkeypatch):
    """No Content-Length above the cap — that is what buys the exemption."""
    _serve(monkeypatch, client, _BIG_SIZE)
    res = client.get(f"/api/video/eval/{_BIG_ITEM}")

    assert res.status_code == 200
    assert "Content-Length" not in res.headers
    assert res.headers["Accept-Ranges"] == "bytes"


def test_whole_object_under_the_cap_still_declares_its_length(client, monkeypatch):
    _serve(monkeypatch, client, 2048)
    res = client.get("/api/video/eval/small")

    assert res.status_code == 200
    assert res.headers["Content-Length"] == "2048"
