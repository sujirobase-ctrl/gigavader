"""Regression tests for resolver changes ported from userscript 3.22."""

from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup

import src.resolvers as resolvers
from src.config import USER_AGENT
from src.parsers.host_parser import parse_hosts
from src.resolvers import (
    _gofile_website_token,
    _resolve_direct,
    _resolve_gofile,
    _resolve_turbo,
)


class _Response:
    def __init__(self, payload=None, *, status=200, text=""):
        self._payload = payload or {}
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


class _GoFileSession:
    def __init__(self):
        import requests

        self.cookies = requests.cookies.RequestsCookieJar()
        self.content_calls = []

    def post(self, url, **_kwargs):
        assert url.endswith("/accounts")
        return _Response({"status": "ok", "data": {"token": "guest-token"}})

    def get(self, url, params=None, headers=None, **_kwargs):
        if url.endswith("/accounts/website"):
            return _Response({"status": "ok"})
        content_id = url.rsplit("/", 1)[-1]
        self.content_calls.append((content_id, params or {}, headers or {}))
        if content_id == "root":
            return _Response(
                {
                    "status": "ok",
                    "data": {
                        "type": "folder",
                        "name": "Root",
                        "children": {
                            "one": {"type": "file", "id": "1", "name": "one.mp4", "link": "https://store.gofile.io/one.mp4"},
                            "nested": {"type": "folder", "id": "child"},
                        },
                    },
                }
            )
        if content_id == "child":
            return _Response(
                {
                    "status": "ok",
                    "data": {
                        "type": "folder",
                        "children": {
                            "two": {"type": "file", "id": "2", "name": "two.zip", "directLink": "https://store.gofile.io/download/direct/two.zip"}
                        },
                    },
                }
            )
        return _Response({"status": "error-notFound"})


class _GoFileClient:
    timeout = 30

    def __init__(self):
        self.session = _GoFileSession()


def test_gofile_dynamic_token_and_recursive_folder(monkeypatch):
    monkeypatch.setattr(resolvers, "_gofile_salt_cache", resolvers.GOFILE_DEFAULT_SALT)
    client = _GoFileClient()
    out = _resolve_gofile("https://gofile.io/d/root", client)
    assert sorted(item.filename for item in out) == ["one.mp4", "two.zip"]
    assert all(item.subfolder == "Root" for item in out)
    _, params, headers = client.session.content_calls[0]
    assert params["pageSize"] == 1000
    assert headers["Authorization"] == "Bearer guest-token"
    assert headers["X-Website-Token"] == _gofile_website_token(
        "guest-token", salt=resolvers.GOFILE_DEFAULT_SALT
    )


def test_gofile_token_formula_is_stable_for_bucket():
    now = 14_400 * 123
    expected = hashlib.sha256(
        f"{USER_AGENT}::en-US::token::123::{resolvers.GOFILE_DEFAULT_SALT}".encode()
    ).hexdigest()
    assert _gofile_website_token("token", salt=resolvers.GOFILE_DEFAULT_SALT, now=now) == expected


def test_gofile_reads_literal_salt_without_running_javascript(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(resolvers, "_gofile_salt_cache", None)
    source = "throw Error('must never execute'); function generateWT(){return '::example_salt123';}"
    client = SimpleNamespace(timeout=30, get=lambda *args, **kwargs: SimpleNamespace(text=source))
    assert resolvers._discover_gofile_salt(client) == "example_salt123"


def test_gofile_obfuscated_script_falls_back(monkeypatch, caplog):
    from types import SimpleNamespace
    monkeypatch.setattr(resolvers, "_gofile_salt_cache", None)
    client = SimpleNamespace(timeout=30, get=lambda *args, **kwargs: SimpleNamespace(text="function generateWT(){}"))
    assert resolvers._discover_gofile_salt(client) == resolvers.GOFILE_DEFAULT_SALT
    assert "script format changed" in caplog.text


def test_new_host_variants_are_detected():
    html = " ".join(
        [
            '<a href="https://filester.gg/d/ABC">f</a>',
            '<a href="https://pixeldrain.net/u/XYZ">p</a>',
            '<a href="https://turbo.cr/v/VID">t</a>',
            '<a href="https://coomer.st/site/user/name">c</a>',
        ]
    )
    urls = {match.url for match in parse_hosts(html)}
    assert "https://filester.gg/d/ABC" in urls
    assert "https://pixeldrain.net/u/XYZ" in urls
    assert "https://turbo.cr/v/VID" in urls
    assert "https://coomer.st/site/user/name" in urls


def test_goonbox_medium_thumbnail_is_normalized_to_original():
    medium = (
        "https://goonbox.cr/images/"
        "2310x4096_7ed90ec0c9551c8d3af385cb85f2e012.md.jpg"
    )
    original = _resolve_direct(medium, object())[0]
    assert original.url.endswith(
        "2310x4096_7ed90ec0c9551c8d3af385cb85f2e012.jpg"
    )
    assert original.filename.endswith(".jpg")
    assert ".md.jpg" not in original.filename


def test_reddit_image_uses_media_headers_without_browser_cookies():
    media = _resolve_direct("https://i.redd.it/drfy08tqb89g1.jpeg", object())[0]
    assert media.headers is not None
    assert media.headers["Accept"].startswith("image/")
    assert media.headers["Referer"] == "https://www.reddit.com/"
    assert media.headers["Cookie"] == ""


class _TurboSession:
    def get(self, url, **_kwargs):
        if "/api/sign" in url:
            return _Response(
                {
                    "success": True,
                    "url": "https://turbocdn.st/video.mp4?token=x",
                    "original_filename": "Original Video.mp4",
                }
            )
        return _Response({}, status=404)


class _TurboClient:
    timeout = 30
    session = _TurboSession()

    def get_soup(self, _url):
        return BeautifulSoup("", "html.parser")


def test_turbo_v_link_uses_signed_endpoint():
    out = _resolve_turbo("https://turbo.cr/v/ABC", _TurboClient())
    assert len(out) == 1
    assert out[0].filename == "Original Video.mp4"
    assert out[0].url.startswith("https://turbocdn.st/video.mp4")
