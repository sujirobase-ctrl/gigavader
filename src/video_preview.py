"""Short, silent, memory-only video previews. Never touches download state."""

from __future__ import annotations

import io
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from threading import Event
from urllib.parse import urlparse

from PIL import Image

from src.http_client import HttpClient
from src.resolvers import ResolvedMedia, refresh_media

MAX_VIDEO_BYTES = 24 * 1024 * 1024
VIDEO_TIMEOUT = 20
VIDEO_SIZE = (480, 480)
VIDEO_FPS = 8
VIDEO_SECONDS = 10
BLOCK_SIZE = 256 * 1024


class PreviewUnavailable(OSError):
    """Preview failure, deliberately separate from a download failure."""


@dataclass
class VideoPreview:
    frames: list[Image.Image] = field(default_factory=list)
    fps: int = VIDEO_FPS
    message: str = "Prévia indisponível neste host. O arquivo ainda pode ser baixado."


class PreviewBudget:
    def __init__(self, stop: Event, *, max_bytes=None, timeout=None):
        self.max_bytes = MAX_VIDEO_BYTES if max_bytes is None else max_bytes
        self.stop = stop
        self.deadline = time.monotonic() + (VIDEO_TIMEOUT if timeout is None else timeout)
        self.bytes_read = 0
        self.requests = 0

    def check(self):
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise PreviewUnavailable("Prévia cancelada ou tempo limite atingido")

    def account(self, size):
        self.check()
        self.bytes_read += size
        if self.bytes_read > self.max_bytes:
            raise PreviewUnavailable("Limite de dados da prévia atingido")


class PreviewClient(HttpClient):
    """Apply the same cancellation/budget to resolver and direct media requests."""

    def __init__(self, cookies_file, budget):
        super().__init__(cookies_file=cookies_file, timeout=4, max_retries=1)
        self.budget = budget
        self.session.max_redirects = 4
        original_request = self.session.request

        def request(method, url, **kwargs):
            budget.check()
            if urlparse(url).scheme not in {"http", "https"}:
                raise PreviewUnavailable("Protocolo não suportado")
            budget.requests += 1
            if budget.requests > 128:
                raise PreviewUnavailable("Muitas requisições para uma prévia")
            streamed = kwargs.pop("stream", False)
            remaining = max(0.1, min(4, budget.deadline - time.monotonic()))
            kwargs["timeout"] = (remaining, remaining)
            response = original_request(method, url, stream=True, **kwargs)
            if streamed:
                return response
            # Resolver HTML/JSON is bounded too, even if Content-Length lies.
            data = bytearray()
            try:
                for chunk in response.iter_content(32 * 1024):
                    budget.account(len(chunk))
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise PreviewUnavailable("Página de resolução muito grande")
                response._content = bytes(data)
                response._content_consumed = True
                return response
            finally:
                response.close()

        self.session.request = request


