"""Offline regression coverage for prepare/select/download and safe previews."""

from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PIL import Image

import gui
import src.downloaders.thread_downloader as thread_module
import src.preview as preview
from src.config import TaskResult
from src.downloaders import (
    DownloadOutcome,
    PreparedThread,
    ThreadDownloader,
    ThreadOptions,
)
from src.file_utils import load_state, save_state
from src.parsers import ParsedPost
from src.preview import (
    PreviewCommand,
    PreviewSelection,
    fetch_thumbnail,
    media_kind,
    preview_fingerprint,
    thumbnail_source,
)
from src.resolvers import ResolvedMedia


class Manager:
    def __init__(self):
        self.logs = []
        self.counts = []

    def update_log(self, **kwargs):
        self.logs.append(kwargs)

    def update_summary(self, *args):
        self.counts.append(args)

    def add_overall_task(self, title, num_tasks):
        self.total = num_tasks

    def add_task(self, *args, **kwargs):
        return 1

    def update_task(self, *args, **kwargs):
        pass


def test_prepare_does_not_download_or_save_state(tmp_path, monkeypatch):
    downloader = ThreadDownloader(object(), Manager(), ThreadOptions(download_path=str(tmp_path)))
    posts = [ParsedPost("1", "4", 1, "", '<a href="https://example.test/pic.jpg">Photo</a>')]
    monkeypatch.setattr(downloader, "_collect_pages", lambda _url: ("Thread", posts))
    prepared = downloader.prepare_thread("https://example.test/threads/1")
    assert prepared is not None
    assert len(prepared.plan) == 1
    assert prepared.plan[0][0].post_label == "post-4"
    assert not prepared.root.exists()


def test_quality_warning_is_visible_during_preparation(tmp_path, monkeypatch):
    manager = Manager()
    downloader = ThreadDownloader(object(), manager, ThreadOptions(download_path=str(tmp_path)))
    posts = [ParsedPost("1", "4", 1, "", '<a href="https://goonbox.cr/img/demo">Photo</a>')]
    monkeypatch.setattr(downloader, "_collect_pages", lambda _url: ("Thread", posts))
    item = ResolvedMedia("https://cdn.cr/thumb.jpg", "thumb.jpg", warning="Original indisponível; miniatura.")
    monkeypatch.setattr(thread_module, "resolve", lambda *args, **kwargs: [item])
    assert downloader.prepare_thread("https://example.test/threads/1").plan
    assert any(log["event"] == "Aviso de qualidade" and "miniatura" in log["details"] for log in manager.logs)


def test_video_poster_belongs_only_to_its_source(tmp_path, monkeypatch):
    downloader = ThreadDownloader(object(), Manager(), ThreadOptions(download_path=str(tmp_path)))
    posts = [ParsedPost("1", "4", 1, "", (
        '<video poster="/cover.jpg"><source src="https://example.test/video.mp4"></video>'
        '<a href="https://example.test/other.mp4">Other</a>'
    ))]
    monkeypatch.setattr(downloader, "_collect_pages", lambda _url: ("Thread", posts))
    prepared = downloader.prepare_thread("https://example.test/threads/1")
    media = {item.filename: item for item, _ in prepared.plan}
    assert media["video.mp4"].thumbnail_url == "https://example.test/cover.jpg"
    assert media["other.mp4"].thumbnail_url is None


@pytest.mark.parametrize("choice,expected", [(None, ["one.jpg", "two.jpg"]), ({"https://x/two.jpg"}, ["two.jpg"]), (set(), [])])
def test_selection_exactly_limits_downloads(tmp_path, choice, expected):
    downloader = ThreadDownloader(object(), Manager(), ThreadOptions(workers=1))
    items = [ResolvedMedia(f"https://x/{name}", name) for name in ("one.jpg", "two.jpg")]
    root = tmp_path / "thread"
    prepared = PreparedThread("Thread", root, [(item, root) for item in items], 1)
    calls = []

    class Files:
        def download(self, media, folder, _index):
            calls.append(media.filename)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / media.filename
            target.write_bytes(b"file")
            return DownloadOutcome(TaskResult.COMPLETED, path=target)

    downloader.downloader = Files()
    assert not downloader.download_prepared(prepared, choice)
    assert calls == expected
    state = load_state(root)
    assert set(state) == {f"https://x/{name}" for name in expected}
    if not expected:
        assert not root.exists()


def test_selection_preserves_unselected_resume_entries(tmp_path):
    (tmp_path / "old.jpg").write_bytes(b"verified")
    old = {"https://x/old.jpg": {"status": "completed", "size": 8, "path": "old.jpg"},
           "https://x/pending.jpg": {"status": "failed"}}
    save_state(tmp_path, old)
    manager = Manager()
    downloader = ThreadDownloader(object(), manager, ThreadOptions())
    item = ResolvedMedia("https://x/old.jpg", "old.jpg")
    prepared = PreparedThread("Thread", tmp_path, [(item, tmp_path)], 1)
    assert not downloader.download_prepared(prepared, {item.state_key})
    assert load_state(tmp_path) == old
    assert manager.total == 1


