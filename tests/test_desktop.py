import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.desktop import DesktopController
from src.downloaders.media_downloader import MediaDownloader
from src.downloaders import PreparedThread
from src.resolvers import ResolvedMedia


DATA = b'PK\x03\x04' + bytes(range(256)) * 2048


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        fail = True
        slow = 0
        ranges = []
        downloads = []
        heads = []
        temporary_errors = 3
        def log_message(self, *args): pass
        def do_HEAD(self):
            self.heads.append(self.path)
            self.send_response(405)
            self.end_headers()
        def do_GET(self):
            if not self.path.startswith('/threads/'):
                self.downloads.append(self.path)
            if self.path.startswith('/threads/'):
                base = f'http://127.0.0.1:{self.server.server_port}'
                body = (f'<h1 class="p-title-value">Desktop QA</h1><article class="message" id="post-1"><div class="message-body">'
                        f'<a href="{base}/one.zip">One</a><a href="{base}/two.zip">Two</a></div></article>').encode()
                self.send_response(200); self.send_header('Content-Type', 'text/html')
            elif self.path == '/temporary.zip' and type(self).temporary_errors:
                type(self).temporary_errors -= 1
                self.send_response(503); body = b'temporary'
            elif self.path == '/two.zip' and self.fail:
                self.send_response(404); body = b'missing'
            else:
                body = DATA
                header = self.headers.get('Range')
                if header:
                    self.ranges.append(header)
                    start = int(header.split('=')[1].split('-')[0])
                    body = body[start:]
                    self.send_response(206); self.send_header('Content-Range', f'bytes {start}-{len(DATA)-1}/{len(DATA)}')
                else: self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
            self.send_header('ETag', '"desktop-qa-v1"')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            try:
                for offset in range(0, len(body), 65536):
                    self.wfile.write(body[offset:offset+65536])
                    self.wfile.flush()
                    if self.slow: time.sleep(self.slow)
            except (BrokenPipeError, ConnectionResetError): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}', Handler
    server.shutdown()


def finish(controller):
    controller.worker.join(20)
    assert not controller.worker.is_alive()
    assert not controller.error, (controller.error, list(controller.logs))


def options(base, tmp):
    return dict(url=base+'/threads/qa', destination=str(tmp / 'output'), connections=1, retries=0, delay=0)


def test_real_capture_download_retry_history(tmp_path, server):
    base, handler = server
    c = DesktopController(tmp_path/'state')
    c.analyze(options(base, tmp_path), True); finish(c)
    assert [r['status'] for r in c.rows.values()] == ['ok', 'fail']
    assert c.state == 'partial' and c.recovery.pending()
    assert hashlib.sha256(next((tmp_path/'output').rglob('one.zip')).read_bytes()).digest() == hashlib.sha256(DATA).digest()
    handler.fail = False
    c.retry(); finish(c)
    assert [r['status'] for r in c.rows.values()] == ['ok', 'ok']
    assert not c.recovery.pending()
    assert len(DesktopController(tmp_path/'state').history) == 2
    c.download(); finish(c)
    assert all(r['status'] == 'ok' for r in c.rows.values())


def test_recovery_schedules_only_files_missing_from_verified_checkpoint(tmp_path, server):
    base, handler = server
    c = DesktopController(tmp_path/'state')
    c.analyze(options(base, tmp_path), True); finish(c)
    assert [r['status'] for r in c.rows.values()] == ['ok', 'fail']

    recovered = DesktopController(tmp_path/'state')
    pending = recovered.recover(c.recovery.path.name)
    assert pending['download']
    handler.downloads.clear()
    handler.fail = False
    recovered.download(pending['ids']); finish(recovered)
    assert handler.downloads == ['/two.zip']
    assert [r['status'] for r in recovered.rows.values()] == ['ok', 'ok']
    assert any('1 arquivos verificados no disco. 1 ainda na fila.' in line for line in recovered.logs)


def test_bunkr_5xx_refreshes_and_then_retries_once_after_other_files(tmp_path, server, monkeypatch):
    from dataclasses import replace
    import src.downloaders.thread_downloader as engine

    base, handler = server
    c = DesktopController(tmp_path/'state')
    c.options = c._options(dict(options(base, tmp_path), workers=1, connections=2))
    c.url = base+'/threads/qa'
    root = tmp_path/'output'
    source = 'https://bunkr.cr/f/example'
    media = ResolvedMedia(source, 'example', source_url=source, resolver='bunkr')
    c._prepared(PreparedThread('Temporary error', root, [
        (media, root), (ResolvedMedia(base+'/one.zip', 'one.zip'), root)
    ], 1))
    routes = []

    def resolve(item, _client, *, prefer_download_page=False):
        if item.resolver == 'bunkr':
            routes.append(prefer_download_page)
            return replace(item, url=base+'/temporary.zip', filename='temporary.zip')
        return item

    monkeypatch.setattr(engine, 'refresh_media', resolve)
    c.download(); finish(c)
    assert c.state == 'done'
    assert routes == [False, True, True, False]
    assert handler.downloads == ['/temporary.zip'] * 3 + ['/one.zip', '/temporary.zip']
    assert handler.heads == ['/temporary.zip'] * 3 + ['/one.zip']
    assert (root/'Photos'/'temporary.zip').read_bytes() == DATA


