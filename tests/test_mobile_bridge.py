"""Desktop-side checks for the exact Python engine bundled into Android."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import requests as http_requests

MODULE = Path(__file__).resolve().parents[1] / "android/app/src/main/python/mobile_bridge.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("mobile_bridge", MODULE)
mobile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mobile)


def wait(engine):
    end = time.monotonic() + 10
    while engine.busy and time.monotonic() < end:
        time.sleep(0.02)
    assert not engine.busy
    return json.loads(engine.snapshot())


@pytest.fixture
def server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/threads/"):
                base = f"http://127.0.0.1:{self.server.server_port}"
                body = (f'<h1 class="p-title-value">Mobile QA</h1><article class="message" id="post-1">'
                        f'<div class="message-body"><a href="{base}/one.jpg">One</a>'
                        f'<a href="{base}/two.jpg">Two</a><a href="{base}/bad.zip">Bad</a></div></article>').encode()
                content_type = "text/html"
            elif self.path == "/bad.zip":
                body, content_type = b"<html>Login required</html>", "text/html"
            else:
                body, content_type = b"test bytes for a downloaded image", "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", requests
    finally:
        http.shutdown()
        http.server_close()


def test_mobile_selection_cache_failures_and_resume(tmp_path, server):
    base, requests = server
    engine = mobile.Engine(str(tmp_path / "private"), str(tmp_path / "downloads"))
    settings = json.dumps({"url": base + "/threads/1", "workers": 1})
    engine.scan(settings)
    assert wait(engine)["state"] == "ready"
    assert len(json.loads(engine.items())) == 3
    assert not list((tmp_path / "downloads").iterdir())
    engine.download(1, "[0]")
    assert wait(engine)["completed"] == 1
    assert json.loads(engine.files())[0]["name"] == "one.jpg"
    assert requests == ["/threads/1", "/one.jpg"]
    engine.scan(settings)
    assert wait(engine)["scan_count"] == 1
    engine.download(1, "[0,1]")
    result = wait(engine)
    assert result["completed"] == result["skipped"] == 1
    assert requests.count("/threads/1") == 1
    engine.download(1, "[2]")
    result = wait(engine)
    assert result["failed"] == 1 and result["state"] == "failed" and result["errors"]
    engine.scan(settings, True)
    assert wait(engine)["queue_id"] == 2
    with pytest.raises(ValueError):
        engine.download(1, "[0]")


def test_mobile_145_recovery_selection_history_and_retry(tmp_path, server):
    base, requests = server
    engine = mobile.Engine(str(tmp_path/'private'), str(tmp_path/'downloads'))
    engine.scan(json.dumps(dict(url=base+'/threads/1', workers=1)))
    wait(engine)
    engine.select(1, '[0,2]')
    restarted = mobile.Engine(str(engine.private_dir), str(engine.download_dir))
    pending = json.loads(restarted.snapshot())['recoveries']
    assert len(pending) == 1
    restarted.recover(pending[0]['id'])
    assert restarted.selection == [0, 2]
    assert requests.count('/threads/1') == 1  # No repeat of collection.
    restarted.download(1, '[0,2]')
    assert wait(restarted)['state'] == 'failed'
    queue = json.loads(restarted.queue())['rows']
    assert [r['status'] for r in queue] == ['completed', 'pending', 'failed']
    restarted.retry()
    assert wait(restarted)['failed'] == 1
    assert requests.count('/one.jpg') == 1
    assert len(restarted.history) == 2
    assert restarted.selection == [0, 2]
    assert set(restarted.recovery.pending()[0]['selection']) == {restarted.prepared.plan[i][0].state_key for i in [0, 2]}
    report = restarted.report()
    assert base not in report and 'cookies.txt' not in report
    again = mobile.Engine(str(engine.private_dir), str(engine.download_dir))
    assert len(json.loads(again.history_json())) == 2


def test_mobile_145_dry_run_preserves_recovery_and_destination(tmp_path, server):
    base, _ = server
    engine, storage = destination_engine(tmp_path)
    engine.scan(json.dumps(dict(url=base+'/threads/1', workers=1)))
    wait(engine)
    old = engine.recovery.pending()[0]['_path']
    engine.scan(json.dumps(dict(url=base+'/threads/1', dry_run=True)), True, True)
    wait(engine)
    assert Path(old).is_file()
    assert storage.published == 0
    assert not list(engine.download_dir.rglob('*.jpg'))


@pytest.mark.parametrize('key,value', [('workers',17), ('connections',9), ('rate',float('nan')), ('max_pages',1.2), ('delay',-1)])
def test_mobile_145_options_validate_before_work(tmp_path, key, value):
    engine = mobile.Engine(str(tmp_path/'private'), str(tmp_path/'downloads'))
    with pytest.raises(ValueError):
        engine.scan(json.dumps(dict(url='https://example.com/threads/1', **{key:value})))
    assert not engine.busy
    assert not engine.recovery.pending()


def test_mobile_145_completed_session_does_not_prompt_again(tmp_path, server):
    base, _ = server
    engine = mobile.Engine(str(tmp_path/'private'), str(tmp_path/'downloads'))
    engine.scan(json.dumps(dict(url=base+'/threads/1')))
    wait(engine)
    engine.download(1, '[0,1]')
    assert wait(engine)['completed'] == 2
    assert not engine.recovery.pending()


def test_mobile_rejects_bad_input_and_keeps_old_cookies(tmp_path):
    engine = mobile.Engine(str(tmp_path / "private"), str(tmp_path / "downloads"))
    with pytest.raises(ValueError):
        engine.scan('{"url":"file:///private"}')
    pending = tmp_path / "private" / "cookies.pending"
    pending.write_text("invalid cookies")
    engine.cookie_path.write_text("previous cookie data")
    with pytest.raises(ValueError):
        engine.import_cookies(str(pending))
    assert engine.cookie_path.read_text() == "previous cookie data"


def test_no_credentials_in_visible_query_strings():
    assert mobile.redacted("Error https://host/file?token=private") == "Error https://host/file?[oculto]"


def test_capture_persists_session_agent_scope_and_invalidates_cache(tmp_path):
    engine = mobile.Engine(str(tmp_path / "private"), str(tmp_path / "downloads"))
    engine.fingerprint = ("old session",)
    assert engine.capture_cookies("simpcity.cr", "xf_session=synthetic==; cf_clearance=qa; empty=", "BUIDL QA WebView/1") == 3
    assert engine.fingerprint is None
    restarted = mobile.Engine(str(engine.private_dir), str(engine.download_dir))
    assert json.loads(restarted.snapshot())["cookies"]
    with restarted._client() as client:
        def prepared(url):
            return client.session.prepare_request(http_requests.Request("GET", url))
        request = prepared("https://simpcity.cr/threads/qa")
        assert request.headers["User-Agent"] == "BUIDL QA WebView/1"
        assert request.headers["Cookie"] == "xf_session=synthetic==; cf_clearance=qa; empty="
        assert all(cookie.secure and cookie.has_nonstandard_attr("HTTPOnly") for cookie in client.session.cookies)
        for url in ["http://simpcity.cr/", "https://simpcity.cr.evil.invalid/", "https://other.invalid/"]:
            assert "Cookie" not in prepared(url).headers
    assert "synthetic==" not in engine.snapshot()
    assert not list(engine.private_dir.glob("*.pending"))


@pytest.mark.parametrize("host,header,agent", [
    ("evil.invalid", "xf_session=secret", "QA"),
    ("simpcity.cr.evil.invalid", "xf_session=secret", "QA"),
    ("simpcity.cr", "", "QA"),
    ("simpcity.cr", None, "QA"),
    ("simpcity.cr", "xf_session=secret\nnew=value", "QA"),
    ("simpcity.cr", "xf_session=secret\tnew=value", "QA"),
    ("simpcity.cr", "invalid name=secret", "QA"),
    ("simpcity.cr", "missing_equals_secret", "QA"),
    ("simpcity.cr", "a=" + "x" * 65536, "QA"),
    ("simpcity.cr", "; ".join(f"c{i}=secret" for i in range(301)), "QA"),
    ("simpcity.cr", "xf_session=secret", "QA\r\nCookie: secret"),
    ("simpcity.cr", "xf_session=secret", ""),
], ids=["wrong-host", "spoofed-host", "empty", "none", "newline", "tab", "name", "equals", "oversize", "count", "agent-injection", "empty-agent"])
def test_capture_invalid_preserves_previous_session_without_exposing_values(tmp_path, host, header, agent):
    engine = mobile.Engine(str(tmp_path / "private"), str(tmp_path / "downloads"))
    engine.capture_cookies("simpcity.cr", "old=previous", "old-agent")
    previous = engine.cookie_path.read_bytes()
    with pytest.raises(ValueError) as error:
        engine.capture_cookies(host, header, agent)
    assert "secret" not in str(error.value)
    assert engine.cookie_path.read_bytes() == previous


def test_capture_busy_atomic_failure_import_and_remove(tmp_path, monkeypatch):
    engine = mobile.Engine(str(tmp_path / "private"), str(tmp_path / "downloads"))
    engine.capture_cookies("simpcity.cr", "old=previous", "old-agent")
    old = engine.cookie_path.read_bytes()
    engine.busy = True
    with pytest.raises(ValueError):
        engine.capture_cookies("simpcity.cr", "new=secret", "new-agent")
    engine.busy = False
    with monkeypatch.context() as context:
        def fail_replace(*_args):
            raise OSError("synthetic disk failure")
        context.setattr(Path, "replace", fail_replace)
        with pytest.raises(ValueError, match="anteriores foram mantidos"):
            engine.capture_cookies("simpcity.cr", "new=secret", "new-agent")
    assert engine.cookie_path.read_bytes() == old
    assert not list(engine.private_dir.glob("*.pending"))
    pending = engine.private_dir / "cookies.pending"
    pending.write_text("# Netscape HTTP Cookie File\nqa.invalid\tFALSE\t/\tFALSE\t0\tmanual\tqa\n")
    engine.import_cookies(str(pending))
    with engine._client() as client:
        assert client.session.headers["User-Agent"] != "old-agent"
        assert client.session.cookies.get("manual") == "qa"
    engine.remove_cookies()
    assert not json.loads(engine.snapshot())["cookies"]


def test_capture_duplicate_names_use_browser_order(tmp_path):
    engine = mobile.Engine(str(tmp_path / "private"), str(tmp_path / "downloads"))
    assert engine.capture_cookies("www.simpcity.cr", "name=first; name=second", "QA") == 1
    with engine._client() as client:
        assert client.session.cookies.get("name") == "first"


class FakeDestinationStorage:
    """Filesystem-backed SAF substitute; real SAF is separately instrumented."""
    def __init__(self, root):
        self.root = root
        self.paths = {}
        self.denied = False
        self.fail_write = False
        self.published = 0
        self.pending = set()

    def describe(self, tree):
        if self.denied:
            raise OSError("Permissão da pasta perdida")
        return "QA / " + tree.rsplit("/", 1)[-1]

    def exists(self, tree, uri, size):
        self.describe(tree)
        path = self.paths.get(uri)
        return bool(path and path.is_file() and path.stat().st_size == size > 0)

    def publish(self, tree, source, relative, mime):
        self.describe(tree)
        if self.fail_write:
            raise OSError("Sem espaço na pasta escolhida")
        destination = self.root / tree.rsplit("/", 1)[-1] / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_stem(destination.stem + "-new")
        shutil.copyfile(source, destination)
        self.published += 1
        uri = tree + "/document/" + str(self.published)
        self.paths[uri] = destination
        self.pending.add(uri)
        return json.dumps(dict(uri=uri, name=destination.name, relative=relative, size=destination.stat().st_size))

    def discard(self, tree, uri):
        assert uri in self.pending
        self.paths[uri].unlink()
        self.pending.remove(uri)

    def commit(self, uri):
        self.pending.remove(uri)


def destination_engine(tmp_path):
    storage = FakeDestinationStorage(tmp_path / "chosen")
    engine = mobile.Engine(str(tmp_path / "private"), str(tmp_path / "downloads"), storage)
    engine.set_destination("content://qa/tree/a")
    return engine, storage


def test_destination_selected_download_resume_and_restart(tmp_path, server):
    base, requests = server
    engine, storage = destination_engine(tmp_path)
    settings = json.dumps({"url": base + "/threads/1", "workers": 1})
    engine.scan(settings)
    wait(engine)
    assert not storage.root.exists()  # Analysis never copies files.
    engine.download(1, "[1]")
    assert wait(engine)["completed"] == 1
    files = json.loads(engine.files())
    assert len(files) == 1 and files[0]["name"] == "two.jpg"
    assert storage.paths[files[0]["uri"]].read_bytes() == b"test bytes for a downloaded image"
    assert not list(engine.download_dir.rglob("*.jpg"))
    assert not list(engine.private_dir.rglob("*.jpg"))  # Successful staging was removed.
    engine.download(1, "[1]")
    assert wait(engine)["skipped"] == 1
    assert requests.count("/two.jpg") == 1
    restarted = mobile.Engine(str(engine.private_dir), str(engine.download_dir), storage)
    assert json.loads(restarted.snapshot())["destination"] == "content://qa/tree/a"
    restarted.scan(settings)
    wait(restarted)
    restarted.download(1, "[1]")
    assert wait(restarted)["skipped"] == 1
    assert storage.published == 1


def test_destination_change_preserves_gallery_and_keeps_old_files(tmp_path, server):
    base, requests = server
    engine, storage = destination_engine(tmp_path)
    engine.scan(json.dumps({"url": base + "/threads/1", "workers": 1}))
    wait(engine)
    engine.download(1, "[0]")
    wait(engine)
    first = json.loads(engine.files())[0]
    engine.set_destination("content://qa/tree/b")
    assert json.loads(engine.snapshot())["scan_count"] == 1
    assert len(json.loads(engine.items())) == 3
    engine.download(1, "[0]")
    assert wait(engine)["completed"] == 1
    assert storage.paths[first["uri"]].exists()
    assert requests.count("/one.jpg") == 2
    engine.set_destination("content://qa/tree/a")
    engine.download(1, "[0]")
    assert wait(engine)["skipped"] == 1
    engine.set_destination("")
    engine.download(1, "[0]")
    assert wait(engine)["completed"] == 1
    assert json.loads(engine.files())[0]["path"].endswith("one.jpg")
    assert storage.published == 2


def test_destination_write_failure_is_failed_and_retry_reuses_staging(tmp_path, server):
    base, requests = server
    engine, storage = destination_engine(tmp_path)
    engine.scan(json.dumps({"url": base + "/threads/1", "workers": 1}))
    wait(engine)
    storage.fail_write = True
    engine.download(1, "[0]")
    result = wait(engine)
    assert result["failed"] == 1 and result["completed"] == 0 and result["state"] == "failed"
    assert "pasta escolhida" in " ".join(result["errors"])
    assert len(list(engine.private_dir.rglob("one.jpg"))) == 1
    storage.fail_write = False
    # Resume even after app restart, without refreshing/resolving the file again.
    engine = mobile.Engine(str(engine.private_dir), str(engine.download_dir), storage)
    engine.scan(json.dumps({"url": base + "/threads/1", "workers": 1}))
    wait(engine)
    engine.download(1, "[0]")
    assert wait(engine)["completed"] == 1
    assert requests.count("/one.jpg") == 1
    assert not list(engine.private_dir.rglob("one.jpg"))


def test_destination_missing_file_redownloads_without_overwriting_existing(tmp_path, server):
    base, requests = server
    engine, storage = destination_engine(tmp_path)
    existing = storage.root / "a" / "Mobile QA" / "Photos" / "one.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"unrelated user file")
    engine.scan(json.dumps({"url": base + "/threads/1", "workers": 1}))
    wait(engine)
    engine.download(1, "[0]")
    assert wait(engine)["completed"] == 1
    record = json.loads(engine.files())[0]
    storage.paths[record["uri"]].unlink()
    engine.download(1, "[0]")
    assert wait(engine)["completed"] == 1
    assert requests.count("/one.jpg") == 2
    assert existing.read_bytes() == b"unrelated user file"


def test_destination_permission_error_never_falls_back_to_app(tmp_path, server):
    base, requests = server
    engine, storage = destination_engine(tmp_path)
    engine.scan(json.dumps({"url": base + "/threads/1", "workers": 1}))
    wait(engine)
    storage.denied = True
    engine.download(1, "[0]")
    result = wait(engine)
    assert result["state"] == "error" and result["completed"] == 0 and result["errors"]
    assert requests == ["/threads/1"]
    assert not list(engine.download_dir.iterdir())
    with pytest.raises(OSError):
        engine.set_destination("content://qa/tree/b")
    assert engine.destination["uri"] == "content://qa/tree/a"
    with pytest.raises(OSError):
        engine.files()
    storage.denied = False
    engine.busy = True
    with pytest.raises(ValueError):
        engine.set_destination("")


def test_destination_metadata_failure_rolls_back_new_document_only(tmp_path, server, monkeypatch):
    import destination_storage

    base, requests = server
    engine, storage = destination_engine(tmp_path)
    engine.scan(json.dumps({"url": base + "/threads/1", "workers": 1}))
    wait(engine)
    original = destination_storage.Destination.remember

    def fail_completed(self, key, record):
        if record["status"] == "completed":
            raise OSError("Synthetic receipt failure")
        return original(self, key, record)

    with monkeypatch.context() as context:
        context.setattr(destination_storage.Destination, "remember", fail_completed)
        engine.download(1, "[0]")
        result = wait(engine)
        assert result["failed"] == 1 and result["completed"] == 0
    assert not list(storage.root.rglob("*.jpg"))
    engine.download(1, "[0]")
    assert wait(engine)["completed"] == 1
    assert requests.count("/one.jpg") == 1
