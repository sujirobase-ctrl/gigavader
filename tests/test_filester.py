"""Offline tests for the filester.me resolver (_resolve_filester).

Mocks HttpClient so no network is touched; locks the SPA->API->CDN flow.
"""
from __future__ import annotations

import sys
import os
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.resolvers import _resolve_filester


FOLDER_HTML = """
<html><body>
<h1>Ikumi VODs</h1>
<div class="files-list" id="filesGrid">
  <div class="file-item" data-name="Video A.mp4" onclick="window.location.href='/d/AAAA'"></div>
  <div class="file-item" data-name="Video B.mp4" onclick="window.location.href='/d/BBBB'"></div>
</div>
</body></html>
"""


class _FakeResp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, api_by_slug):
        self._api_by_slug = api_by_slug

    def get(self, url, **_):
        return _FakeResp(text=FOLDER_HTML)

    def post(self, url, json=None, **_):
        slug = (json or {}).get("file_slug", "")
        r = _FakeResp()
        r._json = self._api_by_slug.get(slug, {"success": "False"})
        return r


class _FakeClient:
    def __init__(self, api_by_slug):
        self.session = _FakeSession(api_by_slug)
        self.timeout = 30

    def get_soup(self, url):
        from bs4 import BeautifulSoup
        return BeautifulSoup(FOLDER_HTML, "html.parser")


def _api(file_id, name, server="https://fsc1.cdn.cr"):
    return {
        "success": "True",
        "file": f"{file_id}.mp4",
        "name": name,
        "server": server,
        "token": "deadbeef",
        "expires_in": "1800",
    }


def test_folder_resolves_all_items():
    client = _FakeClient({
        "AAAA": {"success": "True", "file": "aaaa.mp4", "name": "Video A.mp4",
                 "server": "https://fsc1.cdn.cr", "token": "tokA"},
        "BBBB": {"success": "True", "file": "bbbb.mp4", "name": "Video B.mp4",
                 "server": "https://fsc1.cdn.cr", "token": "tokB"},
    })
    out = _resolve_filester("https://filester.me/f/0000000000000001", client)
    assert len(out) == 2, out
    names = sorted(m.filename for m in out)
    assert names == ["Video A.mp4", "Video B.mp4"], names
    for m in out:
        assert m.url.startswith("https://fsc1.cdn.cr/v2/")
        assert "token=" in m.url and "download=true" in m.url
        assert m.subfolder == "Ikumi VODs"


def test_single_file_page():
    client = _FakeClient({"CCCC": _api("cccc", "Solo.mp4")})
    out = _resolve_filester("https://filester.me/d/CCCC", client)
    assert len(out) == 1
    assert out[0].filename == "Solo.mp4"
    assert "token=deadbeef" in out[0].url


def test_failed_api_returns_empty():
    client = _FakeClient({})
    assert _resolve_filester("https://filester.me/f/0000000000000001", client) == []


if __name__ == "__main__":
    test_folder_resolves_all_items()
    test_single_file_page()
    test_failed_api_returns_empty()
    print("filester tests — passed 3, failed 0")