def test_saved_bunkr_503_uses_single_stream_on_resume(tmp_path, server, monkeypatch):
    from dataclasses import replace
    import src.downloaders.thread_downloader as engine
    from src.file_utils import save_state

    base, handler = server
    c = DesktopController(tmp_path/'state')
    c.options = c._options(dict(options(base, tmp_path), connections=2))
    c.url = base+'/threads/qa'
    root = tmp_path/'output'
    source = 'https://bunkr.cr/f/example'
    media = ResolvedMedia(source, 'example', source_url=source, resolver='bunkr')
    c._prepared(PreparedThread('Saved error', root, [(media, root)], 1))
    save_state(root, {source: {'status': 'failed', 'http_status': 503}})

    monkeypatch.setattr(engine, 'refresh_media',
                        lambda item, _client: replace(item, url=base+'/one.zip', filename='one.zip'))
    c.download(); finish(c)
    assert c.state == 'done'
    assert handler.heads == []
    assert handler.downloads == ['/one.zip']


def test_file_timings_are_measured_saved_and_exported(tmp_path, server):
    import csv
    from datetime import datetime
    from types import SimpleNamespace

    base, _ = server
    c = DesktopController(tmp_path/'state')
    c.analyze(options(base, tmp_path), True); finish(c)
    restored = DesktopController(tmp_path/'state')
    report = restored.file_timings()
    assert report['total'] == 2
    done = next(item for item in report['items'] if item['status'] == 'ok')
    assert datetime.fromisoformat(done['started']) <= datetime.fromisoformat(done['finished'])
    assert 0 <= done['transfer'] <= done['duration']
    assert done['received'] == done['size'] == len(DATA)
    assert done['host'] == '127.0.0.1' and done['speed'] > 0
    failed = next(item for item in report['items'] if item['status'] == 'fail')
    assert failed['received'] == 0 and failed['speed'] is None

    output = tmp_path/'tempos.csv'
    restored.window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: str(output))
    assert restored.file_timings(True)
    with output.open(encoding='utf-8-sig', newline='') as handle:
        exported = list(csv.DictReader(handle))
    assert {row['Arquivo'] for row in exported} == {'one.zip', 'two.zip'}
    assert all(row['Conclusão da tentativa'] for row in exported)
    assert 'http://' not in output.read_text(encoding='utf-8-sig')


@pytest.mark.parametrize(('restart', 'same_id', 'same_folder'), [
    (False, True, True), (True, True, True), (False, False, True), (False, True, False),
])
def test_bunkr_reuse_checks_identity_and_destination(tmp_path, server, monkeypatch, restart, same_id, same_folder):
    from dataclasses import replace
    import src.downloaders.thread_downloader as engine

    base, handler = server
    root = tmp_path/'output'
    urls = ['https://bunkr.ws/f/example', 'https://bunkr.cr/f/example']
    folders = [root, root if same_folder else root/'other']
    plan = [(ResolvedMedia(url, 'archive.zip', source_url=url, resolver='bunkr'), folder)
            for url, folder in zip(urls, folders)]
    prepared = PreparedThread('Mirrors', root, plan, 1)

    def resolve(media, _client):
        endpoint = '/one.zip' if media.source_url == urls[0] else '/mirror.zip'
        file_id = '42' if same_id or media.source_url == urls[0] else '43'
        return replace(media, url=base+endpoint, bunkr_file_id=file_id)

    monkeypatch.setattr(engine, 'refresh_media', resolve)

    def controller():
        c = DesktopController(tmp_path/'state')
        c.options = c._options(options(base, tmp_path))
        c.url = base+'/threads/qa'
        c._prepared(prepared)
        return c

    c = controller()
    if restart:
        c.download([0]); finish(c)
        c = controller()
        c.download([1]); finish(c)
        assert c.rows[1]['status'] == 'skip'
    else:
        c.download(); finish(c)

    reuse = same_id and same_folder
    transfers = 1 if reuse else 2
    assert len(handler.downloads) == transfers
    assert [p.read_bytes() for p in root.rglob('*.zip')] == [DATA] * transfers
    from src.file_utils import load_state
    state = load_state(root)
    assert (state[urls[0]]['path'] == state[urls[1]]['path']) is reuse
    assert sum(entry['timing']['received'] for entry in state.values()) == len(DATA) * transfers
    if not restart:
        assert sorted(row['status'] for row in c.rows.values()) == (['ok', 'skip'] if reuse else ['ok', 'ok'])


def test_selection_and_recovery(tmp_path, server):
    base, handler = server
    c = DesktopController(tmp_path/'state')
    c.analyze(options(base, tmp_path)); finish(c)
    assert not list((tmp_path/'output').rglob('*.zip'))
    pending = c.bootstrap()['recovery'][0]
    other = DesktopController(tmp_path/'state')
    restored = other.recover(pending['id'])
    assert restored['ids'] == [0, 1] and not restored['download']
    other.download([0]); finish(other)
    assert other.rows[0]['status'] == 'ok' and other.rows[1]['status'] == 'pending'
    assert len(list((tmp_path/'output').rglob('*.zip'))) == 1


