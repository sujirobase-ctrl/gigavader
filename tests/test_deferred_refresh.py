"""Regression tests for short-lived URL refresh and stable resume state."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

import src.downloaders.thread_downloader as thread_module
import src.resolvers as resolvers
from src.config import FailReason, TaskResult
from src.downloaders.media_downloader import DownloadOutcome
from src.downloaders.thread_downloader import ThreadDownloader, ThreadOptions
from src.resolvers import ResolvedMedia, refresh_media, resolve


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.posts = 0
        self.gets = 0

    def post(self, _url, json=None, **_kwargs):
        self.posts += 1
        slug = (json or {}).get("file_slug", "")
        return _Response(
            {
                "success": True,
                "file": f"{slug}.zip",
                "name": "Fresh archive.zip",
                "server": "https://fsc2.cdn.cr",
                "token": f"token-{self.posts}",
            }
        )

    def get(self, url, **_kwargs):
        self.gets += 1
        if "/api/sign" in url:
            return _Response(
                {
                    "success": True,
                    "url": f"https://dl100.turbocdn.st/video.mov?token={self.gets}",
                    "original_filename": "Fresh video.mov",
                }
            )
        return _Response({}, status=404)


class _Client:
    timeout = 30

    def __init__(self):
        self.session = _Session()


def test_filester_token_is_created_only_when_worker_refreshes():
    client = _Client()
    queued = resolve("https://filester.me/d/ABC123", client)

    assert len(queued) == 1
    assert queued[0].resolver == "filester"
    assert queued[0].state_key == "https://filester.me/d/ABC123"
    assert client.session.posts == 0

    fresh = refresh_media(queued[0], client)
    assert fresh is not None
    assert client.session.posts == 1
    assert fresh.filename == "Fresh archive.zip"
    assert "token=token-1" in fresh.url
    assert fresh.state_key == queued[0].state_key


def test_turbo_signing_is_deferred_and_repeatable():
    client = _Client()
    queued = resolve("https://turbo.cr/v/VIDEO1", client)

    assert queued[0].resolver == "turbo"
    assert client.session.gets == 0
    first = refresh_media(queued[0], client)
    second = refresh_media(queued[0], client)

    assert first is not None and second is not None
    assert first.url != second.url
    assert client.session.gets == 2


def test_turbo_album_preserves_video_cover_without_signing(monkeypatch):
    soup = BeautifulSoup('<h1>Album</h1><a href="/v/abc"><img data-src="/cover.jpg"></a>', "html.parser")
    monkeypatch.setattr(resolvers, "_soup_of", lambda *_args: soup)
    client = _Client()
    queued = resolve("https://turbo.cr/a/album", client)
    assert len(queued) == 1
    assert queued[0].thumbnail_url == "https://turbo.cr/cover.jpg"
    assert client.session.gets == 0


class _Manager:
    def add_task(self, *_args, **_kwargs):
        return 1

    def update_task(self, *_args, **_kwargs):
        pass

    def update_summary(self, *_args, **_kwargs):
        pass

    def update_log(self, **_kwargs):
        pass


def _bare_thread_downloader() -> ThreadDownloader:
    downloader = ThreadDownloader.__new__(ThreadDownloader)
    downloader.options = ThreadOptions(workers=1, delay=99)
    downloader.live_manager = _Manager()
    downloader.client = object()
    return downloader


def test_verified_completion_skips_before_queue_delay(tmp_path, monkeypatch):
    downloader = _bare_thread_downloader()

    class _NeverDownload:
        def download(self, *_args, **_kwargs):
            raise AssertionError("completed item reached downloader")

    downloader.downloader = _NeverDownload()
    monkeypatch.setattr(
        thread_module.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("delay ran before cache")),
    )
    media = ResolvedMedia(url="https://host/already.jpg", filename="already.jpg")

    (tmp_path / "already.jpg").write_bytes(b"verified")
    assert not downloader._run_downloads(
        [(media, tmp_path)],
        {media.url: {"status": "completed", "path": "already.jpg", "size": 8}},
        tmp_path,
    )


@pytest.mark.parametrize(("resolver", "source"), [
    ("filester", "https://filester.me/d/RETRY"),
    ("bunkr", "https://bunkr.ws/f/RETRY"),
])
def test_failed_renewable_download_gets_a_new_url(tmp_path, monkeypatch, resolver, source):
    downloader = _bare_thread_downloader()
    downloader.options.delay = 0
    media = ResolvedMedia(
        url=source,
        filename="placeholder",
        source_url=source,
        resolver=resolver,
    )
    refreshes = []

    def _refresh(original, _client, *, prefer_download_page=False):
        refreshes.append((original.state_key, prefer_download_page))
        return ResolvedMedia(
            url=f"https://fsc2.cdn.cr/file.zip?token={len(refreshes)}",
            filename="file.zip",
            source_url=source,
            resolver=resolver,
        )

    target = tmp_path / "file.zip"

    class _Downloader:
        def __init__(self):
            self.calls = 0

        def download(self, _media, _folder, _index):
            self.calls += 1
            if self.calls == 1:
                return DownloadOutcome(
                    TaskResult.FAILED,
                    FailReason.HTTP_ERROR,
                    message="HTTP 403",
                )
            target.write_bytes(b"ok")
            return DownloadOutcome(TaskResult.COMPLETED, path=target)

    downloader.downloader = _Downloader()
    monkeypatch.setattr(thread_module, "refresh_media", _refresh)
    state = {}

    assert not downloader._run_downloads([(media, tmp_path)], state, tmp_path)
    assert refreshes == [(source, False), (source, resolver == "bunkr")]
    assert state[source]["status"] == "completed"
    assert state[source]["size"] == 2


def test_fileditch_download_is_not_skipped_based_on_name(tmp_path):
    downloader = _bare_thread_downloader()
    downloader.options.delay = 0
    existing = tmp_path / "mirror.zip"
    existing.write_bytes(b"other data")
    called = []

    class Download:
        def download(self, media, *_args):
            called.append(media.url)
            return DownloadOutcome(TaskResult.COMPLETED, path=existing)

    downloader.downloader = Download()
    url = "https://fileditchfiles.st/test/mirror.zip"
    media = ResolvedMedia(url=url, filename=existing.name)
    assert not downloader._run_downloads([(media, tmp_path)], {}, tmp_path)
    assert called == [url]
