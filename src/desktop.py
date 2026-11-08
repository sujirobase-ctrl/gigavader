"""Local desktop controller. Network and durable state stay in Python."""
from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
import re
import threading
import time
import webbrowser
from collections import OrderedDict, deque
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.browser_session import FORUM_ORIGIN, SESSION_SUFFIX, expiry_info, load_session, logged_in, save_session, trusted_origin
from src.config import SkipReason, TaskResult
from src.downloaders import PreparedThread, ThreadDownloader, ThreadOptions
from src.downloaders.media_downloader import TRANSIENT_HTTP_STATUSES
from src.file_utils import load_state
from src.http_client import HttpClient
from src.preview import fetch_thumbnail, media_kind
from src.recovery import RecoveryStore
from src.video_preview import fetch_video_preview

VERSION = '2.0.0'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def safe_report(logs):
    text = '\n'.join(logs)
    text = re.sub(r'https?://\S+', '[link oculto]', text)
    text = re.sub(r'[A-Za-z]:[\\/][^\n]*', '[pasta oculta]', text)
    text = re.sub(r'(?i)(cookie|authorization|token|password|senha)[^\n]*', '[credencial oculta]', text)
    return text


class DesktopManager:
    def __init__(self, controller, ids):
        self.owner, self.ids, self.tasks = controller, ids, {}
        self.timing = {}

    def start(self): pass
    def stop(self): pass
    def add_overall_task(self, description, num_tasks): pass
    def update_summary(self, *args, **kwargs): pass

    def item_started(self, index):
        with self.owner.lock:
            row = self.owner.rows[self.ids[index]]
            self.timing[index] = {
                'started': datetime.now().astimezone().isoformat(timespec='seconds'),
                'began': time.monotonic(), 'transfer': 0.0, 'host': row['host'], 'received': 0,
            }
            row.update(status='down', error='')
            self.owner.dirty.add(row['id'])

    def item_result(self, index, outcome):
        with self.owner.lock:
            row = self.owner.rows[self.ids[index]]
            timing = self.timing.pop(index, None)
            if outcome is None:
                row['status'] = 'pending' if self.owner.cancel.is_set() else 'skip'
            else:
                row['status'] = {TaskResult.COMPLETED: 'ok', TaskResult.SKIPPED: 'skip', TaskResult.FAILED: 'fail'}[outcome.result]
                row['error'] = outcome.message or (outcome.reason.name if outcome.reason else '')
                if outcome.result is TaskResult.FAILED:
                    row['error'] = ThreadDownloader._failure_message(outcome.reason, outcome.message)
                if outcome.path and outcome.path.exists():
                    row['size'] = outcome.path.stat().st_size
                    row['name'] = outcome.path.name
                if row['status'] in ('ok', 'skip'):
                    row['progress'] = 100
            self.owner.dirty.add(row['id'])
            if timing is None or outcome is None or outcome.reason is SkipReason.DRY_RUN:
                return None
            transfer = timing['transfer']
            received = timing['received']
            return {
                'name': row['name'], 'host': timing['host'], 'status': row['status'],
                'started': timing['started'], 'finished': datetime.now().astimezone().isoformat(timespec='seconds'),
                'duration': round(time.monotonic() - timing['began'], 3), 'transfer': round(transfer, 3),
                'received': received, 'speed': round(received / transfer) if transfer and received else None,
                'size': row['size'], 'note': safe_report([row['error']]),
            }

    def item_transfer(self, index, seconds, host, filename):
        with self.owner.lock:
            timing = self.timing[index]
            timing['transfer'] += seconds
            if host:
                timing['host'] = host
            if filename:
                self.owner.rows[self.ids[index]]['name'] = filename

    def item_bytes(self, index, amount):
        with self.owner.lock:
            self.timing[index]['received'] += amount

    def add_task(self, current_task=0, total=100):
        with self.owner.lock:
            key = len(self.tasks)
            self.tasks[key] = [self.ids[current_task], total, 0]
            return key

    def update_task(self, task_id, *, advance=None, completed=None, **kwargs):
        with self.owner.lock:
            ident, total, old = self.tasks[task_id]
            current = completed if completed is not None else old + (advance or 0)
            self.tasks[task_id][2] = current
            self.owner.rows[ident]['progress'] = min(100, current / max(total, 1) * 100)
            self.owner.dirty.add(ident)

    def update_log(self, *, event, details, verbose_only=False):
        if not verbose_only:
            self.owner.log(event + ': ' + details)


