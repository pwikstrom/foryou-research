"""Static scripts and stylesheets are referenced through ``asset_url()``.

``asset_url`` stamps each URL with a hash of the file's content, replacing the
hand-maintained ``?v=N`` suffixes (which were easy to forget, and whose
hand-editing once broke a start tag in production).
"""

import hashlib
import re
from pathlib import Path

from flask import Flask

from web_interface.static_assets import asset_url

WEB = Path(__file__).resolve().parents[2] / "web_interface"
TEMPLATES = sorted((WEB / "templates").rglob("*.html"))
_STATIC_URL_FOR = re.compile(
    r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"][^'"]+\.(?:js|css)['"]"""
)
_ASSET_REF = re.compile(r"""asset_url\(\s*['"]([^'"]+)['"]\s*\)""")


def test_templates_carry_no_hand_versioned_assets():
    offenders = [
        str(p.relative_to(WEB)) for p in TEMPLATES if "?v=" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"hand-maintained ?v= in: {offenders} — use asset_url()"


def test_scripts_and_stylesheets_use_asset_url():
    offenders = [
        str(p.relative_to(WEB))
        for p in TEMPLATES
        if _STATIC_URL_FOR.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"url_for('static') for JS/CSS in: {offenders} — use asset_url()"


def test_every_referenced_asset_exists():
    missing = sorted(
        {
            name
            for p in TEMPLATES
            for name in _ASSET_REF.findall(p.read_text(encoding="utf-8"))
            if not (WEB / "static" / name).is_file()
        }
    )
    assert not missing, f"asset_url() references missing static files: {missing}"


def _app(static_dir: Path) -> Flask:
    return Flask(__name__, static_folder=str(static_dir), static_url_path="/static")


def test_asset_url_is_stamped_with_the_content_hash(tmp_path):
    js = tmp_path / "x.js"
    js.write_text("one", encoding="utf-8")
    app = _app(tmp_path)
    with app.test_request_context():
        first = asset_url("x.js")
        assert first == f"/static/x.js?v={hashlib.sha256(b'one').hexdigest()[:10]}"
        assert asset_url("x.js") == first

        js.write_text("two, longer", encoding="utf-8")
        second = asset_url("x.js")
        assert second == f"/static/x.js?v={hashlib.sha256(b'two, longer').hexdigest()[:10]}"


def test_asset_url_degrades_to_a_plain_url_for_a_missing_file(tmp_path):
    with _app(tmp_path).test_request_context():
        assert asset_url("nope.js") == "/static/nope.js"


def _caching_app(static_dir: Path) -> Flask:
    from web_interface.static_assets import cache_hashed_static

    app = _app(static_dir)
    app.after_request(cache_hashed_static)
    return app


def test_a_url_whose_hash_matches_is_cached_as_immutable(tmp_path):
    (tmp_path / "x.js").write_text("one", encoding="utf-8")
    app = _caching_app(tmp_path)
    with app.test_request_context():
        url = asset_url("x.js")
    res = app.test_client().get(url)
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_a_stale_or_missing_hash_keeps_revalidation(tmp_path):
    """A request for a hash this instance does not serve must not pin its bytes."""
    (tmp_path / "x.js").write_text("one", encoding="utf-8")
    client = _caching_app(tmp_path).test_client()
    for url in ("/static/x.js?v=0123456789", "/static/x.js"):
        res = client.get(url)
        assert res.status_code == 200
        assert "immutable" not in res.headers.get("Cache-Control", "")
    assert "immutable" not in client.get("/static/absent.js?v=0123456789").headers.get(
        "Cache-Control", ""
    )


def test_the_real_app_marks_its_hashed_assets_immutable():
    from web_interface.fyp_data_hub import app

    with app.test_request_context():
        url = asset_url("style.css")
    res = app.test_client().get(url)
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "public, max-age=31536000, immutable"
