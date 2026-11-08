"""Android adapter for the unchanged desktop download engine (no Tk or Pillow)."""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import tempfile
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import requests
from destination_storage import Destination, DestinationDownloader, atomic_json

from src.config import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, TaskResult
from src.downloaders import ThreadDownloader, ThreadOptions
from src.http_client import HttpClient
from src.resolvers import refresh_media
from src.recovery import RecoveryStore

CAPTURE_PREFIX = "# BUIDL-WebView: "
CAPTURE_HOSTS = {"simpcity.cr", "www.simpcity.cr"}


def valid_user_agent(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and all(32 <= ord(c) <= 126 for c in value)


def kind(media):
    suffix = Path(media.filename or urlparse(media.url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "Imagens"
    if suffix in VIDEO_EXTENSIONS or media.resolver == "turbo":
        return "Vídeos"
    if suffix in AUDIO_EXTENSIONS:
        return "Áudios"
    return "Arquivos"


def redacted(text):
    return re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[oculto]", str(text))


class MobileManager:
    def __init__(self, engine):
        self.engine = engine

    def update_log(self, event="", details="", verbose_only=False, **_kwargs):
        if verbose_only:
            return
        message = redacted(f"{event}: {details}")
        with self.engine.lock:
            self.engine.logs.append(message)
            if any(word in event.lower() for word in ("failed", "error", "unresolved", "unsupported", "empty thread")):
                self.engine.errors.append(message)

    def update_summary(self, result, reason=None):
        name = {TaskResult.COMPLETED: "completed", TaskResult.SKIPPED: "skipped", TaskResult.FAILED: "failed"}[result]
        with self.engine.lock:
            self.engine.counts[name] += 1

    def add_overall_task(self, title, num_tasks):
        with self.engine.lock:
            self.engine.total = num_tasks

    def item_started(self, index):
        with self.engine.lock:
            self.engine.rows[self.engine.active_ids[index]].update(status="downloading", error="")

    def item_result(self, index, outcome):
        with self.engine.lock:
            row = self.engine.rows[self.engine.active_ids[index]]
            if outcome is None:
                row.update(status="pending")
            else:
                state = {TaskResult.COMPLETED: "completed", TaskResult.SKIPPED: "skipped", TaskResult.FAILED: "failed"}[outcome.result]
                row.update(status=state, error=redacted(outcome.message or (str(outcome.reason.value) if outcome.reason else "")))
                if state in {"completed", "skipped"}: row["pct"] = 100

    def add_task(self, index, total=0, **_kwargs):
        with self.engine.lock:
            if index < len(self.engine.active_ids):
                self.engine.rows[self.engine.active_ids[index]].update(size=total, done=0)
        return index

    def update_task(self, task_id, **kwargs):
        with self.engine.lock:
            if task_id is None or task_id >= len(self.engine.active_ids): return
            row = self.engine.rows[self.engine.active_ids[task_id]]
            if "total" in kwargs: row["size"] = kwargs["total"] or 0
            row["done"] = kwargs.get("completed", row.get("done", 0) + kwargs.get("advance", 0))
            row["pct"] = min(100, int(100 * row["done"] / row["size"])) if row.get("size") else 0


class Engine:
    def __init__(self, private_dir, download_dir, storage_backend=None):
        self.private_dir = Path(private_dir)
        self.download_dir = Path(download_dir)
        self.private_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_path = self.private_dir / "cookies.txt"
        self.storage_backend = storage_backend
        self.destination_path = self.private_dir / "destination.json"
        self.destination = json.loads(self.destination_path.read_text(encoding="utf-8")) if self.destination_path.exists() else {}
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.prepared = None
        self.fingerprint = None
        self.queue_id = 0
        self.scan_count = 0
        self.state = "idle"
        self.message = "Pronto para analisar uma thread"
        self.busy = False
        self.logs = deque(maxlen=150)
        self.errors = deque(maxlen=300)
        self.counts = dict(completed=0, skipped=0, failed=0)
        self.total = 0
        self.worker = None
        self.settings = {}
        self.manager = MobileManager(self)
        self.recovery = RecoveryStore(self.private_dir / "recovery")
        self.rows, self.active_ids, self.selection = [], [], []
        self.started = time.monotonic()
        self.history_path = self.private_dir / "history.json"
        try: self.history = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError): self.history = []
        self.recoveries = self.recovery.pending()

    def _client(self):
        options = {}
        if self.cookie_path.is_file():
            # Metadata and cookies share one atomic file, so a session never uses
            # a user agent left over from a different import/capture.
            with self.cookie_path.open(encoding="utf-8") as source:
                source.readline(4096)
                metadata = source.readline(4096)
            if metadata.startswith(CAPTURE_PREFIX):
                try:
                    agent = json.loads(metadata[len(CAPTURE_PREFIX):])["user_agent"]
                    if valid_user_agent(agent):
                        options["user_agent"] = agent
                except (ValueError, KeyError, TypeError):
                    pass
        return HttpClient(cookies_file=str(self.cookie_path) if self.cookie_path.is_file() else None,
                          timeout=15, max_retries=2, **options)

    def _options(self, settings):
        def number(key, default, low, high, integer=False):
            value = float(settings.get(key, default))
            if not math.isfinite(value) or not low <= value <= high or (integer and value != int(value)):
                raise ValueError("Valor inválido: " + key)
            return int(value) if integer else value
        return ThreadOptions(download_path=str(self.download_dir),
            all_pages=bool(settings.get("all_pages", True)),
            workers=number("workers", 2, 1, 16, True),
            connections=number("connections", 2, 1, 8, True),
            max_retries=number("retries", 3, 0, 10, True), delay=number("delay", 1, 0, 60),
            max_pages=number("max_pages", 3, 1, 10000, True),
            rate_limit=number("rate", 0, 0, 10000) * 1024 * 1024 or None,
            flat=bool(settings.get("flat")), dry_run=bool(settings.get("dry_run")),
            separate_posts=bool(settings.get("separate_posts", False)),
            include=list(settings.get("include", [])), ignore=list(settings.get("ignore", [])),
            cancel_event=self.cancel_event, pause_event=self.pause_event)

    def _save_session(self, preview=True):
        if self.settings.get("dry_run") or not self.settings.get("recovery", True): return
        if self.recovery.document is None:
            self.recovery = RecoveryStore(self.private_dir / "recovery")
            self.recovery.begin(self.settings["url"], str(self.cookie_path), self._options(self.settings), preview)
        self.recovery.document["mobile_settings"] = dict(self.settings)
        self.recovery.document["mobile_destination"] = dict(self.destination)
        if self.prepared is not None: self.recovery.save_prepared(self.prepared, preview)
        else: self.recovery._write()
        self.recoveries = self.recovery.pending()

    def select(self, queue_id, indices_json):
        with self.lock:
            if self.busy or self.prepared is None or queue_id != self.queue_id: return
            indices = json.loads(indices_json)
            self.selection = sorted({int(i) for i in indices if 0 <= int(i) < len(self.prepared.plan)})
            if self.recovery.document:
                self.recovery.document["selection"] = [self.prepared.plan[i][0].state_key for i in self.selection]
                self.recovery._write()

    def recover(self, identifier, run=False):
        with self.lock:
            if self.busy: raise ValueError("Aguarde a operação atual.")
            document = next((d for d in self.recovery.pending() if Path(d["_path"]).name == identifier), None)
            if document is None: raise ValueError("Sessão indisponível.")
            self.settings = document.get("mobile_settings", {})
            if not self.settings: raise ValueError("Sessão de outra versão.")
            self.destination = document.get("mobile_destination", {})
            atomic_json(self.destination_path, self.destination)
            self.recovery.begin(document["url"], str(self.cookie_path), self._options(self.settings), True, resume=document)
            self.prepared = self.recovery.restore_prepared(document)
            if self.prepared is None:
                self.scan(json.dumps(self.settings), True, run)
                return
            self.queue_id += 1
            self._reset_rows()
            selected = document.get("selection")
            self.selection = [i for i, (m, _) in enumerate(self.prepared.plan) if selected is None or m.state_key in selected]
            self.state, self.message = "ready", "Sessão recuperada. Arquivos concluídos serão verificados e reutilizados."
        if run and self.selection: self.download(self.queue_id, json.dumps(self.selection))

    def _reset_rows(self):
        self.rows = [dict(index=i, name=m.filename, kind=kind(m), post=m.post_label,
            host=urlparse(m.origin_url or m.state_key).hostname or "", status="pending", pct=0, error="")
            for i, (m, _) in enumerate(self.prepared.plan)]

    def queue(self, offset=0, status="Todos"):
        with self.lock:
            rows = [r for r in self.rows if status == "Todos" or r["status"] == status]
            return json.dumps(dict(total=len(rows), rows=rows[max(0, int(offset)):max(0, int(offset))+50]), ensure_ascii=False)

    def retry(self, indices_json="null"):
        ids = json.loads(indices_json)
        if ids is None: ids = [r["index"] for r in self.rows if r["status"] == "failed"]
        if not ids: raise ValueError("Nenhuma falha para repetir.")
        self.download(self.queue_id, json.dumps(ids), retry=True)

    def history_json(self):
        with self.lock: return json.dumps(self.history, ensure_ascii=False)

    def report(self):
        with self.lock:
            return json.dumps(dict(version="2.0.0", state=self.state, counts=self.counts,
                total=self.total, catalog=len(self.rows), cookies_provided=self.cookie_path.is_file(),
                elapsed=round(time.monotonic()-self.started, 1), cached_media="memory only"), ensure_ascii=False, indent=2)

    def snapshot(self):
        with self.lock:
            return json.dumps(dict(state=self.state, message=self.message, busy=self.busy,
                                   queue_id=self.queue_id, scan_count=self.scan_count,
                                   selection=self.selection, settings=self.settings, elapsed=round(time.monotonic()-self.started),
                                   recoveries=[dict(id=Path(d["_path"]).name, title=d.get("prepared", {}).get("title", "Análise interrompida")) for d in self.recoveries],
                                   title=self.prepared.title if self.prepared else "",
                                   count=len(self.prepared.plan) if self.prepared else 0,
                                   total=self.total, **self.counts,
                                   cookies=self.cookie_path.is_file(), logs=list(self.logs),
                                   errors=list(self.errors), output=str(self.download_dir),
                                   destination=self.destination.get("uri", ""),
                                   destination_label=self.destination.get("label", "Pasta do aplicativo")), ensure_ascii=False)

    def set_destination(self, uri):
        with self.lock:
            if self.busy:
                raise ValueError("Aguarde a operação terminar antes de trocar a pasta.")
            if uri:
                if self.storage_backend is None or not str(uri).startswith("content://"):
                    raise ValueError("Escolha uma pasta pelo seletor do Android.")
                label = str(self.storage_backend.describe(uri))
                destination = {"uri": str(uri), "label": label}
            else:
                destination = {}
            atomic_json(self.destination_path, destination)
            self.destination = destination

    def _begin(self, state, message, action):
        with self.lock:
            if self.busy:
                raise ValueError("Aguarde a operação atual terminar.")
            self.busy, self.state, self.message = True, state, message
            self.cancel_event.clear()
            self.pause_event.set()
            self.counts = dict(completed=0, skipped=0, failed=0)
            self.total = 0
            self.errors.clear()
            self.started = time.monotonic()

        def run():
            try:
                action()
            except Exception as error:
                with self.lock:
                    self.state = "error"
                    self.message = redacted(str(error))
                    self.errors.append(self.message)
            finally:
                with self.lock:
                    if self.cancel_event.is_set():
                        self.state, self.message = "cancelled", "Fila interrompida. Os arquivos concluídos foram mantidos."
                    self.busy = False
                    if state == "downloading" or self.total:
                        self.history.insert(0, dict(title=self.prepared.title if self.prepared else "", state=self.state,
                            time=time.time(), elapsed=round(time.monotonic()-self.started), **self.counts))
                        self.history = self.history[:200]
                        atomic_json(self.history_path, self.history)

        self.worker = threading.Thread(target=run, name="buidl-mobile", daemon=True)
        self.worker.start()

    def scan(self, settings_json, force=False, download_all=False):
        settings = json.loads(settings_json)
        url = str(settings.get("url", "")).strip()
        if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
            raise ValueError("Informe uma URL http ou https válida.")
        settings["url"] = url
        self._options(settings)  # Validate before creating a worker.
        cookie_stat = self.cookie_path.stat() if self.cookie_path.is_file() else None
        fingerprint = (url, bool(settings.get("all_pages", True)), bool(settings.get("separate_posts", False)),
                       tuple(settings.get("include", [])), tuple(settings.get("ignore", [])),
                       settings.get("max_pages", 3), bool(settings.get("flat")),
                       (cookie_stat.st_mtime_ns, cookie_stat.st_size) if cookie_stat else None)

        def action():
            with self._client() as client:
                downloader = ThreadDownloader(client, self.manager, self._options(settings))
                if force or self.prepared is None or fingerprint != self.fingerprint:
                    self.settings = settings
                    self.prepared = None
                    self.recovery = RecoveryStore(self.private_dir / "recovery")
                    self._save_session(not download_all)
                    prepared = downloader.prepare_thread(url)
                    if self.cancel_event.is_set():
                        return
                    if prepared is None or not prepared.plan:
                        raise ValueError("Nenhum arquivo encontrado. Verifique a URL, os cookies e os avisos.")
                    with self.lock:
                        self.prepared, self.fingerprint = prepared, fingerprint
                        self.queue_id += 1
                        self.scan_count += 1
                        self.selection = []
                        self._reset_rows()
                        self._save_session(not download_all)
                self.settings = settings
                if download_all:
                    self._download(downloader, None)
                else:
                    with self.lock:
                        self.state, self.message = "ready", f"{len(self.prepared.plan)} arquivos encontrados. Escolha na galeria."

        self._begin("scanning", "Analisando páginas e resolvendo links…", action)

    def items(self):
        with self.lock:
            if self.prepared is None:
                return "[]"
            return json.dumps([dict(index=i, name=m.filename, kind=kind(m), post=m.post_label,
                                    host=urlparse(m.origin_url or m.state_key).hostname or "",
                                    origin=m.origin_url or m.state_key,
                                    thumbnail=bool(m.thumbnail_url or kind(m) == "Imagens"))
                               for i, (m, _) in enumerate(self.prepared.plan)], ensure_ascii=False)

    def _item(self, queue_id, index):
        with self.lock:
            if queue_id != self.queue_id or self.prepared is None:
                raise ValueError("A galeria mudou. Abra a prévia novamente.")
            if not 0 <= index < len(self.prepared.plan):
                raise ValueError("Arquivo inválido")
            return self.prepared.plan[index][0]

    def download(self, queue_id, indices_json, retry=False):
        indices = json.loads(indices_json)
        with self.lock:
            if self.prepared is None or queue_id != self.queue_id:
                raise ValueError("Analise uma thread primeiro.")
            selected = None if indices is None else {self._item(queue_id, int(i)).state_key for i in indices}
            if selected == set():
                raise ValueError("Selecione pelo menos um arquivo.")

        def action():
            with self._client() as client:
                downloader = ThreadDownloader(client, self.manager, self._options(self.settings))
                self._download(downloader, selected, retry=retry)

        self._begin("downloading", "Baixando arquivos…", action)

    def _download(self, downloader, selected, retry=False):
        with self.lock:
            self.state, self.message = "downloading", "Baixando arquivos…"
            self.active_ids = [i for i, (m, _) in enumerate(self.prepared.plan) if selected is None or m.state_key in selected]
            if not retry: self.selection = list(self.active_ids)
            self._save_session(False)
            if self.recovery.document and not self.settings.get("dry_run"):
                self.recovery.select({self.prepared.plan[i][0].state_key for i in self.selection})
        if self.destination.get("uri") and not downloader.options.dry_run:
            if self.storage_backend is None:
                raise ValueError("A pasta de destino está indisponível. Escolha a pasta novamente.")
            storage = Destination(self.private_dir, self.destination["uri"], self.storage_backend)
            downloader = DestinationDownloader(downloader.client, self.manager, downloader.options, storage)
        failed = downloader.download_prepared(self.prepared, selected)
        with self.lock:
            self.state = "failed" if failed else "done"
            self.message = ("Finalizado com falhas — veja os detalhes" if failed else "Download concluído")
            unresolved = any(self.rows[i]["status"] not in {"completed", "skipped"} for i in self.selection)
            if not failed and not unresolved and not self.cancel_event.is_set() and not self.settings.get("dry_run"):
                self.recovery.complete()
                self.recoveries = self.recovery.pending()
            if not failed and self.errors:
                self.message = "Download concluído com avisos — veja os detalhes"

    def pause(self):
        with self.lock:
            if self.state == "paused":
                self.pause_event.set()
                self.state, self.message = "downloading", "Baixando arquivos…"
            elif self.state == "downloading":
                self.pause_event.clear()
                self.state, self.message = "paused", "Fila pausada. Arquivos em andamento podem continuar."

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.pause_event.set()
            with self.lock:
                self.message = "Parando a fila. Aguardando operações em andamento…"

    def import_cookies(self, pending_file):
        if self.busy:
            raise ValueError("Aguarde a operação terminar antes de trocar os cookies.")
        pending = Path(pending_file).resolve()
        if pending.parent != self.private_dir.resolve() or pending.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Arquivo de cookies inválido.")
        with HttpClient(cookies_file=str(pending)):
            pass
        pending.replace(self.cookie_path)
        self.fingerprint = None

    def capture_cookies(self, host, cookie_header, user_agent):
        """Capture effective HTTPS root cookies, never passwords or page scripts.

        WebView exposes name/value pairs without expiry/domain/path attributes.
        Scope the snapshot to the HTTPS origin and root used for getCookie;
        the server remains authoritative about session expiry.
        """
        with self.lock:
            if self.busy:
                raise ValueError("Aguarde a operação terminar antes de trocar os cookies.")
            if host not in CAPTURE_HOSTS or not valid_user_agent(user_agent):
                raise ValueError("Origem ou navegador inválido para capturar a sessão.")
            if not isinstance(cookie_header, str) or not cookie_header or len(cookie_header) > 65536:
                raise ValueError("Nenhum cookie disponível. Conclua o login e tente novamente.")
            if any(ord(c) < 32 or ord(c) > 126 for c in cookie_header):
                raise ValueError("O navegador retornou cookies inválidos. Tente novamente.")
            pairs = []
            names = set()
            for part in cookie_header.split(";"):
                name, equals, value = part.strip().partition("=")
                if not equals or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    raise ValueError("O navegador retornou cookies inválidos. Tente novamente.")
                # Keep the browser's first effective value if a domain cookie
                # and host cookie have the same name.
                if name not in names:
                    pairs.append((name, value))
                    names.add(name)
            if len(pairs) > 300:
                raise ValueError("A sessão contém cookies demais. Tente novamente.")
            metadata = json.dumps({"host": host, "user_agent": user_agent}, ensure_ascii=True)
            lines = ["# Netscape HTTP Cookie File", CAPTURE_PREFIX + metadata]
            lines.extend(f"#HttpOnly_{host}\tFALSE\t/\tTRUE\t0\t{name}\t{value}" for name, value in pairs)
            pending = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=self.private_dir, prefix="capture-", suffix=".pending",
                                                 delete=False) as output:
                    pending = Path(output.name)
                    output.write("\n".join(lines) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                with HttpClient(cookies_file=str(pending)):
                    pass
                pending.replace(self.cookie_path)
            except (OSError, ValueError):
                # Parser exceptions can quote cookie lines: never expose them.
                raise ValueError("Não foi possível salvar a sessão. Os cookies anteriores foram mantidos.") from None
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
            self.fingerprint = None
            return len(pairs)

    def remove_cookies(self):
        if self.busy:
            raise ValueError("Aguarde a operação terminar.")
        self.cookie_path.unlink(missing_ok=True)
        self.fingerprint = None

    def thumbnail(self, queue_id, index, full=False):
        media = self._item(queue_id, index)
        source = (media.url if full and kind(media) == "Imagens" and not media.resolver else None) or media.thumbnail_url or (media.url if kind(media) == "Imagens" and not media.resolver else None)
        limit = (16 if full else 4) * 1024 * 1024
        if not source or urlparse(source).scheme not in {"http", "https"}:
            return None
        try:
            with self._client() as client:
                headers = dict(media.headers or {}) if source == media.url else {}
                referer = media.origin_url or media.source_url
                if referer and urlparse(referer).scheme in {"http", "https"}: headers.setdefault("Referer", referer)
                headers["Accept"] = "image/*"
                with client.get(source, headers=headers, stream=True, timeout=(4, 4)) as response:
                    if response.status_code != 200 or not response.headers.get("Content-Type", "").startswith("image/"):
                        return None
                    if int(response.headers.get("Content-Length", "0")) > limit:
                        return None
                    data = bytearray()
                    start = time.monotonic()
                    for chunk in response.iter_content(32768):
                        data.extend(chunk)
                        if len(data) > limit or time.monotonic() - start > 10:
                            return None
                    return bytes(data)
        except Exception:
            return None

    def video_source(self, queue_id, index):
        media = self._item(queue_id, index)
        with self._client() as client:
            playable = refresh_media(replace(media), client)
            if playable is None or urlparse(playable.url).scheme not in {"http", "https"}:
                raise ValueError("Prévia indisponível neste host; o download continua disponível.")
            headers = dict(playable.headers or {})
            referer = media.origin_url or media.source_url
            if referer and urlparse(referer).scheme in {"http", "https"}: headers.setdefault("Referer", referer)
            request = requests.Request("GET", playable.url, headers=headers)
            prepared = client.session.prepare_request(request)
            return json.dumps(dict(url=playable.url, headers=dict(prepared.headers)))

    def files(self):
        if self.destination.get("uri"):
            if self.storage_backend is None:
                raise ValueError("Escolha a pasta de destino novamente.")
            storage = Destination(self.private_dir, self.destination["uri"], self.storage_backend)
            return json.dumps(sorted(storage.files(), key=lambda item: item["relative"]), ensure_ascii=False)
        result = []
        for path in self.download_dir.rglob("*"):
            if path.is_file() and not path.name.startswith(".") and not path.name.endswith((".part", ".tmp", ".part.json")):
                resolved = path.resolve()
                if not resolved.is_relative_to(self.download_dir.resolve()):
                    continue
                result.append(dict(path=str(resolved), name=path.name, relative=str(path.relative_to(self.download_dir)),
                                   size=path.stat().st_size, mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
        return json.dumps(sorted(result, key=lambda item: item["relative"]), ensure_ascii=False)
