"""Offline regression tests for the Goonbox resolver."""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.config import TaskResult
from src.downloaders.media_downloader import DownloadOutcome, MediaDownloader
from src.parsers.host_parser import parse_hosts
from src.resolvers import ResolvedMedia, _resolve_goodbox


class _Response:
    def __init__(self, *, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def close(self):
        return None


class _Session:
    def __init__(self, payloads):
        self.payloads = payloads

    def get(self, url, **_kwargs):
        if "/api/" in url:
            payload = self.payloads.get(url)
            return _Response(status=200 if payload is not None else 403, payload=payload)
        return _Response(status=206)

    def head(self, _url, **_kwargs):
        return _Response(status=405)


class _Client:
    timeout = 30

    def __init__(self, payloads, html=""):
        self.session = _Session(payloads)
        self.html = html

    def get_soup(self, _url, **_kwargs):
        return BeautifulSoup(self.html, "html.parser")


def test_host_matcher_accepts_image_and_album():
    html = (
        '<a href="https://goonbox.cr/img/fixtureImg">image</a>'
        '<a href="https://goonbox.cr/a/albumSlug">album</a>'
    )
    matches = parse_hosts(html)
    assert len([match for match in matches if "goonbox.cr" in match.url]) == 2


def test_nested_original_url_and_probe_fallback():
    page = '<meta property="og:image" content="https://cdn.cr/thumb.md.jpg">'
    client = _Client(
        {
            "https://goonbox.cr/api/images/fixtureImg": {
                "data": {"image": {"assets": {"originalUrl": "https://cdn.cr/original.jpg"}}}
            }
        },
        page,
    )
    out = _resolve_goodbox("https://goonbox.cr/img/fixtureImg", client)
    assert len(out) == 1
    assert out[0].url == "https://cdn.cr/original.jpg"
    assert out[0].warning is None
    assert out[0].headers["Referer"] == "https://goonbox.cr/img/fixtureImg"


def test_cloudflare_api_failure_returns_embedded_thumbnail():
    page = '<meta property="og:image" content="https://cdn.cr/thumb.md.jpg">'
    client = _Client({}, page)
    out = _resolve_goodbox("https://goonbox.cr/img/blocked", client)
    assert len(out) == 1
    assert out[0].url == "https://cdn.cr/thumb.md.jpg"
    assert "miniatura" in out[0].warning


def test_album_paginates():
    client = _Client(
        {
            "https://goonbox.cr/api/albums/demo/images?page=1": {
                "images": [{"original_url": "https://cdn.cr/one.jpg"}],
                "pagination": {"last_page": 2},
            },
            "https://goonbox.cr/api/albums/demo/images?page=2": {
                "images": [{"original_url": "https://cdn.cr/two.jpg"}],
                "pagination": {"last_page": 2},
            },
        },
    )
    out = _resolve_goodbox("https://goonbox.cr/a/demo", client)
    assert [item.url for item in out] == ["https://cdn.cr/one.jpg", "https://cdn.cr/two.jpg"]


def test_download_restores_missing_image_headers_from_saved_queue(tmp_path, monkeypatch):
    downloader = MediaDownloader(None, None)
    seen = []

    def attempt(media, folder, headers, index):
        seen.append(headers)
        return DownloadOutcome(TaskResult.COMPLETED)

    monkeypatch.setattr(downloader, "_attempt", attempt)
    url = "https://simp6.cuckcapital.cr/photo.jpg"
    downloader.download(ResolvedMedia(url, "photo.jpg"), tmp_path)
    page = "https://goonbox.cr/img/demo"
    downloader.download(ResolvedMedia(url, "photo.jpg", origin_url=page), tmp_path)
    downloader.download(ResolvedMedia("https://example.org/photo.jpg", "photo.jpg"), tmp_path)
    assert [headers.get("Referer") for headers in seen] == ["https://goonbox.cr/", page, None]
    assert "image/*" in seen[0]["Accept"] and "image/*" in seen[1]["Accept"]
    assert seen[2] == {"Accept-Encoding": "identity"}
