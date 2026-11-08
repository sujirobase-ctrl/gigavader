"""Offline tests for conflict handling and validated download resume."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SkipReason, TaskResult
from src.downloaders.media_downloader import MediaDownloader
from src.http_client import HttpClient
from src.resolvers import ResolvedMedia, _resolve_direct


class _FakeLive:
    def add_task(self, *a, **k): return 0
    def update_task(self, *a, **k): pass
    def update_log(self, *a, **k): pass


class _FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    @property
    def status_code(self): return 200
    def raise_for_status(self): pass
    @property
    def headers(self):
        class H:
            def get(self, k, d=None):
                return {"Content-Type": "audio/mpeg", "Content-Length": "10", "ETag": '"v1"'}.get(k, d)
        return H()
    def iter_content(self, chunk_size=1):
        yield b"0123456789"


def _make_downloader():
    client = HttpClient()
    client.session.get = lambda *a, **k: _FakeResp()
    return MediaDownloader(client, live_manager=_FakeLive())


def test_existing_nonempty_is_preserved_as_conflict(tmp_path):
    dl = _make_downloader()
    target = tmp_path / "song.mp3"
    target.write_bytes(b"0123")
    out = dl._attempt(ResolvedMedia(url="http://x/song.mp3", filename="song.mp3"), tmp_path, {}, 0)
    assert out.result is TaskResult.COMPLETED
    assert target.read_bytes() == b"0123"
    assert out.path.name == "song (1).mp3"
    assert out.path.read_bytes() == b"0123456789"


def test_simple_download_applies_rate_limit_and_respects_cancel(tmp_path):
    from threading import Event
    cancel = Event()
    consumed = []

    class Limiter:
        def consume(self, amount, event):
            consumed.append((amount, event))
            event.set()

    dl = _make_downloader()
    dl.rate_limiter = Limiter()
    dl.cancel_event = cancel
    media = ResolvedMedia(url="http://x/song.mp3", filename="song.mp3")
    out = dl.download(media, tmp_path)
    assert consumed == [(10, cancel)]
    assert out.result is TaskResult.FAILED
    assert not (tmp_path / "Audio" / "song.mp3").exists()


def test_existing_empty_is_redownloaded():
    dl = _make_downloader()
    d = Path(__file__).parent / "_tmp_skip"
    d.mkdir(exist_ok=True)
    target = d / "song.mp3"
    target.write_bytes(b"")  # 0-byte stub from a killed run -> must re-fetch
    out = dl._attempt(ResolvedMedia(url="http://x/song.mp3", filename="song.mp3"), d, {}, 0)
    assert out.result is TaskResult.COMPLETED, out
    assert target.read_bytes() == b"0123456789"
    target.unlink()


import pytest


@pytest.mark.parametrize(("content_range", "etag"), [
    ("bytes 4-9/10", '"v1"'),
    ("bytes 0-5/10", '"v1"'),
    ("bytes 4-9/10", '"old"'),
])
def test_resume_checks_range_and_version(tmp_path, content_range, etag):
    media = ResolvedMedia(url="http://x/song.mp3", filename="song.mp3")
    target = tmp_path / "song.mp3"
    part = MediaDownloader._work_path(target, media.state_key, '"v1"', 10).with_suffix(".part")
    part.write_bytes(b"0123")

    class ResumeResponse(_FakeResp):
        status_code = 206
        headers = {"Content-Type": "audio/mpeg", "Content-Length": "6",
                   "Content-Range": content_range, "ETag": etag}
        def iter_content(self, chunk_size=1):
            yield b"456789" if content_range.startswith("bytes 4-") and etag == '"v1"' else b"BADBAD"

    requests = []
    dl = _make_downloader()
    def get(*args, headers, **kwargs):
        requests.append(headers)
        return ResumeResponse() if "Range" in headers else _FakeResp()
    dl.client.session.get = get
    out = dl._attempt(media, tmp_path, {}, 0)
    assert out.result is TaskResult.COMPLETED
    assert target.read_bytes() == b"0123456789"
    assert requests[-1] == {"Range": "bytes=4-", "If-Range": '"v1"'}
    assert not part.exists()


def test_reddit_html_interstitial_is_retried(tmp_path, monkeypatch):
    class _Response:
        status_code = 200

        def __init__(self, content_type, body):
            self._content_type = content_type
            self._body = body

        def __enter__(self): return self
        def __exit__(self, *a): pass
        def raise_for_status(self): pass

        @property
        def headers(self):
            return {
                "Content-Type": self._content_type,
                "Content-Length": str(len(self._body)),
            }

        def iter_content(self, chunk_size=1):
            yield self._body

    responses = iter(
        [
            _Response("text/html; charset=utf-8", b"temporary block"),
            _Response("image/jpeg", b"jpeg bytes"),
        ]
    )
    client = HttpClient()
    client.session.get = lambda *a, **k: next(responses)
    downloader = MediaDownloader(client, live_manager=_FakeLive(), max_retries=1)
    media = _resolve_direct("https://i.redd.it/drfy08tqb89g1.jpeg", client)[0]
    monkeypatch.setattr("src.downloaders.media_downloader.time.sleep", lambda _seconds: None)

    outcome = downloader.download(media, tmp_path)

    assert outcome.result is TaskResult.COMPLETED
    assert (tmp_path / "Photos" / "drfy08tqb89g1.jpeg").read_bytes() == b"jpeg bytes"


@pytest.mark.parametrize(("content_type", "body"), [
    ("application/json", b'{"error":"unavailable"}'),
    ("video/mp4", b""),
])
def test_http_200_without_file_data_is_not_a_success(tmp_path, content_type, body):
    class Response(_FakeResp):
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        def iter_content(self, chunk_size=1):
            yield body

    dl = _make_downloader()
    dl.max_retries = 0
    dl.client.session.get = lambda *args, **kwargs: Response()
    out = dl.download(ResolvedMedia("https://cdn.example/video.mp4", "video.mp4"), tmp_path)
    assert out.result is TaskResult.FAILED
    assert not (tmp_path / "Videos" / "video.mp4").exists()


def test_resume_restarts_when_server_ignores_range(tmp_path):
    dl = _make_downloader()
    media = ResolvedMedia(url="http://x/song.mp3", filename="song.mp3")
    target = tmp_path / "song.mp3"
    part = MediaDownloader._work_path(target, media.state_key, '"v1"', 10).with_suffix(".part")
    part.write_bytes(b"0123")

    out = dl._attempt(media, tmp_path, {}, 0)

    assert out.result is TaskResult.COMPLETED
    assert target.read_bytes() == b"0123456789"
    assert not part.exists()


def test_same_filename_from_two_urls_gets_unique_targets():
    dl = _make_downloader()
    d = Path(__file__).parent / "_tmp_skip"
    d.mkdir(exist_ok=True)
    first = d / "song.mp3"
    second = d / "song (1).mp3"
    first.unlink(missing_ok=True)
    second.unlink(missing_ok=True)

    one = dl._attempt(
        ResolvedMedia(url="http://x/one", filename="song.mp3"), d, {}, 0,
    )
    two = dl._attempt(
        ResolvedMedia(url="http://x/two", filename="song.mp3"), d, {}, 1,
    )

    assert one.result is TaskResult.COMPLETED
    assert two.result is TaskResult.COMPLETED
    assert first.read_bytes() == b"0123456789"
    assert second.read_bytes() == b"0123456789"
    first.unlink()
    second.unlink()