class DesktopController:
    def __init__(self, directory=None, *, default_destination=None):
        self.directory = Path(directory) if directory else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'BUIDL' / 'desktop-1.4'
        self.default_destination = default_destination or Path.home() / 'Downloads' / 'BUIDL'
        self.recovery = RecoveryStore(self.directory / 'recovery')
        self.lock = threading.RLock()
        self.capture_lock = threading.Lock()
        self.preview_lock = threading.BoundedSemaphore(3)
        self.video_lock = threading.Lock()
        self.window = None
        self.worker = None
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.pause.set()
        self.preview_stop = threading.Event()
        self.cache = OrderedDict()
        self.logs = deque(maxlen=500)
        self.rows, self.dirty = {}, set()
        self.prepared = None
        self.options = None
        self.settings = read_json(self.directory / 'settings.json', {})
        self.history = read_json(self.directory / 'history.json', [])
        self.state, self.error, self.title = 'idle', '', ''
        self.started = 0
        self.elapsed = 0
        self.url, self.cookies = '', self.settings.get('cookies') or None
        self.generation = 0
        self.job_ids = []
        self.requested_ids = set()
        self.client = None

    def log(self, message):
        with self.lock:
            self.logs.append(time.strftime('%H:%M:%S') + '  ' + message)

    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def bootstrap(self):
        return {'version': VERSION, 'settings': self.settings, 'destination': str(self.default_destination),
                'cookie_status': self.cookie_status(),
                'history': self.history, 'recovery': [{'id': Path(d['_path']).name, 'title': d.get('prepared', {}).get('title', d['url']),
                'count': len(d.get('prepared', {}).get('plan', [])), 'destination': d['options'].get('download_path'),
                'updated': d.get('updated')} for d in self.recovery.pending()]}

    def _options(self, config):
        def number(key, default, low, high, integer=False):
            value = float(config.get(key, default))
            if not math.isfinite(value) or not low <= value <= high or (integer and value != int(value)):
                raise ValueError('Valor inválido: ' + key)
            return int(value) if integer else value
        destination = Path(config.get('destination') or str(self.default_destination)).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        import tempfile
        with tempfile.TemporaryFile(dir=destination): pass
        return ThreadOptions(download_path=str(destination), all_pages=bool(config.get('all_pages', True)),
            max_pages=number('max_pages', 3, 1, 10000, True), workers=number('workers', 3, 1, 16, True),
            connections=number('connections', 2, 1, 8, True), max_retries=number('retries', 3, 0, 10, True),
            delay=number('delay', 1, 0, 60), rate_limit=number('rate', 0, 0, 10000) * 1024 * 1024 or None,
            dry_run=bool(config.get('dry_run')), separate_posts=bool(config.get('separate_posts')), flat=bool(config.get('flat')),
            include=[s.strip() for s in config.get('include', '').split(',') if s.strip()],
            ignore=[s.strip() for s in config.get('exclude', '').split(',') if s.strip()],
            cancel_event=self.cancel, pause_event=self.pause)

    def _launch(self, function):
        self.cancel.clear()
        self.pause.set()
        self.error = ''
        self.started = time.monotonic()
        def run():
            try:
                function()
            except Exception as error:
                with self.lock:
                    self.error = str(error)
                    self.state = 'error'
                    self.log('Erro: ' + str(error))
            finally:
                with self.lock:
                    self.elapsed = time.monotonic() - self.started
                    if self.cancel.is_set(): self.state = 'cancelled'
        self.worker = threading.Thread(target=run, daemon=True, name='buidl-desktop')
        self.worker.start()

    def analyze(self, config, download=False):
        with self.lock:
            if self._busy(): raise ValueError('Aguarde ou cancele a tarefa atual.')
            url = str(config.get('url', '')).strip()
            if urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc:
                raise ValueError('Informe uma URL HTTP ou HTTPS válida.')
            self.options = self._options(config)
            cookies = str(config.get('cookies', '')).strip() or None
            if cookies:
                with HttpClient(cookies_file=cookies): pass
            self.settings = dict(config)
            atomic_json(self.directory / 'settings.json', self.settings)
            self.url, self.cookies = url, cookies
            self.preview_stop.set()
            self.preview_stop = threading.Event()
            self.cache.clear()
            self.prepared = None
            self.rows.clear()
            self.dirty.clear()
            self.job_ids = []
            self.requested_ids.clear()
            self.state = 'scanning'
            # A new task owns a new snapshot; simulations must not clear an
            # earlier interrupted task's durable recovery document.
            self.recovery = RecoveryStore(self.recovery.directory)
            if not self.options.dry_run and config.get('recovery', True):
                self.recovery.begin(url, cookies, self.options, not download)
            def work():
                with HttpClient(cookies_file=cookies) as client:
                    self.client = client
                    manager = DesktopManager(self, [])
                    downloader = ThreadDownloader(client, manager, self.options)
                    prepared = downloader.prepare_thread(url)
                    if self.cancel.is_set(): return
                    if prepared is None or not prepared.plan:
                        raise ValueError('Nenhum arquivo encontrado. Confira o registro de atividades e os filtros.')
                    self._prepared(prepared)
                    if not self.options.dry_run and config.get('recovery', True): self.recovery.save_prepared(prepared, not download)
                    if download:
                        self.requested_ids = set(self.rows)
                        self._download(list(self.rows), client)
                    else: self.state = 'gallery'
                self.client = None
            self._launch(work)
        return True

    def _prepared(self, prepared):
        with self.lock:
            self.prepared, self.title = prepared, prepared.title
            self.requested_ids.clear()
            self.generation += 1
            kinds = {'Imagens': 'img', 'Vídeos': 'vid', 'Áudios': 'audio', 'Arquivos': 'arq'}
            self.rows = {i: {'id': i, 'name': m.filename or Path(urlparse(m.url).path).name or 'Arquivo',
                'type': kinds[media_kind(m)], 'host': urlparse(m.origin_url or m.state_key).hostname or '',
                'post': m.post_label, 'status': 'pending', 'progress': 0, 'size': None, 'error': ''}
                for i, (m, _) in enumerate(prepared.plan)}
            self.dirty = set(self.rows)

    def catalog(self, offset=0, limit=200):
        with self.lock:
            offset, limit = max(0, int(offset)), max(1, min(500, int(limit)))
            return {'generation': self.generation, 'total': len(self.rows), 'title': self.title,
                    'posts': self.prepared.post_count if self.prepared else 0,
                    'items': [dict(self.rows[i]) for i in range(offset, min(offset + limit, len(self.rows)))]}

    def poll(self):
        with self.lock:
            changed = list(self.dirty)[:500]
            self.dirty.difference_update(changed)
            active, waiting = [], []
            remaining = 0
            for ident, row in self.rows.items():
                if ident not in self.requested_ids or row['status'] not in ('pending', 'down'):
                    continue
                remaining += 1
                group = active if row['status'] == 'down' else waiting
                if len(group) < 50:
                    group.append(row)
            return {'state': self.state, 'error': self.error, 'generation': self.generation,
                'items': [dict(self.rows[i]) for i in changed], 'logs': list(self.logs),
                'queue': [dict(row) for row in (active + waiting)[:50]], 'remaining': remaining,
                'retry_pending': bool(remaining),
                'elapsed': time.monotonic() - self.started if self._busy() else self.elapsed,
                'history': self.history, 'saved': self.recovery.document.get('updated') if self.recovery.document else None}

    def download(self, ids=None, continuation=False):
        with self.lock:
            if self._busy(): raise ValueError('Uma operação já está em andamento.')
            if not self.prepared: raise ValueError('Analise uma thread primeiro.')
            ids = list(self.rows) if ids is None else list(dict.fromkeys(int(i) for i in ids))
            if not ids or any(i not in self.rows for i in ids): raise ValueError('Seleção inválida.')
            if not continuation: self.requested_ids = set(ids)
            self.state = 'downloading'
            def work():
                with HttpClient(cookies_file=self.cookies) as client:
                    self.client = client
                    self._download(ids, client)
                self.client = None
            self._launch(work)
        return True

    def _download(self, ids, client):
        with self.lock:
            self.job_ids = ids
            for i in ids:
                self.rows[i].update(status='pending', error='', progress=0, size=None)
                self.dirty.add(i)
            keys = {self.prepared.plan[i][0].state_key for i in self.requested_ids}
            if not self.options.dry_run and self.settings.get('recovery', True):
                if self.recovery.document is None:
                    self.recovery.begin(self.url, self.cookies, self.options, False)
                    self.recovery.save_prepared(self.prepared, False)
                self.recovery.select(keys)
        prepared = self.prepared
        state = {} if self.options.dry_run else load_state(prepared.root)
        verified = {}
        for i in ids:
            if self.cancel.is_set():
                break
            cached = state.get(prepared.plan[i][0].state_key)
            if ThreadDownloader._cached_file_is_complete(cached, prepared.root):
                verified[i] = cached
        pending = [i for i in ids if i not in verified]
        with self.lock:
            if self.state == 'scanning':
                self.state = 'downloading'
            for i, cached in verified.items():
                self.rows[i].update(status='ok', progress=100, size=cached['size'], name=Path(cached['path']).name)
            self.dirty.update(verified)
            if verified:
                self.log(f'Retomada: {len(verified)} arquivos verificados no disco. {len(pending)} ainda na fila.')
        def transient_bunkr(ident, records):
            media, _ = prepared.plan[ident]
            record = records.get(media.state_key)
            return (media.resolver == 'bunkr' and isinstance(record, dict)
                    and record.get('status') == 'failed'
                    and record.get('http_status') in TRANSIENT_HTTP_STATUSES)

        def run_batch(batch, options):
            subset = PreparedThread(prepared.title, prepared.root, [prepared.plan[i] for i in batch], prepared.post_count)
            manager = DesktopManager(self, batch)
            return ThreadDownloader(client, manager, options, reuse_bunkr=True).download_prepared(subset)

        deferred = [i for i in pending if transient_bunkr(i, state)]
        deferred_ids = set(deferred)
        regular = [i for i in pending if i not in deferred_ids]
        failed = run_batch(regular, self.options) if regular and not self.cancel.is_set() else False
        newly_deferred = []
        if failed and not self.cancel.is_set() and not self.options.dry_run:
            state = load_state(prepared.root)
            with self.lock:
                for i in regular:
                    if self.rows[i]['status'] == 'fail' and transient_bunkr(i, state):
                        newly_deferred.append(i)
                        self.rows[i].update(status='pending', error='', progress=0, size=None)
                self.dirty.update(newly_deferred)
        deferred.extend(newly_deferred)
        if deferred and not self.cancel.is_set():
            self.log(f'Bunkr: retomada com uma conexão para {len(deferred)} itens com erro temporário.')
            retry_failed = run_batch(deferred, replace(self.options, workers=1, connections=1))
            failed = retry_failed or (failed and not newly_deferred)
        with self.lock:
            for i in ids:
                if self.rows[i]['status'] == 'down':
                    self.rows[i]['status'] = 'pending'
                    self.dirty.add(i)
            unresolved = any(self.rows[i]['status'] not in ('ok', 'skip') for i in self.requested_ids)
            failed = failed or unresolved
            self.state = 'cancelled' if self.cancel.is_set() else 'partial' if failed else 'done'
            if not failed and not self.cancel.is_set(): self.recovery.complete()
            record = {'title': self.title, 'date': time.strftime('%d/%m/%Y %H:%M'), 'status': self.state,
                      'completed': sum(self.rows[i]['status'] in ('ok', 'skip') for i in ids),
                      'total': len(ids), 'path': str(self.prepared.root), 'simulation': self.options.dry_run}
            self.history.insert(0, record)
            self.history = self.history[:200]
            atomic_json(self.directory / 'history.json', self.history)

    def pause_download(self):
        with self.lock:
            if self.state == 'downloading': self.pause.clear(); self.state = 'paused'
            elif self.state == 'paused': self.pause.set(); self.state = 'downloading'
            return self.state

    def cancel_download(self):
        self.cancel.set()
        self.pause.set()
        self.preview_stop.set()
        self.state = 'cancelling' if self._busy() else 'cancelled'
        return True

    def retry(self, ids=None):
        with self.lock:
            if ids is None:
                retry_ids = [i for i, row in self.rows.items() if i in self.requested_ids
                             and row['status'] in ('fail', 'pending', 'down')]
            else:
                retry_ids = [i for i in ids if i in self.requested_ids and self.rows[i]['status'] == 'fail']
            return self.download(retry_ids, continuation=True)

    def recover(self, ident, discard=False):
        with self.lock:
            if self._busy(): raise ValueError('Aguarde a operação atual.')
            document = next(d for d in self.recovery.pending() if Path(d['_path']).name == ident)
            if discard:
                Path(document['_path']).unlink(missing_ok=True)
                return True
            allowed = {f.name for f in fields(ThreadOptions)} - {'cancel_event', 'pause_event'}
            self.options = ThreadOptions(**{k: v for k, v in document['options'].items() if k in allowed}, cancel_event=self.cancel, pause_event=self.pause)
            self.url, self.cookies = document['url'], document.get('cookies_file')
            self.recovery.begin(self.url, self.cookies, self.options, document['preview'], document)
            prepared = RecoveryStore.restore_prepared(document)
            if prepared is None:
                config = dict(self.settings, url=self.url, cookies=self.cookies or '', destination=self.options.download_path)
                return self.analyze(config, not document['preview'])
            self._prepared(prepared)
            selection = document.get('selection')
            self.state = 'gallery'
            return {'ids': [i for i, (m, _) in enumerate(prepared.plan) if selection is None or m.state_key in selection],
                    'download': document.get('phase') != 'selection'}

    def choose(self, kind):
        import webview
        if kind == 'folder':
            result = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        else:
            result = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=('Cookies Netscape (*.txt;*.cookies)',))
        return result[0] if result else None

    def update_cookies(self, path):
        path = str(path or '').strip() or None
        with HttpClient(cookies_file=path): pass
        with self.lock:
            if self.client:
                if not path: raise ValueError('Selecione um arquivo de cookies.')
                self.client.load_cookies(path)
            self.cookies = path
            self.settings['cookies'] = path or ''
            atomic_json(self.directory / 'settings.json', self.settings)
            if self.recovery.document:
                with self.recovery.lock:
                    self.recovery.document['cookies_file'] = path
                    self.recovery._write()
            self.log('Cookies carregados; a validade será verificada nas respostas dos sites.')
        return True

    def capture_cookies(self, url=None):
        from src.windows_login import capture_in_window

        if not self.capture_lock.acquire(blocking=False):
            raise ValueError('A janela de login já está aberta.')
        try:
            if self._busy():
                raise ValueError('Aguarde a operação atual antes de capturar a sessão.')
            snapshot = capture_in_window(self.window.native, trusted_origin(url) or FORUM_ORIGIN)
            if snapshot is None:
                return None
            path = self.directory / ('simpcity' + SESSION_SUFFIX)
            save_session(path, snapshot)
            self.update_cookies(str(path))
            return self.cookie_status(verify=True)
        finally:
            self.capture_lock.release()

    def cookie_status(self, verify=False):
        path = Path(self.settings['cookies']) if self.settings.get('cookies') else None
        if path is None:
            return {'state': 'missing'}
        captured = path.suffix.lower() == SESSION_SUFFIX
        origin = trusted_origin(self.settings.get('url')) or FORUM_ORIGIN
        status = {'path': str(path), 'captured': captured}
        try:
            if captured:
                snapshot = load_session(path)
                origin = snapshot['origin']
                status['expires'] = expiry_info(snapshot)
            stat = path.stat()
        except (OSError, ValueError):
            return {**status, 'state': 'unreadable'}

        source = {'path': str(path), 'stamp': [stat.st_mtime_ns, stat.st_size], 'origin': origin}
        check_file = self.directory / 'cookie-check.json'
        if not verify:
            last = read_json(check_file, {})
            if last.get('source') == source:
                return {**status, **last['check']}
            return {**status, 'state': 'unchecked'}

        state = 'unknown'
        try:
            with HttpClient(cookies_file=str(path), timeout=8, max_retries=1) as client:
                with client.get(origin, stream=True) as response:
                    if response.status_code == 200 and trusted_origin(response.url):
                        data = bytearray()
                        for chunk in response.iter_content(32768):
                            data.extend(chunk)
                            if len(data) >= 512 * 1024:
                                break
                        session = logged_in(BeautifulSoup(bytes(data), 'html.parser'))
                        if session is not None:
                            state = 'valid' if session else 'signed_out'
        except (requests.RequestException, OSError, ValueError):
            pass
        check = {'state': state, 'checked_at': datetime.now().astimezone().isoformat(timespec='seconds')}
        with self.lock:
            atomic_json(check_file, {'source': source, 'check': check})
        return {**status, **check}

    def open_folder(self, history_index=None):
        path = Path(self.history[int(history_index)]['path']) if history_index is not None else self.prepared.root if self.prepared else Path(self.settings.get('destination') or self.default_destination)
        if not path.is_dir(): raise ValueError('A pasta ainda não foi criada.')
        os.startfile(str(path.resolve()))
        return True

    def open_origin(self, ident):
        media = self.prepared.plan[int(ident)][0]
        url = media.source_url or media.origin_url or media.url
        if urlparse(url).scheme not in ('http', 'https'): raise ValueError('Link inválido.')
        webbrowser.open(url)
        return True

    def preview(self, ident, video=False, refresh=False):
        ident = int(ident)
        with self.video_lock if video else self.preview_lock:
            with self.lock:
                generation, stop = self.generation, self.preview_stop
                key = (generation, ident, bool(video))
                if key in self.cache and not refresh: return self.cache[key]
                media = self.prepared.plan[ident][0]
            if video:
                result = fetch_video_preview(media, self.cookies, stop)
                images, message = result.frames, result.message
            else:
                image = fetch_thumbnail(media, self.cookies, stop)
                if image is None and media_kind(media) == 'Vídeos' and not stop.is_set():
                    with self.video_lock:
                        cover = fetch_video_preview(media, self.cookies, stop, cover_only=True)
                    image = cover.frames[0] if cover.frames else None
                images, message = ([image] if image else []), ('' if image else 'Prévia indisponível; o download ainda pode funcionar.')
            encoded = []
            for image in images:
                out = io.BytesIO()
                image.save(out, format='JPEG', quality=75)
                encoded.append('data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode('ascii'))
            value = {'frames': encoded, 'message': message}
            with self.lock:
                if encoded and generation == self.generation and not stop.is_set():
                    self.cache[key] = value
                    # Keep only two decoded video previews in the encoded cache;
                    # cap total base64 payload as well as the entry count.
                    videos = [k for k in self.cache if k[2]]
                    while len(videos) > 2: self.cache.pop(videos.pop(0), None)
                    def cache_size():
                        return sum(sum(len(frame) for frame in item['frames']) for item in self.cache.values())
                    while self.cache and (len(self.cache) > 32 or cache_size() > 16 * 1024 * 1024):
                        self.cache.popitem(last=False)
            return value if not stop.is_set() else {'frames': [], 'message': 'Prévia cancelada.'}

    def stop_preview(self):
        self.preview_stop.set()
        self.preview_stop = threading.Event()
        return True

    def image_preview(self, ident):
        """Load a larger image on demand, without changing the download queue."""
        with self.preview_lock:
            with self.lock:
                media = self.prepared.plan[int(ident)][0]
                stop = self.preview_stop
            if media_kind(media) != 'Imagens':
                raise ValueError('Este arquivo não é uma imagem.')
            # Direct images use the original URL. Renewable files retain their
            # supplied preview, avoiding downloads/signing of large originals.
            source = media if media.resolver else replace(media, thumbnail_url=None)
            image = fetch_thumbnail(source, self.cookies, stop,
                                    max_bytes=16 * 1024 * 1024, size=(1600, 1600))
            if image is None or stop.is_set():
                return {'image': None, 'message': 'Não foi possível carregar a imagem. Ela pode estar bloqueada ou ultrapassar o limite de prévia de 16 MB. O download continua disponível.'}
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=90)
            return {'image': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii'),
                    'message': 'Imagem inteira, ajustada à janela.' if not media.resolver else 'Prévia disponível do host, ajustada à janela.'}

    def copy_text(self, text):
        from System import Action
        from System.Windows.Forms import Clipboard

        if not text:
            return False
        self.window.native.Invoke(Action(lambda: Clipboard.SetText(text)))
        return True

    def report(self, export=False):
        with self.lock:
            # Export aggregate data only; filenames, paths, hosts and raw logs stay local.
            text = f'BUIDL {VERSION}\nEstado: {self.state}\nArquivos: {len(self.rows)}\n'
            text += '\n'.join(f'{status}: {sum(r["status"] == status for r in self.rows.values())}' for status in ('pending', 'down', 'ok', 'skip', 'fail'))
            text += '\nCookies: ' + ('arquivo informado; validade não presumida' if self.cookies else 'não informado')
        if export:
            import webview
            path = self.window.create_file_dialog(webview.FileDialog.SAVE, save_filename='BUIDL-diagnostico.txt')
            if path:
                Path(path[0] if isinstance(path, (list, tuple)) else path).write_text(text, encoding='utf-8')
        return text

    def file_timings(self, export=False):
        with self.lock:
            root = self.prepared.root if self.prepared else Path(self.history[0]['path']) if self.history else None
        state = load_state(root) if root else {}
        records = [entry['timing'] for entry in state.values()
                   if isinstance(entry, dict) and isinstance(entry.get('timing'), dict)]
        records.sort(key=lambda entry: entry.get('finished', ''), reverse=True)
        if not export:
            return {'items': records[:50], 'total': len(records)}

        if not records:
            raise ValueError('Ainda não há tempos registrados para esta fila.')
        import webview
        target = self.window.create_file_dialog(webview.FileDialog.SAVE, save_filename='BUIDL-tempos.csv')
        if not target:
            return False
        columns = (
            ('name', 'Arquivo'), ('host', 'Servidor'), ('status', 'Resultado'),
            ('started', 'Início'), ('finished', 'Conclusão da tentativa'),
            ('duration', 'Tempo total (s)'), ('transfer', 'Etapa de download (s)'),
            ('size', 'Tamanho final (bytes)'), ('received', 'Bytes de mídia recebidos'),
            ('speed', 'Vazão na etapa de download (bytes/s)'), ('note', 'Observação'),
        )
        with Path(target[0] if isinstance(target, (tuple, list)) else target).open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow([label for _, label in columns])
            for entry in records:
                values = [entry.get(key) for key, _ in columns]
                writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@'))
                                 else value for value in values])
        return True

    def close(self):
        if self._busy() and self.window and not self.window.create_confirmation_dialog('Sair do BUIDL?', 'O andamento salvo poderá ser retomado. Deseja fechar?'):
            return False
        self.cancel_download()
        if self.worker and self.worker.is_alive(): self.worker.join(timeout=3)
        return True


class DesktopAPI:
    """Expose only the explicit UI operations, never controller/runtime objects."""
    def __init__(self, controller):
        self._controller = controller


def _api_method(name):
    def invoke(self, *args):
        return getattr(self._controller, name)(*args)
    return invoke


for _name in ('bootstrap', 'analyze', 'catalog', 'poll', 'download', 'pause_download',
              'cancel_download', 'retry', 'recover', 'choose', 'update_cookies', 'copy_text', 'capture_cookies', 'cookie_status',
              'open_folder', 'open_origin', 'preview', 'image_preview', 'stop_preview', 'report', 'file_timings'):
    setattr(DesktopAPI, _name, _api_method(_name))