def test_unknown_selection_rejected_and_cancel_does_not_write(tmp_path):
    stop = Event()
    downloader = ThreadDownloader(object(), Manager(), ThreadOptions(cancel_event=stop))
    prepared = PreparedThread("Thread", tmp_path / "thread", [], 0)
    with pytest.raises(ValueError):
        downloader.download_prepared(prepared, {"wrong-thread"})
    stop.set()
    assert downloader.download_prepared(prepared)
    assert not prepared.root.exists()


def test_unselected_renewable_links_are_never_refreshed(tmp_path, monkeypatch):
    downloader = ThreadDownloader(object(), Manager(), ThreadOptions())
    item = ResolvedMedia("https://filester.me/d/ABC", "ABC", resolver="filester")
    prepared = PreparedThread("Thread", tmp_path / "thread", [(item, tmp_path)], 1)
    monkeypatch.setattr(thread_module, "refresh_media", lambda *_: pytest.fail("unselected link signed"))
    assert not downloader.download_prepared(prepared, set())


def test_selection_survives_filters_and_page_subsets():
    items = [ResolvedMedia(f"https://x/{i}.jpg", f"photo-{i}.jpg", post_label=f"post-{i}") for i in range(40)]
    items.append(ResolvedMedia("https://x/movie.mp4", "movie.mp4"))
    model = PreviewSelection(items)
    assert not model.selected
    model.select(model.filtered_indices(), False)
    model.select([0, 21, 40], True)
    assert len(model.filtered_indices(kind="Vídeos")) == 1
    assert model.filtered_indices(query="post-21") == [21]
    model.select(model.filtered_indices(kind="Vídeos"), False)
    assert model.selected == {items[0].state_key, items[21].state_key}


def test_media_types_and_no_signing_for_nonimage_previews():
    assert media_kind(ResolvedMedia("https://x/a.mp3", "a.mp3")) == "Áudios"
    video = ResolvedMedia("https://turbo.cr/v/id", "Turbo_id", resolver="turbo")
    archive = ResolvedMedia("https://x/archive.zip", "archive.zip")
    assert media_kind(video) == "Vídeos"
    assert media_kind(archive) == "Arquivos"
    assert thumbnail_source(video) is None
    assert fetch_thumbnail(archive, None, Event()) is None


def fake_thumbnail_client(monkeypatch, *, body=b"", content_type="image/png", length=None, record=None):
    class Response:
        status_code = 200
        headers = {"Content-Type": content_type, "Content-Length": str(len(body) if length is None else length)}

        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def iter_content(self, **_kwargs):
            yield body

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, source, **kwargs):
            if record is not None:
                record.append((source, kwargs))
            return Response()

    monkeypatch.setattr(preview, "HttpClient", Client)


def test_preview_decodes_and_resizes_in_memory(monkeypatch):
    buf = BytesIO()
    Image.new("RGB", (600, 400), "blue").save(buf, "PNG")
    fake_thumbnail_client(monkeypatch, body=buf.getvalue())
    image = fetch_thumbnail(ResolvedMedia("https://x/photo.png", "photo.png"), None, Event())
    assert image is not None
    assert image.width <= 240 and image.height <= 150


@pytest.mark.parametrize("content_type,length", [("text/html", 30), ("image/png", 5 * 1024 * 1024)])
def test_html_and_large_previews_are_ignored(monkeypatch, content_type, length):
    fake_thumbnail_client(monkeypatch, content_type=content_type, length=length)
    assert fetch_thumbnail(ResolvedMedia("https://x/photo.png", "photo.png"), None, Event()) is None


def test_cross_host_thumbnail_does_not_inherit_explicit_auth_headers(monkeypatch):
    record = []
    fake_thumbnail_client(monkeypatch, record=record)
    media = ResolvedMedia(
        "https://host/file.mp4", "file.mp4", headers={"Authorization": "Bearer private"},
        thumbnail_url="https://different-host/thumb.png",
    )
    fetch_thumbnail(media, None, Event())
    assert "Authorization" not in record[0][1]["headers"]


def test_preview_cancelled_before_request_does_not_fetch(monkeypatch):
    stop = Event()
    stop.set()
    monkeypatch.setattr(preview, "HttpClient", lambda **_: pytest.fail("cancelled preview fetched"))
    assert fetch_thumbnail(ResolvedMedia("https://x/a.jpg", "a.jpg"), None, stop) is None