def test_retry_resumes_requested_pending_items_only(tmp_path, server):
    base, handler = server
    c = DesktopController(tmp_path/'state')
    c.analyze(options(base, tmp_path)); finish(c)
    prepared = c.prepared
    prepared.plan.append((ResolvedMedia(base+'/unselected.zip', 'unselected.zip'), prepared.root))
    c._prepared(prepared)
    c.download([1]); finish(c)
    c.requested_ids.add(0)  # Simulate a requested item left pending after interruption.
    assert c.poll()['retry_pending']
    handler.fail = False
    c.retry(); finish(c)
    assert [row['status'] for row in c.rows.values()] == ['ok', 'ok', 'pending']
    assert c.state == 'done'


def test_partial_resume_from_saved_queue(tmp_path, server):
    base, _ = server
    c = DesktopController(tmp_path/'state')
    c.analyze(options(base, tmp_path)); finish(c)
    c.recovery.select({c.prepared.plan[0][0].state_key})
    folder = c.prepared.root/'Photos'; folder.mkdir(parents=True)
    work = MediaDownloader._work_path(folder/'one.zip', c.prepared.plan[0][0].state_key, '"desktop-qa-v1"', len(DATA))
    work.with_suffix('.part').write_bytes(DATA[:8192])
    other = DesktopController(tmp_path/'state')
    restored = other.recover(c.recovery.path.name)
    assert restored['ids'] == [0] and restored['download']
    other.download(restored['ids']); finish(other)
    assert (folder/'one.zip').read_bytes() == DATA
    assert server[1].ranges


def test_large_catalog_bounded_updates_and_redacted_report(tmp_path):
    c = DesktopController(tmp_path)
    root = tmp_path/'output'
    c._prepared(PreparedThread('Large', root, [(ResolvedMedia(f'https://host.test/{i}.zip',f'{i}.zip'),root) for i in range(12800)],20))
    assert len(c.catalog(0,999999)['items']) == 500
    p = c.poll()
    assert len(p['items']) == 500 and len(c.dirty) == 12300
    c.log('Cookie: SECRET https://host/secret C:\\Private\\file')
    assert 'SECRET' not in c.report() and 'host/' not in c.report() and 'Private' not in c.report()


@pytest.mark.parametrize('value', [-1, 0, 1.5, float('nan'), 100000])
def test_invalid_workers_rejected(tmp_path, value):
    c = DesktopController(tmp_path)
    with pytest.raises(ValueError): c._options(dict(destination=str(tmp_path/'out'), workers=value))


def test_simulation_no_media_or_recovery(tmp_path, server):
    c = DesktopController(tmp_path/'state')
    c.analyze(dict(options(server[0],tmp_path),dry_run=True),True);finish(c)
    assert not list((tmp_path/'output').rglob('*.zip'))
    assert not c.recovery.pending()


def test_process_crash_and_range_resume(tmp_path, server):
    import subprocess
    import sys
    base, handler = server
    handler.slow = .1
    config = options(base, tmp_path)
    config['include'] = 'one.zip'
    code = f'from src.desktop import DesktopController; c=DesktopController({str(tmp_path / "state")!r}); c.analyze({config!r}, True); c.worker.join()'
    process = subprocess.Popen([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1])
    try:
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            parts = list((tmp_path/'output').rglob('*.part'))
            if parts and parts[0].stat().st_size >= 65536: break
            time.sleep(.01)
        else: raise AssertionError('No partial file before crash')
        process.kill(); process.wait(timeout=5)
        partial_size = parts[0].stat().st_size
        assert 0 < partial_size < len(DATA)
        handler.slow = 0
        c = DesktopController(tmp_path/'state')
        pending = c.bootstrap()['recovery'][0]
        restored = c.recover(pending['id'])
        c.download(restored['ids']); finish(c)
        assert next((tmp_path/'output').rglob('one.zip')).read_bytes() == DATA
        assert f'bytes={partial_size}-' in handler.ranges
        assert not c.recovery.pending()
    finally:
        if process.poll() is None: process.kill(); process.wait()


def test_api_exposes_no_runtime_objects(tmp_path):
    from src.desktop import DesktopAPI
    api = DesktopAPI(DesktopController(tmp_path))
    assert all(callable(getattr(api, name)) for name in dir(api) if not name.startswith('_'))
    assert not hasattr(api, 'window') and not hasattr(api, 'recovery')


def test_simulation_preserves_previous_pending_task(tmp_path, server):
    c = DesktopController(tmp_path/'state')
    c.analyze(options(server[0], tmp_path), True); finish(c)
    previous = c.recovery.path
    assert previous.exists()
    c.analyze(dict(options(server[0], tmp_path), dry_run=True), True); finish(c)
    assert previous.exists()
