"""Offline tests for the bunkr resolver (_resolve_bunkr).

Mocks HttpClient so no network is touched. Locks the sign flow:
  page HTML -> numeric id -> POST /api/_001_v2 -> GET sign -> signed CDN URL.
"""
from __future__ import annotations

import sys
import os
import base64
import pytest
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from src.resolvers import _resolve_bunkr

FILE_PAGE = (
    '<html><body>'
    '<a id="download-btn" href="#" data-id="33864542">Download</a>'
    '<script>var x = "/file/33864542";</script>'
    '</body></html>'
)
ALBUM_PAGE = (
    '<html><body><h1>My Album</h1>'
    '<a href="/f/AAAA1111"></a><a href="/f/BBBB2222"></a>'
    '</body></html>'
)
FILE_A = '<html><body><script>var x = "/file/11111111";</script></body></html>'
FILE_B = '<html><body><script>var x = "/file/22222222";</script></body></html>'


class _FakeResp:
    def __init__(self, text="", json_data=None):
        self.text = text
        self._json = json_data
    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, by_id, sign_data):
        self._by_id = by_id
        self._sign = sign_data

    def get(self, url, **_):
        if "/a/" in url:
            return _FakeResp(text=ALBUM_PAGE)
        if "/f/AAAA1111" in url:
            return _FakeResp(text=FILE_A)
        if "/f/BBBB2222" in url:
            return _FakeResp(text=FILE_B)
        if "glb-apisign.cdn.cr/sign" in url:
            return _FakeResp(json_data=self._sign)
        return _FakeResp(text=FILE_PAGE)

    def post(self, url, json=None, **_):
        fid = (json or {}).get("id", "")
        return _FakeResp(json_data=self._by_id.get(fid))


class _FakeClient:
    def __init__(self, by_id, sign_data):
        self.session = _FakeSession(by_id, sign_data)
        self.timeout = 30
    def get_soup(self, url):
        if "/a/" in url: return BeautifulSoup(ALBUM_PAGE, "html.parser")
        if "/f/AAAA1111" in url: return BeautifulSoup(FILE_A, "html.parser")
        if "/f/BBBB2222" in url: return BeautifulSoup(FILE_B, "html.parser")
        return BeautifulSoup(FILE_PAGE, "html.parser")


def _meta(mediafiles, path, original):
    return {"mediafiles": mediafiles, "path": path, "original": original}


def test_single_file_page():
    client = _FakeClient(
        {"33864542": _meta("https://c5bu-b.cdn.cr", "/storage/media/x.mp3", "song.mp3")},
        {"token": "tok", "ex": "999"},
    )
    out = _resolve_bunkr("https://bunkr.cr/f/fixtureSlug", client)
    assert len(out) == 1, out
    m = out[0]
    assert m.filename == "song.mp3"
    assert m.url.startswith("https://c5bu-b.cdn.cr/storage/media/x.mp3?token=tok&ex=999")
    assert "n=song.mp3" in m.url
    assert m.subfolder == "bunkr"


def test_album_expands_all_items():
    client = _FakeClient(
        {
            "11111111": _meta("https://c1.cdn.cr", "/a.mp3", "A.mp3"),
            "22222222": _meta("https://c2.cdn.cr", "/b.mp4", "B.mp4"),
        },
        {"token": "t", "ex": "1"},
    )
    out = _resolve_bunkr("https://bunkr.cr/a/fixtureAlbum", client)
    assert len(out) == 2, out
    names = sorted(m.filename for m in out)
    assert names == ["A.mp3", "B.mp4"], names
    for m in out:
        assert m.subfolder == "My Album"
        assert "token=" in m.url and "ex=" in m.url


def test_failed_meta_returns_empty():
    client = _FakeClient({}, {"token": "t", "ex": "1"})
    # ids 33864542 / 1111.. not in by_id -> post returns {} -> no mediafiles
    assert _resolve_bunkr("https://bunkr.cr/f/fixtureSlug", client) == []