def test_thumbnail_host_requires_referer():
    payload = BytesIO()
    Image.new('RGB', (900, 600), 'blue').save(payload, 'JPEG')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            valid = self.headers.get('Referer') == 'https://forum.example/post/1'
            body = payload.getvalue() if valid else b'Forbidden'
            self.send_response(200 if valid else 403)
            self.send_header('Content-Type', 'image/jpeg' if valid else 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        media = ResolvedMedia(f'http://127.0.0.1:{server.server_port}/image.jpg', 'image.jpg')
        assert fetch_thumbnail(media, None, Event()) is None
        media.origin_url = 'https://forum.example/post/1'
        thumb = fetch_thumbnail(media, None, Event())
        assert thumb is not None and thumb.width <= 240 and thumb.height <= 150
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("decision", ["selected", "all", "cancel"])
def test_gui_worker_waits_for_selection_before_fetching_media(tmp_path, decision):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/threads/"):
                base = f"http://127.0.0.1:{self.server.server_port}"
                body = (
                    '<h1 class="p-title-value">Selection Test</h1>'
                    '<article class="message" id="post-1"><div class="message-body">'
                    f'<a href="{base}/one.jpg">one</a><a href="{base}/two.jpg">two</a>'
                    '</div></article>'
                ).encode()
                content_type = "text/html"
            else:
                body, content_type = b"test image bytes", "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    events, decisions, stop = Queue(), Queue(), Event()
    app = SimpleNamespace(events=events, cancel_event=stop, _save_recovery=lambda *args, **kwargs: True)
    options = ThreadOptions(download_path=str(tmp_path), workers=1, delay=0, cancel_event=stop)
    worker = Thread(
        target=gui.BuidlApp._download_worker,
        args=(app, f"http://127.0.0.1:{server.server_port}/threads/1", None,
              options, tmp_path / "gui-test.log", True, decisions),
        daemon=True,
    )
    try:
        worker.start()
        while True:
            event = events.get(timeout=5)
            if event[0] == "preview_ready":
                prepared = event[1]
                break
            assert event[0] not in {"fatal", "done"}, event
        assert requests == ["/threads/1"]
        assert not prepared.root.exists()
        if decision == "cancel":
            stop.set()
        else:
            decisions.put(None if decision == "all" else {prepared.plan[1][0].state_key})
        worker.join(timeout=5)
        assert not worker.is_alive()
        names = sorted(path.name for path in prepared.root.rglob("*.jpg"))
        assert names == {"selected": ["two.jpg"], "all": ["one.jpg", "two.jpg"], "cancel": []}[decision]
        if decision == "cancel":
            assert not prepared.root.exists()

        # Reopen after either completing a selection or closing the gallery:
        # the cached queue must be returned without another forum GET.
        stop.clear()
        events, decisions = Queue(), Queue()
        app.events = events
        worker = Thread(
            target=gui.BuidlApp._download_worker,
            args=(app, f"http://127.0.0.1:{server.server_port}/threads/1", None,
                  options, tmp_path / "gui-test.log", True, decisions, prepared),
            daemon=True,
        )
        worker.start()
        while True:
            event = events.get(timeout=5)
            if event[0] == "preview_ready":
                assert event[1] is prepared
                break
            assert event[0] not in {"fatal", "done"}, event
        assert requests.count("/threads/1") == 1
        decisions.put({prepared.plan[0][0].state_key})
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert requests.count("/threads/1") == 1
        assert (prepared.root / "Photos" / "one.jpg").exists()

        # Only the explicit refresh command scans the thread again.
        events, decisions = Queue(), Queue()
        app.events = events
        worker = Thread(
            target=gui.BuidlApp._download_worker,
            args=(app, f"http://127.0.0.1:{server.server_port}/threads/1", None,
                  options, tmp_path / "gui-test.log", True, decisions, prepared),
            daemon=True,
        )
        worker.start()
        for refreshing in (False, True):
            while True:
                event = events.get(timeout=5)
                if event[0] == "preview_ready":
                    break
                assert event[0] not in {"fatal", "done"}, event
            assert requests.count("/threads/1") == (2 if refreshing else 1)
            if not refreshing:
                decisions.put(PreviewCommand.REFRESH)
            else:
                assert event[1] is not prepared
                stop.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        stop.set()
        worker.join(timeout=5)
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("change", [
    {"max_pages": 2}, {"all_pages": True}, {"include": [".jpg"]},
    {"ignore": [".mp4"]}, {"separate_posts": True}, {"flat": True},
    {"download_path": "different-folder"}, {"post_ids": ["2"]},
])
def test_preview_cache_key_changes_when_queue_settings_change(change):
    options = ThreadOptions()
    key = preview_fingerprint("https://forum/thread1", None, options)
    assert key != preview_fingerprint("https://forum/thread1", None, replace(options, **change))


def test_preview_cache_does_not_rescan_for_execution_settings():
    options = ThreadOptions(all_pages=True)
    key = preview_fingerprint("https://forum/thread1", None, options)
    changed = replace(options, workers=12, max_retries=9, connections=5, delay=8,
                      rate_limit=1000, dry_run=True, max_pages=99)
    assert key == preview_fingerprint("https://forum/thread1", None, changed)
    assert key != preview_fingerprint("https://forum/thread2", None, changed)


def test_replacing_cookies_invalidates_preview_cache(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("first")
    options = ThreadOptions()
    key = preview_fingerprint("https://forum/thread", str(cookies), options)
    cookies.write_text("replacement session")
    assert key != preview_fingerprint("https://forum/thread", str(cookies), options)
