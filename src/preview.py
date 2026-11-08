"""Selection model and bounded, memory-only image previews for the desktop UI."""

from __future__ import annotations

import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from io import BytesIO
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from PIL import Image, ImageOps

from src.config import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from src.downloaders import PreparedThread, ThreadOptions
from src.http_client import HttpClient
from src.resolvers import ResolvedMedia

MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_PIXELS = 40_000_000
PREVIEW_SIZE = (240, 150)


class PreviewCommand(Enum):
    REFRESH = auto()


def preview_fingerprint(url: str, cookies_file: str | None, options: ThreadOptions) -> tuple:
    """Only settings affecting the scanned list or destinations invalidate it."""
    cookie_identity = None
    if cookies_file:
        path = Path(cookies_file).resolve()
        stat = path.stat()
        cookie_identity = (str(path), stat.st_mtime_ns, stat.st_size)
    return (
        url.strip(), cookie_identity,
        str(Path(options.download_path or options.custom_path or ".").resolve()),
        options.all_pages, None if options.all_pages else options.max_pages,
        tuple(options.post_ids), tuple(options.include), tuple(options.ignore),
        options.flat, options.separate_posts,
    )


@dataclass
class PreviewSession:
    """Single in-memory gallery, retained until settings change or app closes."""

    prepared: PreparedThread
    fingerprint: tuple
    selected: set[str] = field(default_factory=set)
    thumbnails: OrderedDict = field(default_factory=OrderedDict)
    videos: OrderedDict = field(default_factory=OrderedDict)
    page: int = 0
    query: str = ""
    kind: str = "Todos"
    scroll: float = 0.0
    warnings: list = field(default_factory=list)


def media_kind(media: ResolvedMedia) -> str:
    suffix = Path(media.filename or urlparse(media.url).path).suffix.casefold()
    if suffix in IMAGE_EXTENSIONS:
        return "Imagens"
    if suffix in VIDEO_EXTENSIONS or media.resolver == "turbo":
        return "Vídeos"
    if suffix in AUDIO_EXTENSIONS:
        return "Áudios"
    return "Arquivos"


class PreviewSelection:
    """Keep selection independent of the visible page, search and type filter."""

    def __init__(self, items: list[ResolvedMedia]) -> None:
        self.items = items
        self.selected: set[str] = set()

    def filtered_indices(self, query: str = "", kind: str = "Todos") -> list[int]:
        query = query.strip().casefold()
        return [
            index for index, media in enumerate(self.items)
            if (kind == "Todos" or media_kind(media) == kind)
            and query in " ".join((
                media.filename, media.post_label, media.subfolder,
                urlparse(media.origin_url or media.state_key).hostname or "",
            )).casefold()
        ]

    def select(self, indices: list[int], enabled: bool) -> None:
        keys = {self.items[index].state_key for index in indices}
        if enabled:
            self.selected.update(keys)
        else:
            self.selected.difference_update(keys)


def thumbnail_source(media: ResolvedMedia) -> str | None:
    if media.thumbnail_url:
        return media.thumbnail_url
    if media_kind(media) == "Imagens" and not media.resolver:
        return media.url
    return None


def fetch_thumbnail(
    media: ResolvedMedia, cookies_file: str | None, stop: Event,
    *, max_bytes: int = MAX_PREVIEW_BYTES, size: tuple[int, int] = PREVIEW_SIZE,
) -> Image.Image | None:
    """Read at most 4 MiB, never save media, and never sign renewable links.

    Missing/oversized previews do not affect eligibility for download. Each
    worker owns its HTTP session; Tk objects are created only on the UI thread.
    """
    source = thumbnail_source(media)
    if not source or urlparse(source).scheme not in {"http", "https"} or stop.is_set():
        return None
    headers = {"Accept": "image/*,*/*;q=0.8"}
    # Do not forward explicit authorization/cookies or resolver headers to a
    # different thumbnail host. Cookie jars themselves remain domain-scoped.
    if source == media.url:
        headers.update(media.headers or {})
    # Image CDNs commonly require the referring page even for thumbnails.
    # Keep credentials in the domain-scoped cookie jar, never in this header.
    referer = media.origin_url or media.source_url
    if referer and urlparse(referer).scheme in {"http", "https"}:
        headers.setdefault("Referer", referer)
    data = bytearray()
    deadline = time.monotonic() + 12
    try:
        with HttpClient(cookies_file=cookies_file, timeout=8, max_retries=1) as client:
            with client.get(source, headers=headers, stream=True, timeout=(4, 5)) as response:
                if response.status_code != 200:
                    return None
                if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    return None
                if int(response.headers.get("Content-Length") or 0) > max_bytes:
                    return None
                for chunk in response.iter_content(chunk_size=32 * 1024):
                    if stop.is_set() or time.monotonic() > deadline:
                        return None
                    if len(data) + len(chunk) > max_bytes:
                        return None
                    data.extend(chunk)
        if stop.is_set():
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                if original.width * original.height > MAX_PREVIEW_PIXELS:
                    return None
                original.draft("RGB", size)
                thumb = ImageOps.exif_transpose(original)
                thumb.thumbnail(size)
                return thumb.convert("RGB")
    except Exception:  # noqa: BLE001 - an unavailable preview is never a failed download
        return None