def test_official_download_page_is_used_after_cdn_rejection(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from src.resolvers import ResolvedMedia, refresh_media

    source = "https://bunkr.ws/f/example"
    official = "https://dl.bunkr.cr/file/123"
    client = _FakeClient({}, {"token": "fresh", "ex": "999"})
    pages = {
        source: f'<a href="{official}">Download</a>',
        official: '<button id="download-btn" data-id="123">Download</button>',
    }
    monkeypatch.setattr(client, "get_soup", lambda url: BeautifulSoup(pages[url], "html.parser"))

    def post(url, **kwargs):
        assert url == "https://dl.bunkr.cr/api/_001_v2"
        assert kwargs["json"] == {"id": "123"}
        assert kwargs["headers"]["Referer"] == official
        return _FakeResp(json_data=_meta("https://cdn.example", "/file.mp4", "file.mp4"))

    monkeypatch.setattr(client.session, "post", post)
    queued = ResolvedMedia(source, "file.mp4", source_url=source, resolver="bunkr")
    resolved = refresh_media(queued, client, prefer_download_page=True)
    assert resolved.state_key == source
    assert resolved.bunkr_file_id == "123"
    assert resolved.headers["Referer"] == official
    assert parse_qs(urlsplit(resolved.url).query) == {"n": ["file.mp4"], "token": ["fresh"], "ex": ["999"]}


def test_current_encrypted_api_and_data_file_id(monkeypatch):
    import src.resolvers as resolvers
    timestamp = 1_780_000_000
    raw = "https://c5bu-b.cdn.cr/storage/a%20video.mp4?download=1#media"
    key = f"SECRET_KEY_{timestamp // 3600}".encode()
    encrypted = base64.b64encode(bytes(b ^ key[i % len(key)] for i, b in enumerate(raw.encode()))).decode()
    client = _FakeClient({"42": {"url": encrypted, "encrypted": True, "timestamp": timestamp,
                                     "original": "A video.mp4"}}, {"token": "new", "ex": "999"})
    monkeypatch.setattr(resolvers, "_soup_of", lambda *_: BeautifulSoup('<button data-file-id="42"></button>', "html.parser"))
    original_post = client.session.post
    def post(url, **kwargs):
        assert url == "https://apidl.bunkr.ru/api/_001_v2"
        assert kwargs["headers"]["Origin"] == "https://get.bunkrr.su"
        assert kwargs["headers"]["Referer"].endswith("/file/42")
        return original_post(url, **kwargs)
    client.session.post = post
    out = _resolve_bunkr("https://bunkr.lu/f/example", client)
    assert out[0].filename == "A video.mp4"
    assert out[0].url == "https://c5bu-b.cdn.cr/storage/a%20video.mp4?download=1&token=new&ex=999#media"


@pytest.mark.parametrize("payload", [
    {"url": "not base64!", "encrypted": True, "timestamp": 1},
    {"url": "eA==", "encrypted": True},
    {"url": "javascript:alert(1)"},
    {"url": "https:///missing-host"},
    {"status": "error"},
])
def test_bad_api_payload_is_not_a_download(payload):
    client = _FakeClient({"33864542": payload}, {})
    assert _resolve_bunkr("https://bunkr.cr/f/example", client) == []


def test_plain_url_survives_unavailable_optional_signer():
    raw = "https://cdn.example/video.mp4?token=existing&ex=1"
    client = _FakeClient({"33864542": {"url": raw, "encrypted": False}}, {})
    out = _resolve_bunkr("https://bunkr.cx/f/example", client)
    assert out[0].url == raw
    assert out[0].filename == "video.mp4"
    raw = "https://cdn.example/video.mp4"
    client.session._by_id["33864542"] = {"url": raw, "encrypted": "false"}
    assert _resolve_bunkr("https://bunkr.cx/f/example", client)[0].url == raw


def test_modern_album_and_legacy_cdn():
    client = _FakeClient({
        "11111111": {"url": "https://cdn.example/a.mp4"},
        "22222222": {"url": "https://cdn.example/b.mp4"},
        "33864542": {"url": "https://cdn.example/fresh.mp4"},
    }, {})
    out = _resolve_bunkr("https://bunkr.sx/a/example", client)
    assert [m.filename for m in out] == ["a.mp4", "b.mp4"]
    assert all(m.subfolder == "My Album" for m in out)
    out = _resolve_bunkr("https://cdn2.bunkr.cr/old.mp4", client)
    assert out[0].url == "https://cdn.example/fresh.mp4"


@pytest.mark.parametrize("domain", ["lu", "lv", "bi", "cm", "at", "by", "cx", "sx"])
def test_new_domains_parse_and_dispatch(domain):
    from src.parsers.host_parser import parse_hosts
    from src.resolvers import resolve
    url = f"https://bunkr.{domain}/f/example"
    assert any(m.url == url for m in parse_hosts(f'<a href="{url}">file</a>'))
    client = _FakeClient({"33864542": {"url": "https://cdn.example/video.mp4"}}, {})
    from src.resolvers import refresh_media
    queued = resolve(url, client)[0]
    assert queued.resolver == "bunkr" and queued.source_url == url
    assert refresh_media(queued, client).url == "https://cdn.example/video.mp4"


def test_album_pagination_deduplicates_and_keeps_successes(monkeypatch):
    import requests
    import src.resolvers as resolvers
    client = _FakeClient({"11111111": {"url": "https://cdn.example/a.mp4"},
                          "22222222": {"url": "https://cdn.example/b.mp4"}}, {})
    visited = []
    def soup(_client, url):
        visited.append(url)
        if "/a/" in url:
            html = ALBUM_PAGE if "page=2" in url else '<h1>Album</h1><a href="/f/AAAA1111"></a><a href="/f/broken"></a>'
            if "page=3" in url:
                html = ALBUM_PAGE  # Repeating pages must terminate pagination.
        elif url.endswith("broken"):
            raise requests.Timeout("Unavailable item")
        else:
            html = FILE_A if "AAAA1111" in url else FILE_B
        return BeautifulSoup(html, "html.parser")
    monkeypatch.setattr(resolvers, "_soup_of", soup)
    out = _resolve_bunkr("https://bunkr.lu/a/demo", client)
    assert [m.filename for m in out] == ["a.mp4", "b.mp4"]
    assert sum("AAAA1111" in url for url in visited) == 1
    assert visited[-1].endswith("?page=3")


if __name__ == "__main__":
    test_single_file_page()
    test_album_expands_all_items()
    test_failed_meta_returns_empty()
    print("bunkr tests — passed 3, failed 0")
