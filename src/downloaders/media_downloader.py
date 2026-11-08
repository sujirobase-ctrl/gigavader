"""Stream a resolved media file to disk, driving the Rich progress bar."""

from __future__ import annotations

import hashlib
import threading
import time
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from src.config import (
    AUDIO_EXTENSIONS,
    CHUNK_SIZE,
    RATE_429_BACKOFF,
    RETRY_BACKOFF,
    VIDEO_EXTENSIONS,
    FailReason,
    SkipReason,
    TaskResult,
)
from src.file_utils import (
    extension_from_content_type,
    sanitize_name,
)
from src.resolvers import goonbox_image_headers

from .chunked import (
    DownloadConfig,
    detect_range_support,
    is_error_document,
    save_file_with_chunks,
    should_use_parallel_download,
    strong_etag,
    valid_range,
)
from .rate_limiter import RateLimiter

TRANSIENT_HTTP_STATUSES = frozenset({500, 502, 503, 504, 520, 521, 522, 523, 524})


@dataclass
class DownloadOutcome:
    """Result of a single file download."""

    result: TaskResult
    reason: SkipReason | FailReason | None = None
    path: Path | None = None
    message: str = ""
    http_status: int | None = None


class MediaDownloader:
    """Download one file with retries, resume-awareness and progress reporting."""

    def __init__(
        self,
        client,  # noqa: ANN001 - HttpClient
        live_manager,  # noqa: ANN001 - LiveManager
        max_retries: int = 5,
        *,
        dry_run: bool = False,
        num_connections: int = 1,
        rate_limiter: RateLimiter | None = None,
        cancel_event=None,
        reuse_bunkr: bool = False,
    ) -> None:
        self.client = client
        self.live_manager = live_manager
        self.max_retries = max_retries
        self.dry_run = dry_run
        self.num_connections = max(1, num_connections)
        self.rate_limiter = rate_limiter
        self.cancel_event = cancel_event
        self.reuse_bunkr = reuse_bunkr
        self._claim_lock = threading.Lock()
        self._claimed_by_url: dict[str, Path] = {}
        self._claimed_paths: set[Path] = set()
        self._bunkr_locks: dict[tuple[str, Path], threading.Lock] = {}
        self._bunkr_files: dict[tuple[str, Path], tuple[Path, int]] = {}

    def remember_bunkr(self, file_id: str, path: Path, size: int) -> None:
        """Seed reusable files from verified checkpoints before workers start."""
        self._bunkr_files[file_id, path.parent.resolve()] = (path, size)

    def download(
        self,
        media,  # noqa: ANN001 - ResolvedMedia
        destination_folder: Path,
        task_index: int = 0,
    ) -> DownloadOutcome:
        """Fetch one media item into destination_folder."""
        folder = destination_folder
        if media.subfolder:
            folder = folder / sanitize_name(media.subfolder)

        # Sort each file into Audio/ Videos/ or Photos/ by media type. The
        # directory is only physically created when a file is written below, so
        # a category folder only appears when the thread actually contains that
        # type (per user request: "if there are no videos, just leave the Photos
        # folder" and "when any audio is found, create an Audio folder").
        _ext = Path(media.filename or media.url).suffix.lower()
        if _ext in AUDIO_EXTENSIONS:
            category = "Audio"
        elif _ext in VIDEO_EXTENSIONS:
            category = "Videos"
        else:
            category = "Photos"
        folder = folder / category

        if self.dry_run:
            self.live_manager.update_log(
                event="Dry run",
                details=f"Would download {media.url}",
            )
            return DownloadOutcome(TaskResult.SKIPPED, SkipReason.DRY_RUN)

        folder.mkdir(parents=True, exist_ok=True)
        if self.reuse_bunkr and media.bunkr_file_id:
            return self._download_bunkr(media, folder, task_index)
        return self._download_file(media, folder, task_index)

    def _download_bunkr(self, media, folder: Path, task_index: int) -> DownloadOutcome:
        key = (media.bunkr_file_id, folder.resolve())
        with self._claim_lock:
            lock = self._bunkr_locks.setdefault(key, threading.Lock())
        with lock:
            cached = self._bunkr_files.get(key)
            if cached is not None:
                path, size = cached
                try:
                    valid = path.is_file() and path.stat().st_size == size > 0
                except OSError:
                    valid = False
                if valid:
                    self.live_manager.update_log(event="Espelho reaproveitado", details=path.name)
                    return DownloadOutcome(TaskResult.SKIPPED, SkipReason.ALREADY_DOWNLOADED, path=path,
                                           message="Arquivo do Bunkr já gravado por outro link")
            outcome = self._download_file(media, folder, task_index)
            if outcome.result is TaskResult.COMPLETED and outcome.path is not None:
                self._bunkr_files[key] = (outcome.path, outcome.path.stat().st_size)
            return outcome

    def _download_file(self, media, folder: Path, task_index: int) -> DownloadOutcome:
        headers = dict(media.headers or {})
        host = (urlparse(media.url).hostname or "").lower()
        origin = urlparse(media.origin_url or "")
        from_goonbox = origin.scheme == "https" and origin.hostname == "goonbox.cr"
        if host == "cuckcapital.cr" or host.endswith(".cuckcapital.cr") or (
            host == "goonbox.cr" or (from_goonbox and (host == "cdn.cr" or host.endswith(".cdn.cr")))
        ):
            defaults = goonbox_image_headers(origin.geturl()) if from_goonbox else goonbox_image_headers()
            present = {name.lower() for name in headers}
            headers.update({name: value for name, value in defaults.items() if name.lower() not in present})

        headers['Accept-Encoding'] = 'identity'

        last_error = ""
        last_status: int | None = None
        for attempt in range(self.max_retries + 1):
            if self.cancel_event is not None and self.cancel_event.is_set():
                return DownloadOutcome(TaskResult.FAILED, FailReason.NETWORK_ERROR, message="Download cancelado; partes preservadas")
            try:
                outcome = self._attempt(media, folder, headers, task_index)
                if outcome.reason is not FailReason.NETWORK_ERROR:
                    return outcome
                last_status = None
                last_error = outcome.message
                if attempt < self.max_retries:
                    self._wait_retry(RETRY_BACKOFF ** min(attempt, 4))
            except requests.HTTPError as error:
                # 429 means the CDN is rate-limiting us by IP — back off harder
                # than a generic network error so we don't extend the ban.
                status = getattr(getattr(error, "response", None), "status_code", None)
                last_status = status
                last_error = str(error)
                if status in (401, 403, 404, 410):
                    return DownloadOutcome(
                        TaskResult.FAILED,
                        FailReason.HTTP_ERROR,
                        message=f"HTTP {status}: desafio anti-bot do site"
                        if getattr(error.response, "headers", {}).get("cf-mitigated", "").lower() == "challenge"
                        else f"HTTP {status} ao solicitar o arquivo",
                        http_status=status,
                    )
                if media.resolver == "bunkr" and status in TRANSIENT_HTTP_STATUSES:
                    retry_after = getattr(error.response, "headers", {}).get("Retry-After", "")
                    try:
                        delay = min(max(float(retry_after), 0), 60)
                    except ValueError:
                        delay = 0
                    if delay > 0:
                        self._wait_retry(delay)
                    return DownloadOutcome(TaskResult.FAILED, FailReason.HTTP_ERROR,
                                           message=f"HTTP {status}", http_status=status)
                if attempt < self.max_retries:
                    if status == 429:
                        retry_after = getattr(error.response, "headers", {}).get("Retry-After", "")
                        try:
                            backoff = min(max(float(retry_after), 0), 60)
                        except ValueError:
                            backoff = min(RATE_429_BACKOFF ** (attempt + 1), 60)
                        if self.cancel_event is not None:
                            self.cancel_event.wait(backoff)
                        else:
                            time.sleep(backoff)
                    else:
                        self._wait_retry(RETRY_BACKOFF ** min(attempt, 4))
            except requests.RequestException as error:
                # Connection drop / timeout: short backoff.
                last_status = None
                last_error = str(error)
                if attempt < self.max_retries:
                    self._wait_retry(RETRY_BACKOFF ** min(attempt, 4))
            except OSError as error:
                return DownloadOutcome(
                    TaskResult.FAILED,
                    FailReason.WRITE_ERROR,
                    message=str(error),
                )

        return DownloadOutcome(
            TaskResult.FAILED,
            FailReason.HTTP_ERROR if last_status else FailReason.NETWORK_ERROR,
            message=f"HTTP {last_status}" if last_status else last_error,
            http_status=last_status,
        )

    def _wait_retry(self, delay: float) -> None:
        if self.cancel_event is None:
            time.sleep(delay)
        else:
            self.cancel_event.wait(delay)

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    @staticmethod
    def _work_path(target: Path, key: str, etag: str | None, size: int) -> Path:
        identity = hashlib.sha256(repr((target.name, key, etag, size)).encode()).hexdigest()[:32]
        return target.with_name(f".buidl-{identity}")

    # Private helpers
    def _attempt(
        self,
        media,  # noqa: ANN001
        folder: Path,
        headers: dict[str, str],
        task_index: int,
    ) -> DownloadOutcome:
        timeout = (self.client.timeout, max(self.client.timeout, 60))
        count_bytes = getattr(self.live_manager, "item_bytes", None)

        # When parallel chunks are requested, probe range support up front via
        # HEAD. If the host allows byte ranges and the file is large enough we
        # go straight to the chunked path (it fetches content-type-free ranges
        # and is resilient to mid-file drops). Otherwise we fall through to the
        # single-stream path below, which opens a full GET.
        if self.num_connections > 1:
            supports_range, content_length, etag = detect_range_support(media.url, headers, self.client.head)
            if etag and should_use_parallel_download(
                content_length, self.num_connections, supports_range=supports_range,
            ):
                filename = sanitize_name(media.filename or "file")
                target = self._claim_target(media.state_key, folder / filename)
                work_path = self._work_path(target, media.state_key, etag, content_length)
                task_id = self.live_manager.add_task(task_index, total=100)

                def _cb(pct: float) -> None:
                    self.live_manager.update_task(task_id, completed=pct)

                failed = save_file_with_chunks(
                    media.url,
                    str(target),
                    _cb,
                    DownloadConfig(
                        content_length=content_length,
                        num_connections=self.num_connections,
                        headers=headers,
                        rate_limiter=self.rate_limiter,
                        cancel_event=self.cancel_event,
                        etag=etag,
                        get=self.client.get,
                        on_bytes=(lambda amount: count_bytes(task_index, amount)) if count_bytes else None,
                    ),
                    work_path=work_path,
                )
                if not failed:
                    return DownloadOutcome(TaskResult.COMPLETED, path=target)
                # Persistent chunk failure -> fall through to single-stream so
                # the outer retry budget still applies.

        if self._cancelled():
            return DownloadOutcome(TaskResult.FAILED, FailReason.NETWORK_ERROR, message="Download cancelado; partes preservadas")

        with self.client.get(
            media.url,
            headers=headers,
            stream=True,
            timeout=timeout,
        ) as response:
            if response.status_code == 404:
                return DownloadOutcome(
                    TaskResult.FAILED,
                    FailReason.HTTP_ERROR,
                    message="HTTP 404 ao solicitar o arquivo",
                    http_status=404,
                )
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()
            # Hosts sometimes answer a 200 with an HTML error/login page
            # instead of the file. Saving that as a ".bin" pollutes the
            # output, so reject non-media responses up front.
            if is_error_document(response):
                reason = (
                    FailReason.NETWORK_ERROR
                    if (urlparse(media.url).hostname or "").casefold() == "i.redd.it"
                    else FailReason.HTTP_ERROR
                )
                return DownloadOutcome(
                    TaskResult.FAILED,
                    reason,
                    message=f"Got HTML or JSON ({content_type}) instead of a file",
                )

            if response.status_code != 200:
                return DownloadOutcome(TaskResult.FAILED, FailReason.HTTP_ERROR,
                                       message=f"HTTP {response.status_code}: esperava o arquivo inteiro")

            filename = self._final_filename(media, response)
            target = self._claim_target(media.state_key, folder / filename)
            length = int(response.headers.get("Content-Length") or 0)
            encoding = response.headers.get("Content-Encoding", "").lower()
            etag = strong_etag(response) if encoding in ("", "identity") else None
            work_path = self._work_path(target, media.state_key, etag, length)
            temp_path = work_path.with_suffix(".part")
            resume_pos = temp_path.stat().st_size if etag and temp_path.is_file() else 0
            if 0 < resume_pos < length:
                with self.client.get(
                    media.url,
                    headers={**headers, "Range": f"bytes={resume_pos}-", "If-Range": etag},
                    stream=True,
                    timeout=timeout,
                ) as resume_resp:
                    if valid_range(resume_resp, resume_pos, length - 1, length, etag):
                        return self._stream_to_file(
                            resume_resp, temp_path, target, resume_pos, length, task_index,
                        )
                    if resume_resp.status_code == 200 and strong_etag(resume_resp) != etag:
                        # Retry the discovery request instead of appending to the old version.
                        return DownloadOutcome(TaskResult.FAILED, FailReason.NETWORK_ERROR,
                                               message="O arquivo mudou durante a retomada")

            return self._stream_to_file(response, temp_path, target, 0, length, task_index)

    def _stream_to_file(
        self,
        response,  # noqa: ANN001 - requests.Response
        temp_path: Path,
        target_path: Path,
        resume_pos: int,
        total: int,
        task_index: int,
    ) -> DownloadOutcome:
        mode = "ab" if resume_pos else "wb"
        downloaded = resume_pos
        task_id = self.live_manager.add_task(task_index, total=total or 100)
        count_bytes = getattr(self.live_manager, "item_bytes", None)
        try:
            with temp_path.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk and count_bytes:
                        count_bytes(task_index, len(chunk))
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        return DownloadOutcome(TaskResult.FAILED, FailReason.NETWORK_ERROR, message="Download cancelado; partes preservadas")
                    if not chunk:
                        continue
                    if self.rate_limiter:
                        self.rate_limiter.consume(len(chunk), self.cancel_event)
                    if self._cancelled():
                        return DownloadOutcome(TaskResult.FAILED, FailReason.NETWORK_ERROR,
                                               message="Download cancelado; partes preservadas")
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        self.live_manager.update_task(task_id, completed=downloaded)
                    else:
                        self.live_manager.update_task(task_id, advance=1)
                handle.flush()
                os.fsync(handle.fileno())
        except (requests.RequestException, OSError) as error:
            return DownloadOutcome(
                TaskResult.FAILED,
                FailReason.NETWORK_ERROR if isinstance(error, requests.RequestException)
                else FailReason.WRITE_ERROR,
                message=str(error),
            )

        content_encoding = response.headers.get("Content-Encoding", "").lower()
        if total and content_encoding in ("", "identity") and downloaded != total:
            return DownloadOutcome(
                TaskResult.FAILED,
                FailReason.NETWORK_ERROR,
                message=(
                    f"Incomplete response: expected {total} bytes, "
                    f"received {downloaded}"
                ),
            )
        if not downloaded:
            return DownloadOutcome(TaskResult.FAILED, FailReason.NETWORK_ERROR,
                                   message="Incomplete response: received no file data")

        if self._cancelled():
            return DownloadOutcome(TaskResult.FAILED, FailReason.NETWORK_ERROR,
                                   message="Download cancelado; partes preservadas")
        temp_path.replace(target_path)
        return DownloadOutcome(TaskResult.COMPLETED, path=target_path)

    def _claim_target(self, media_url: str, target: Path) -> Path:
        """Reserve a collision-free output path across concurrent workers."""
        with self._claim_lock:
            claimed = self._claimed_by_url.get(media_url)
            if claimed is not None:
                return claimed

            candidate = target
            counter = 1
            while candidate in self._claimed_paths or (candidate.exists() and
                    (not candidate.is_file() or candidate.stat().st_size > 0)):
                candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
                counter += 1

            self._claimed_by_url[media_url] = candidate
            self._claimed_paths.add(candidate)
            return candidate

    @staticmethod
    def _final_filename(media, response: requests.Response) -> str:  # noqa: ANN001
        filename = sanitize_name(media.filename or "")

        disposition = response.headers.get("Content-Disposition", "")
        if not filename and "filename=" in disposition:
            raw = disposition.split("filename=")[-1].strip('";\' ')
            filename = sanitize_name(raw)

        if not filename:
            filename = "file"

        if "." not in filename:
            extension = extension_from_content_type(response.headers.get("Content-Type"))
            filename = f"{filename}.{extension}"

        return filename
