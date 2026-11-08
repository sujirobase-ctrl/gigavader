"""Regression coverage for long queues, expiring links and durable recovery."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from bs4 import BeautifulSoup

import src.downloaders.thread_downloader as thread_module
import src.resolvers as resolvers
from src.config import TaskResult, SkipReason
from src.downloaders import ThreadDownloader, ThreadOptions, PreparedThread
from src.downloaders.media_downloader import MediaDownloader, DownloadOutcome
from src.error_summary import summarize_error
from src.file_utils import load_state, record_state, save_state
from src.http_client import HttpClient
from src.recovery import RecoveryStore
from src.resolvers import ResolvedMedia, resolve, refresh_media
from tests.test_preview import Manager


def test_recovery_roundtrip_selection_and_no_cookie_values(tmp_path):
    store = RecoveryStore(tmp_path / "sessions")
    options = ThreadOptions(download_path=str(tmp_path / "output"))
    store.begin("https://forum.example/threads/1", "cookies.txt", options, True)
    a = ResolvedMedia("https://cdn.example/a.zip?token=OLD", "a.zip", source_url="https://bunkr.cr/f/a",
                      resolver="bunkr", headers={"Cookie": "SECRET", "Referer": "https://bunkr.cr/"})
    b = ResolvedMedia("https://cdn.example/b.zip", "b.zip")
    prepared = PreparedThread("Thread", tmp_path / "output", [(a, tmp_path / "output"), (b, tmp_path / "output")], 2)
    store.save_prepared(prepared, True)
    store.select({a.state_key})
    raw = store.path.read_text(encoding="utf-8")
    assert "SECRET" not in raw and "token=OLD" not in raw
    pending = RecoveryStore(store.directory).pending()[0]
    restored = RecoveryStore.restore_prepared(pending)
    assert len(restored.plan) == 2
    assert restored.plan[0][0].url == a.source_url
    assert pending["selection"] == [a.source_url]
    assert pending["phase"] == "downloading"
    store.complete()
    assert not store.pending()


def test_recovery_ignores_corrupt_snapshot_and_rejects_path_escape(tmp_path):
    store = RecoveryStore(tmp_path)
    (tmp_path / "bad.json").write_text('{"version":', encoding="utf-8")
    assert store.pending() == []
    document = {"prepared": {"root": str(tmp_path), "title": "x", "post_count": 1,
                             "plan": [{"folder": "../escape", "media": {"url": "https://x/a", "filename": "a"}}]}}
    with pytest.raises(ValueError):
        store.restore_prepared(document)


def test_journal_survives_interrupted_last_line_and_compacts(tmp_path):
    for i in range(3):
        record_state(tmp_path, str(i), {"status": "completed", "size": i + 1})
    with (tmp_path / ".fpd_state.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"key":"unfinished')
    state = load_state(tmp_path)
    assert len(state) == 3
    record_state(tmp_path, "after-truncation", {"status": "completed"})
    state = load_state(tmp_path)
    assert len(state) == 4
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    assert not (tmp_path / ".fpd_state.jsonl").exists()


def test_bunkr_12800_links_20_pages_never_signed_early(monkeypatch):
    calls = []
    def soup(_client, url):
        from urllib.parse import parse_qs, urlparse
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        calls.append(page)
        anchors = "".join(f'<a href="/f/item{i}" title="file{i}.zip"></a>'
                          for i in range((page - 1) * 640, page * 640)) if page <= 20 else ""
        return BeautifulSoup("<h1>Large album</h1>" + anchors, "html.parser")
    monkeypatch.setattr(resolvers, "_soup_of", soup)
    queued = resolve("https://bunkr.cr/a/large", object())
    assert len(queued) == 12800 and len(calls) == 21
    assert all(item.resolver == "bunkr" for item in queued)
    clock = [0]
    issued = []
    def fresh(url, _client):
        issued.append(clock[0])
        return [ResolvedMedia(f"https://cdn.example/file.zip?token={clock[0]}", "file.zip")]
    monkeypatch.setattr(resolvers, "_resolve_bunkr", fresh)
    clock[0] = 55 * 3600
    out = refresh_media(queued[-1], object())
    assert issued == [198000] and "token=198000" in out.url
    assert out.state_key == queued[-1].state_key


def test_12800_jobs_use_bounded_executor_and_cancel_fast(tmp_path, monkeypatch):
    peak = [0]
    pending = [0]
    lock = threading.Lock()
    class TrackingPool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            with lock:
                pending[0] += 1
                peak[0] = max(peak[0], pending[0])
            future = super().submit(*args, **kwargs)
            def finished(_):
                with lock:
                    pending[0] -= 1
            future.add_done_callback(finished)
            return future
    monkeypatch.setattr(thread_module, "ThreadPoolExecutor", TrackingPool)
    stop = threading.Event()
    options = ThreadOptions(workers=3, dry_run=True, cancel_event=stop)
    downloader = ThreadDownloader(object(), Manager(), options)
    count = [0]
    class Download:
        def download(self, *args):
            time.sleep(.001)
            with lock:
                count[0] += 1
            return DownloadOutcome(TaskResult.SKIPPED, SkipReason.DRY_RUN)
    downloader.downloader = Download()
    plan = [(ResolvedMedia(f"https://host.example/{i}.zip", f"{i}.zip"), tmp_path) for i in range(12800)]
    assert not downloader._run_downloads(plan, {}, tmp_path)
    assert count[0] == 12800 and peak[0] <= 6
    stop.set()
    count[0] = 0
    assert not downloader._run_downloads(plan, {}, tmp_path)
    assert count[0] == 0


def test_thread_20_pages_captures_all_12800_files(tmp_path):
    from types import SimpleNamespace
    calls = []
    class Client:
        def get(self, url):
            page = int(url.rstrip('/').rsplit('page-', 1)[1]) if 'page-' in url else 1
            calls.append(page)
            links = ''.join(f'<a href="https://media.example/{page}-{i}.jpg">image</a>' for i in range(640))
            more = f'<a class="pageNav-jump--next" href="https://forum.example/threads/large/page-{page+1}">Next</a>' if page < 20 else ''
            html = (f'<h1 class="p-title-value">Large thread</h1>{more}'
                    f'<article class="message" id="post-{page}"><div class="message-body">{links}</div></article>')
            return SimpleNamespace(status_code=200, text=html)
    downloader = ThreadDownloader(Client(), Manager(), ThreadOptions(all_pages=True, download_path=str(tmp_path)))
    prepared = downloader.prepare_thread('https://forum.example/threads/large/')
    assert calls == list(range(1, 21))
    assert prepared.post_count == 20 and len(prepared.plan) == 12800
    assert len({item.state_key for item, _ in prepared.plan}) == 12800
    assert not prepared.root.exists()


def test_gofile_paginates_1001_files_and_reuses_account(monkeypatch):
    from tests.test_resolver_updates import _GoFileClient, _Response
    monkeypatch.setattr(resolvers, '_gofile_salt_cache', resolvers.GOFILE_DEFAULT_SALT)
    client = _GoFileClient()
    posts = []
    old_post = client.session.post
    def post(url, **kwargs):
        posts.append(url)
        return old_post(url, **kwargs)
    client.session.post = post
    pages = []
    def get(url, params=None, **kwargs):
        if url.endswith('/accounts/website'):
            return _Response({'status': 'ok'})
        content = url.rsplit('/', 1)[-1]
        if content.startswith('file-'):
            return _Response({'status': 'ok', 'data': {'type': 'file', 'id': content, 'name': content+'.zip',
                                                     'link': 'https://store.gofile.io/'+content+'.zip?token=new'}})
        page = params['page']
        pages.append(page)
        children = {f'file-{i}': {'id': f'file-{i}', 'name': f'{i}.zip', 'type': 'file',
                                  'link': f'https://store.gofile.io/{i}.zip'}
                    for i in (range(1000) if page == 1 else [1000])}
        return _Response({'status': 'ok', 'data': {'type': 'folder', 'name': 'root', 'children': children}})
    client.session.get = get
    queued = resolvers._resolve_gofile('https://gofile.io/d/root', client)
    assert len(queued) == 1001 and pages == [1, 2]
    fresh = refresh_media(queued[-1], client)
    assert fresh is not None and 'token=new' in fresh.url
    assert len(posts) == 1


@pytest.fixture
def local_server():
    state = {"requests": [], "mode": "cookie"}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            state["requests"].append(self.headers.get("Cookie", ""))
            if state["mode"] == "cookie":
                status = 200 if "session=fresh" in self.headers.get("Cookie", "") else 401
            else:
                status = int(state["mode"])
            data = b"synthetic media"
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/a.jpg", state
    finally:
        server.shutdown()
        server.server_close()


def test_cookies_can_be_reexported_without_restart(tmp_path, local_server):
    url, state = local_server
    path = tmp_path / "cookies.txt"
    def write(value, expires):
        comment = "# expired fixture\n" if expires else ""
        path.write_text("# Netscape HTTP Cookie File\n" + comment +
                        f"127.0.0.1\tFALSE\t/\tFALSE\t{expires}\tsession\t{value}\n", encoding="utf-8")
    write("stale", 1)
    with HttpClient(cookies_file=str(path), max_retries=1) as client:
        assert client.get(url).status_code == 401
        write("fresh", 0)
        assert client.get(url).status_code == 200
        assert client.session.cookies.get("session") == "fresh"


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_permanent_http_failure_is_not_retried_seven_times(tmp_path, local_server, status):
    url, state = local_server
    state["mode"] = str(status)
    with HttpClient(max_retries=3) as client:
        result = MediaDownloader(client, Manager(), max_retries=6).download(ResolvedMedia(url, "a.jpg"), tmp_path)
    assert result.message == f"HTTP {status} ao solicitar o arquivo"
    assert len(state["requests"]) == 1
    assert "link quebrado" not in summarize_error(result.message)


def test_429_budget_has_no_nested_retry_multiplier(tmp_path, local_server):
    url, state = local_server
    state["mode"] = "429"
    with HttpClient(max_retries=3) as client:
        result = MediaDownloader(client, Manager(), max_retries=1).download(ResolvedMedia(url, "a.jpg"), tmp_path)
    assert result.message == "HTTP 429"
    assert len(state["requests"]) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_repeated_auth_failures_do_not_stop_remaining_downloads(tmp_path, status):
    from src.config import FailReason
    downloader = ThreadDownloader(object(), Manager(), ThreadOptions(workers=2))
    calls = []
    class Download:
        def download(self, media, *args):
            calls.append(media.url)
            if media.filename.startswith("blocked"):
                return DownloadOutcome(TaskResult.FAILED, FailReason.HTTP_ERROR, message=f"HTTP {status}")
            return DownloadOutcome(TaskResult.SKIPPED, SkipReason.ALREADY_DOWNLOADED)
    downloader.downloader = Download()
    plan = [(ResolvedMedia(f"https://host.example/blocked{i}.zip", f"blocked{i}.zip"), tmp_path)
            for i in range(12)]
    for host in ("host.example", "mirror.example"):
        plan.append((ResolvedMedia(f"https://{host}/ok.zip", "ok.zip"), tmp_path))
    state = {}
    assert downloader._run_downloads(plan, state, tmp_path)
    assert len(calls) == len(plan)
    assert all(state[f"https://{host}/ok.zip"]["status"] == "skipped"
               for host in ("host.example", "mirror.example"))