class RangeReader(io.RawIOBase):
    """Seekable HTTP input; MP4 metadata at EOF does not require a full download."""

    def __init__(self, media, client, budget):
        super().__init__()
        self.media, self.client, self.budget = media, client, budget
        self.position = 0
        self.size = None
        self.blocks = OrderedDict()
        self._block(0)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        self.budget.check()
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END):
            raise ValueError("Invalid seek")
        target = offset + (self.position if whence == io.SEEK_CUR else self.size if whence == io.SEEK_END else 0)
        if target < 0:
            raise ValueError("Negative seek")
        self.position = target
        return target

    def _block(self, start):
        self.budget.check()
        if start in self.blocks:
            self.blocks.move_to_end(start)
            return self.blocks[start]
        end = start + BLOCK_SIZE - 1
        headers = dict(self.media.headers or {})
        headers.update({"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"})
        with self.client.get(self.media.url, headers=headers, stream=True) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if any(kind in content_type for kind in ("html", "json", "mpegurl", "dash+xml")):
                raise PreviewUnavailable("O host não retornou um vídeo direto")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise PreviewUnavailable("Resposta comprimida não suporta busca por trechos")
            if response.status_code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match:
                    raise PreviewUnavailable("Resposta parcial inválida")
                first, last, size = map(int, match.groups())
                if first != start or last != min(end, size - 1) or size <= last:
                    raise PreviewUnavailable("Trecho de vídeo incorreto")
                expected = last - first + 1
            elif response.status_code == 200 and start == 0:
                # Tiny videos may be returned whole; never stream a large file
                # from a host that ignores Range or hides its size.
                size = int(response.headers.get("Content-Length", "0"))
                if not 0 < size <= BLOCK_SIZE:
                    raise PreviewUnavailable("Este host não permite prévia por trechos")
                expected = size
            else:
                raise PreviewUnavailable("Este host não permite prévia por trechos")
            if self.size is not None and size != self.size:
                raise PreviewUnavailable("O vídeo mudou durante a prévia")
            self.size = size
            data = bytearray()
            for chunk in response.iter_content(32 * 1024):
                self.budget.account(len(chunk))
                data.extend(chunk)
                if len(data) > expected:
                    raise PreviewUnavailable("Resposta maior que o trecho solicitado")
            if len(data) != expected:
                raise PreviewUnavailable("Trecho incompleto")
        self.blocks[start] = bytes(data)
        while len(self.blocks) > 8:
            self.blocks.popitem(last=False)
        return self.blocks[start]

    def read(self, size=-1):
        self.budget.check()
        remaining = max(0, self.size - self.position)
        size = remaining if size < 0 else min(size, remaining)
        if size > MAX_VIDEO_BYTES:
            raise PreviewUnavailable("Leitura muito grande para a prévia")
        data = bytearray()
        while len(data) < size:
            start = self.position // BLOCK_SIZE * BLOCK_SIZE
            block = self._block(start)
            offset = self.position - start
            chunk = block[offset:offset + size - len(data)]
            if not chunk:
                raise PreviewUnavailable("Trecho vazio")
            data.extend(chunk)
            self.position += len(chunk)
        return bytes(data)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def fetch_video_preview(media: ResolvedMedia, cookies_file: str | None, stop: Event, *, cover_only=False) -> VideoPreview:
    result = VideoPreview()
    budget = PreviewBudget(stop, max_bytes=4 * 1024 * 1024, timeout=8) if cover_only else PreviewBudget(stop)
    target_size = (240, 150) if cover_only else VIDEO_SIZE
    max_frames = 1 if cover_only else VIDEO_SECONDS * VIDEO_FPS
    try:
        budget.check()
        import av

        with PreviewClient(cookies_file, budget) as client:
            # Refresh only a copy. Gallery IDs, selection and the download queue
            # must never be replaced by a temporary signed URL.
            playable = refresh_media(replace(media, headers=dict(media.headers or {})), client)
            if playable is None:
                return result
            referer = media.origin_url or media.source_url
            if referer and urlparse(referer).scheme in {"http", "https"}:
                playable.headers = dict(playable.headers or {})
                playable.headers.setdefault("Referer", referer)
            with RangeReader(playable, client, budget) as reader:
                with av.open(reader, options={
                    "format_whitelist": "mov,matroska,webm,avi,flv,mpeg,mpegts,ogg",
                    "protocol_whitelist": "", "enable_drefs": "0", "use_absolute_path": "0",
                    "probesize": "1048576", "analyzeduration": "2000000",
                }) as container:
                    if not container.streams.video:
                        return result
                    stream = container.streams.video[0]
                    if not 0 < stream.width * stream.height <= 8_294_400:
                        return result
                    stream.codec_context.thread_count = 1
                    first_time = None
                    rate = float(stream.average_rate or 25)
                    for number, frame in enumerate(container.decode(stream)):
                        budget.check()
                        if number >= 2400:
                            break
                        timestamp = float(frame.time) if frame.time is not None else number / max(rate, 1)
                        if first_time is None:
                            first_time = timestamp
                        elapsed = timestamp - first_time
                        if elapsed >= VIDEO_SECONDS:
                            break
                        if elapsed + 0.001 < len(result.frames) / VIDEO_FPS:
                            continue
                        scale = min(target_size[0] / frame.width, target_size[1] / frame.height, 1)
                        image = frame.to_image(width=max(1, int(frame.width * scale)),
                                               height=max(1, int(frame.height * scale)))
                        result.frames.append(image)
                        if len(result.frames) >= max_frames:
                            break
    except Exception:
        # Authentication, unsupported formats and budgets are preview-only
        # failures. Never report them to download counters or leak signed URLs.
        pass
    if stop.is_set():
        return VideoPreview()
    if len(result.frames) > 1:
        result.message = "Trecho de até 10 segundos · sem áudio"
    elif result.frames:
        result.message = "Somente capa disponível. O arquivo ainda pode ser baixado."
    return result
