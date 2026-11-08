"""Resolve host page URLs into direct downloadable media URLs.

Python port of the `resolvers` array in ForumPostDownloader's main.js. Each
resolver takes a URL plus an HttpClient and returns a list of ResolvedMedia.

Resolvers are matched in order; the first whose pattern matches wins. Anything
unmatched falls through to `_resolve_direct`, which treats the URL as a direct
link when it already points at a media file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import json
import random
import re
import time
import threading
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from src.config import (
    ARCHIVE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    USER_AGENT,
    VIDEO_EXTENSIONS,
)
from src.file_utils import filename_from_url, sanitize_name
from src.http_client import HttpClient

MEDIA_EXTENSIONS = (
    IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + AUDIO_EXTENSIONS + ARCHIVE_EXTENSIONS
)

GOFILE_WT_SCRIPT = "https://gofile.io/js/wt.obf.js"
GOFILE_DEFAULT_SALT = "12af056dacea0b"
GOFILE_LANGUAGE = "en-US"
_gofile_salt_cache: str | None = None
logger = logging.getLogger(__name__)
_GOFILE_LOCK = threading.RLock()


class ResolverAccessError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status} ao renovar o link: acesso recusado pelo host")


def _truthy(value: object) -> bool:
    """Interpret the string booleans used by a few host APIs."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "ok"}
    return bool(value)


def _first_url_for_keys(value: object, keys: tuple[str, ...], depth: int = 0) -> str | None:
    """Find the first absolute URL stored under one of ``keys`` in nested JSON."""
    if depth > 7:
        return None
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip().lower().startswith(("http://", "https://")):
                return candidate.strip()
        for child in value.values():
            candidate = _first_url_for_keys(child, keys, depth + 1)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for child in value:
            candidate = _first_url_for_keys(child, keys, depth + 1)
            if candidate:
                return candidate
    return None


@dataclass
class ResolvedMedia:
    """A file to download, optionally backed by a renewable source page."""

    url: str
    filename: str
    subfolder: str = ""
    headers: dict[str, str] | None = None
    source_url: str | None = None
    resolver: str | None = None
    thumbnail_url: str | None = None
    post_label: str = ""
    origin_url: str | None = None
    warning: str | None = None
    bunkr_file_id: str | None = None

    @property
    def state_key(self) -> str:
        """Stable key used across refreshes of short-lived signed URLs."""
        return self.source_url or self.url


def is_direct_media(url: str) -> bool:
    """Whether the URL already points at a media file."""
    path = urlparse(url).path.lower()
    return path.endswith(MEDIA_EXTENSIONS)


def _soup_of(client: HttpClient, url: str) -> BeautifulSoup:
    return client.get_soup(url)


def _meta_content(soup: BeautifulSoup, *properties: str) -> str | None:
    for prop in properties:
        node = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop},
        )
        if node and node.get("content"):
            return node["content"]
    return None


# --- Individual resolvers ----------------------------------------------------


def _resolve_direct(url: str, _client: HttpClient) -> list[ResolvedMedia]:
    """Treat the URL as an already-direct media link."""
    if not is_direct_media(url):
        return []

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()

    # Goonbox pages commonly expose both the original and a medium thumbnail
    # such as ``photo.md.jpg``.  The thumbnail endpoint is frequently removed
    # while ``photo.jpg`` remains valid, so normalize both entries to the same
    # original URL.  ThreadDownloader can then de-duplicate them before any
    # network request is made.
    if hostname == "goonbox.cr" and "/images/" in parsed.path.casefold():
        url = re.sub(r"\.(?:md|th)(\.[a-z0-9]{2,5})(?=\?|$)", r"\1", url, flags=re.IGNORECASE)

    headers = None
    if hostname == "i.redd.it":
        # A document-style Accept header (and stale browser cookies) can make
        # Reddit's image CDN return an HTML interstitial for an existing image.
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.reddit.com/",
            "Cookie": "",
        }

    return [ResolvedMedia(url=url, filename=filename_from_url(url), headers=headers)]


def _resolve_nitter(url: str, _client: HttpClient) -> list[ResolvedMedia]:
    """Rewrite nitter proxy image URLs back to pbs.twimg.com."""
    rewritten = re.sub(
        r"https?://nitter\.[^/]+/pic/(orig/)?media%2F(.+)",
        r"https://pbs.twimg.com/media/\2",
        url,
        flags=re.IGNORECASE,
    )
    return [ResolvedMedia(url=rewritten, filename=filename_from_url(rewritten))]


def _resolve_imagevenue(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Follow the interstitial page to the full-size image."""
    soup = _soup_of(client, url)
    node = soup.select_one(".col-md-12 > a > img") or soup.select_one("#main-image")
    if node is None or not node.get("src"):
        return []
    src = urljoin(url, node["src"])
    return [ResolvedMedia(url=src, filename=filename_from_url(src))]


def _resolve_meta_image(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Generic image-host resolver via og:image / twitter:image."""
    soup = _soup_of(client, url)
    src = _meta_content(soup, "og:image", "twitter:image")

    if not src:
        node = (
            soup.select_one("#image-viewer-container img")
            or soup.select_one(".image-viewer img")
            or soup.select_one("img.centred")
            or soup.select_one("link[rel='image_src']")
        )
        if node is not None:
            src = node.get("src") or node.get("href")

    if not src:
        return []

    src = urljoin(url, src)
    src = re.sub(r"\.md(\.[a-z0-9]{2,4})$", r"\1", src)  # strip thumbnail marker
    src = re.sub(r"\.th(\.[a-z0-9]{2,4})$", r"\1", src)
    return [ResolvedMedia(url=src, filename=filename_from_url(src))]


def _resolve_postimg(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """postimg.cc single image."""
    soup = _soup_of(client, url)
    node = soup.select_one("#main-image") or soup.select_one("a#download")
    src = None
    if node is not None:
        src = node.get("src") or node.get("href")
    if not src:
        src = _meta_content(soup, "og:image")
    if not src:
        return []
    src = urljoin(url, src).split("?")[0]
    return [ResolvedMedia(url=src, filename=filename_from_url(src))]


def _resolve_pixhost(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """pixhost.to thumbnails map directly to the image server."""
    if "/thumbs/" in url or re.match(r"https?://t\d*\.pixhost\.to/", url):
        direct = url.replace("/thumbs/", "/images/")
        direct = re.sub(r"https?://t(\d*)\.pixhost\.to", r"https://img\1.pixhost.to", direct)
        return [
            ResolvedMedia(
                url=direct,
                filename=filename_from_url(direct),
                headers={"Referer": "https://pixhost.to/"},
            ),
        ]
    return _resolve_meta_image(url, client)


def _resolve_imgbox(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """imgbox thumbnails and single pages."""
    if re.match(r"https?://(thumbs|images)\d*\.imgbox\.com/", url):
        direct = re.sub(r"https?://thumbs(\d*)\.imgbox\.com", r"https://images\1.imgbox.com", url)
        direct = direct.replace("_t.", "_o.")
        return [ResolvedMedia(url=direct, filename=filename_from_url(direct))]
    return _resolve_meta_image(url, client)


def _resolve_gallery(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Generic album page: collect item links, then resolve each one."""
    soup = _soup_of(client, url)
    folder = sanitize_name(
        (soup.select_one("h1") or soup.select_one("title")).get_text(strip=True)
        if (soup.select_one("h1") or soup.select_one("title"))
        else "album",
    )

    candidates: list[str] = []
    for anchor in soup.select("a[href]"):
        href = urljoin(url, anchor["href"])
        if is_direct_media(href):
            candidates.append(href)

    for img in soup.select("img[src]"):
        src = urljoin(url, img["src"])
        src = re.sub(r"\.(md|th)(\.[a-z0-9]{2,4})$", r"\2", src)
        if is_direct_media(src):
            candidates.append(src)

    seen: set[str] = set()
    resolved: list[ResolvedMedia] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(
            ResolvedMedia(
                url=candidate,
                filename=filename_from_url(candidate),
                subfolder=folder,
            ),
        )
    return resolved


def _resolve_bunkr(url: str, client: HttpClient, *, prefer_download_page: bool = False) -> list[ResolvedMedia]:
    """bunkr file (/f/) or album (/a/) pages -> signed CDN URLs.

    Read the numeric data-file-id (or older data-id/file links), request the
    current metadata API, decode its URL and refresh the CDN signature.
    Legacy mediafiles/path payloads remain supported. Album pages are walked
    until no new slugs remain, with a bound matching the upstream userscript.
    """
    legacy_cdn = re.match(r"https?://(?:cdn\d*|stream)\.bunkr+\.[a-z]+/", url, re.IGNORECASE)
    if legacy_cdn:
        # Follow the old CDN's redirect to the view page, then resolve its ID.
        soup = _soup_of(client, url)
        node = soup.select_one("[data-file-id], #download-btn[data-id]")
        ids = re.findall(r"/file/(\d+)", str(soup))
        legacy_id = (node.get("data-file-id") or node.get("data-id")) if node else (ids[0] if ids else None)
        if not legacy_id:
            return []
    match = re.search(r"bunkr+(\.[a-z]{2,6})/(a|v|f|d)/([\w\-]+)", url, re.IGNORECASE)
    if not match and not legacy_cdn:
        # already-direct CDN link (e.g. cdn*.bunkr or i*.bunkr)
        if is_direct_media(url) and re.search(r"(cdn|i)\d*\.bunkr", url, re.IGNORECASE):
            return [ResolvedMedia(url=url, filename=filename_from_url(url),
                                  headers={"Referer": "https://bunkr.site/"})]
        return []

    kind, slug = (match.group(2), match.group(3)) if match else ("f", legacy_id)
    def _sign_one(file_id: str, folder: str, download_page: str | None = None) -> list[ResolvedMedia]:
        # Current API returns a plain or XOR-encoded URL. Keep the legacy
        # mediafiles/path response supported for older mirrors.
        origin = "https://dl.bunkr.cr" if download_page else "https://get.bunkrr.su"
        referer = download_page or f"{origin}/file/{file_id}"
        bunkr_file_id = file_id if file_id.isascii() and file_id.isdigit() else None
        try:
            response = client.session.post(
                f"{origin if download_page else 'https://apidl.bunkr.ru'}/api/_001_v2",
                json={"id": file_id},
                headers={"Content-Type": "application/json",
                         "Referer": referer, "Origin": origin},
                timeout=client.timeout,
            )
            if getattr(response, "status_code", None) in (401, 403):
                raise ResolverAccessError(response.status_code)
            meta = response.json()
            if not isinstance(meta, dict):
                return []
        except (*requests_exceptions(), KeyError, ValueError):
            return []
        if meta.get("url"):
            try:
                raw = str(meta["url"])
                if _truthy(meta.get("encrypted")):
                    key = f"SECRET_KEY_{int(meta['timestamp']) // 3600}".encode()
                    encoded = base64.b64decode(raw, validate=True)
                    raw = bytes(b ^ key[i % len(key)] for i, b in enumerate(encoded)).decode("utf-8")
                raw = raw.strip()
                if raw.startswith("//"):
                    raw = "https:" + raw
                if urlparse(raw).scheme not in ("http", "https") or not urlparse(raw).netloc:
                    return []
            except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
                return []
            signed = raw
            # Signing is best-effort, as in upstream: some URLs already carry
            # valid credentials or are served by a CDN that needs no signature.
            if not re.search(r"[?&]token=", raw):
                try:
                    sign = client.session.get(
                        "https://glb-apisign.cdn.cr/sign?path=" + quote(unquote(urlparse(raw).path), safe=""),
                        timeout=client.timeout,
                    ).json()
                    if isinstance(sign, dict) and sign.get("token") and sign.get("ex"):
                        parsed = urlparse(raw)
                        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
                        params.update(token=sign["token"], ex=sign["ex"])
                        signed = urlunparse(parsed._replace(query=urlencode(params)))
                except (*requests_exceptions(), ValueError):
                    pass
            name = meta.get("original") or meta.get("filename") or filename_from_url(raw)
            return [ResolvedMedia(url=signed,
                                  filename=sanitize_name(name),
                                  subfolder=folder, headers={"Referer": download_page or "https://bunkr.site/"},
                                  bunkr_file_id=bunkr_file_id)]
        mediafiles = meta.get("mediafiles")
        path = meta.get("path")
        if not isinstance(mediafiles, str) or not isinstance(path, str) or not mediafiles or not path:
            return []
        raw = urlparse(mediafiles.rstrip("/") + "/" + path.lstrip("/"))
        if raw.scheme not in ("http", "https") or not raw.hostname:
            return []
        try:
            sign = client.session.get(
                "https://glb-apisign.cdn.cr/sign?path=" + quote(unquote(raw.path), safe=""),
                headers={"Origin": origin, "Referer": f"{origin}/"} if download_page else {},
                timeout=client.timeout,
            ).json()
        except (*requests_exceptions(), KeyError, ValueError):
            return []
        if not isinstance(sign, dict):
            return []
        token = sign.get("token")
        ex = sign.get("ex")
        if not token or not ex:
            return []
        original = meta.get("original")
        params = dict(parse_qsl(raw.query, keep_blank_values=True))
        params.update(token=token, ex=ex)
        if original:
            params["n"] = original
        signed = urlunparse(raw._replace(query=urlencode(params)))
        return [
            ResolvedMedia(
                url=signed,
                filename=sanitize_name(original) or filename_from_url(signed),
                subfolder=folder,
                headers={"Referer": download_page or "https://bunkr.site/"},
                bunkr_file_id=bunkr_file_id,
            ),
        ]

    if kind == "a":
        soup = _soup_of(client, url)
        folder = sanitize_name(
            soup.select_one("h1").get_text(strip=True) if soup.select_one("h1") else "album",
        )
        items: list[ResolvedMedia] = []
        seen: set[str] = set()
        seen_slugs: set[str] = set()
        for page_number in range(1, 501):
            fresh = 0
            for anchor in soup.select('a[href*="/f/"], a[href*="/v/"], a[href*="/d/"]'):
                item_url = urljoin(url, anchor["href"])
                m = re.search(r"/(f|v|d)/([\w\-]+)", item_url)
                if not m or m.group(2) in seen_slugs:
                    continue
                seen_slugs.add(m.group(2))
                fresh += 1
                try:
                    page = _soup_of(client, item_url)
                    btn = page.select_one("[data-file-id], #download-btn[data-id]")
                    nums = re.findall(r"/file/(\d+)", str(page))
                    file_id = (btn.get("data-file-id") or btn.get("data-id")) if btn else (nums[0] if nums else m.group(2))
                    for media in _sign_one(file_id, folder):
                        if media.url not in seen:
                            seen.add(media.url)
                            items.append(media)
                except requests_exceptions():
                    logger.warning("Bunkr: an album item could not be resolved.")
            if not fresh or page_number == 500:
                break
            parsed = urlparse(url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            params["page"] = str(page_number + 1)
            try:
                soup = _soup_of(client, urlunparse(parsed._replace(query=urlencode(params))))
            except requests_exceptions():
                logger.warning("Bunkr: album pagination failed; returning the files already resolved.")
                break
        return items

    # single file page: numeric id lives in /file/<digits> (data-id absent on
    # the lightweight page); fall back to the slug if nothing matches.
    if legacy_cdn:
        return _sign_one(legacy_id, "bunkr")
    soup = _soup_of(client, url)
    if prefer_download_page:
        # Follow the official button advertised by the file page. Some CDNs
        # reject tokens minted for the alternate metadata returned by apidl.
        for anchor in soup.select('a[href*="/file/"]'):
            page = urljoin(url, anchor["href"])
            parsed = urlparse(page)
            if parsed.netloc != "dl.bunkr.cr" or parsed.scheme != "https" or not parsed.path.startswith("/file/"):
                continue
            try:
                download = _soup_of(client, page)
                button = download.select_one("#download-btn[data-id]")
                ident = button["data-id"] if button else ""
                if ident.isascii() and ident.isdigit():
                    result = _sign_one(ident, "bunkr", page)
                    if result:
                        return result
            except requests_exceptions():
                pass
            break
    btn = soup.select_one("[data-file-id], #download-btn[data-id]")
    nums = re.findall(r"/file/(\d+)", str(soup))
    file_id = (btn.get("data-file-id") or btn.get("data-id")) if btn else (nums[0] if nums else slug)
    return _sign_one(file_id, "bunkr")


def _queue_bunkr(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Collect source pages, never mint thousands of expiring URLs in advance."""
    if "/a/" not in url:
        if is_direct_media(url) and not re.match(r"https?://(?:cdn\d*|stream)\.bunkr", url, re.I):
            return _resolve_bunkr(url, client)
        return [ResolvedMedia(url=url, filename=filename_from_url(url), source_url=url, resolver="bunkr")]
    base = urlparse(url)
    items, seen = [], set()
    folder = "bunkr"
    for page_number in range(1, 501):
        params = dict(parse_qsl(base.query))
        if page_number > 1:
            params["page"] = str(page_number)
        page_url = urlunparse(base._replace(query=urlencode(params)))
        try:
            soup = _soup_of(client, page_url)
        except requests_exceptions():
            break
        if page_number == 1:
            title = soup.select_one("h1")
            folder = sanitize_name(title.get_text(" ", strip=True) if title else "bunkr")
        fresh = 0
        for anchor in soup.select('a[href*="/f/"], a[href*="/v/"], a[href*="/d/"]'):
            source = urljoin(url, anchor["href"])
            match = re.search(r"/(?:f|v|d)/([\w-]+)", source)
            if not match or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            fresh += 1
            name = anchor.get("title") or anchor.get("download") or anchor.get_text(" ", strip=True)
            name = sanitize_name(name or filename_from_url(source))
            image = anchor.select_one("img[src], img[data-src]")
            thumbnail = urljoin(url, image.get("data-src") or image.get("src")) if image else None
            items.append(ResolvedMedia(url=source, filename=name, subfolder=folder,
                                       source_url=source, resolver="bunkr", thumbnail_url=thumbnail))
        if not fresh:
            break
    return items


def _resolve_cyberdrop(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """cyberdrop album/file pages."""
    if re.search(r"fs-\d+\.cyberdrop", url):
        return [ResolvedMedia(url=url, filename=filename_from_url(url))]

    soup = _soup_of(client, url)

    if "/a/" in url:
        folder = sanitize_name(
            soup.select_one("h1#title").get_text(strip=True)
            if soup.select_one("h1#title")
            else "album",
        )
        items: list[ResolvedMedia] = []
        for anchor in soup.select("a.image[href]"):
            item_url = urljoin(url, anchor["href"])
            for media in _resolve_cyberdrop(item_url, client):
                media.subfolder = folder
                items.append(media)
        return items

    node = soup.select_one("#file[href], a#downloadBtn[href], video source[src]")
    src = node.get("href") or node.get("src") if node else _meta_content(soup, "og:image")
    if not src:
        return []
    src = urljoin(url, src)
    return [ResolvedMedia(url=src, filename=filename_from_url(src))]


def _resolve_pixeldrain(url: str, _client: HttpClient) -> list[ResolvedMedia]:
    """pixeldrain uses a stable API path."""
    match = re.search(r"(?:pixeldrain\.(?:com|net)|pixeldra\.in)/([lu])/(\w+)", url)
    if not match:
        return []
    kind, file_id = match.groups()
    if kind == "u":
        return [
            ResolvedMedia(
                url=f"https://pixeldrain.com/api/file/{file_id}?download",
                filename=file_id,
            ),
        ]
    return [
        ResolvedMedia(
            url=f"https://pixeldrain.com/api/list/{file_id}/zip",
            filename=f"{file_id}.zip",
        ),
    ]


def _resolve_erome(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """erome albums expose direct media in the markup."""
    soup = _soup_of(client, url)
    title = soup.select_one("h1, .col-sm-12 h1")
    folder = sanitize_name(title.get_text(strip=True) if title else "erome")

    items: list[ResolvedMedia] = []
    seen: set[str] = set()
    for node in soup.select("div.media-group img[data-src], div.media-group source[src]"):
        src = node.get("data-src") or node.get("src")
        if not src:
            continue
        src = urljoin(url, src)
        if src in seen:
            continue
        seen.add(src)
        items.append(
            ResolvedMedia(
                url=src,
                filename=filename_from_url(src),
                subfolder=folder,
                headers={"Referer": "https://www.erome.com/"},
            ),
        )
    return items


def _resolve_filester(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Resolve Filester v2 links using the server selected for each token.

    Filester's old v1 URL and fixed-CDN fallbacks are no longer valid.  The v2
    response chooses an ``fscN.cdn.cr`` server and the token only works on that
    server, so the response is deliberately trusted instead of constrained to
    a hard-coded host list.
    """
    match = re.search(r"filester\.(?:me|sh|si|gg)/(f|d)/([\w\-]+)", url, re.IGNORECASE)
    if not match:
        return []
    kind, slug = match.groups()
    parsed = urlparse(url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc or 'filester.me'}"

    folder = "filester"
    slugs: list[tuple[str, str]] = []  # (slug, display_name)

    if kind == "f":
        soup = _soup_of(client, url)
        folder_node = soup.select_one("h1, title")
        folder = sanitize_name(
            folder_node.get_text(strip=True) if folder_node else "filester",
        )
        for item in soup.select("div.file-item, a[href*='/d/']"):
            onclick = item.get("onclick", "")
            href = item.get("href", "")
            m = re.search(r"/d/([\w\-]+)", f"{onclick} {href}")
            if not m:
                continue
            name = item.get("data-name", "")
            if not name:
                name_node = item.select_one(".file-name, [data-name]")
                name = (name_node.get("data-name") if name_node else "") or (
                    name_node.get_text(" ", strip=True) if name_node else ""
                )
            slugs.append((m.group(1), name))
    else:
        slugs.append((slug, ""))

    if not slugs:
        return []

    items: list[ResolvedMedia] = []
    seen: set[str] = set()

    for fslug, fname in slugs:
        resp: dict = {}
        for attempt in range(3):
            try:
                response = client.session.post(
                    f"{origin}/v2/api/public/download",
                    json={"file_slug": fslug},
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Content-Type": "application/json;charset=UTF-8",
                        "Origin": origin,
                        "Referer": f"{origin}/d/{fslug}",
                    },
                    timeout=min(client.timeout, 20),
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))
                    continue
                resp = response.json()
                break
            except (*requests_exceptions(), KeyError, ValueError):
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))

        if not resp:
            continue

        if not _truthy(resp.get("success")) or not resp.get("file") or not resp.get("token"):
            continue

        server = str(resp.get("server") or "").rstrip("/")
        if not server.startswith(("http://", "https://")):
            continue
        file_path = "/".join(quote(part, safe="") for part in str(resp["file"]).split("/"))
        name = resp.get("name") or fname
        query = {"token": str(resp["token"]), "download": "true"}
        if name:
            query["n"] = name
        direct = f"{server}/v2/{file_path}?{urlencode(query)}"

        if direct in seen:
            continue
        seen.add(direct)
        items.append(
            ResolvedMedia(
                url=direct,
                filename=sanitize_name(name) or filename_from_url(direct),
                subfolder=folder,
                headers={"Referer": f"{origin}/d/{fslug}"},
            ),
        )
    return items


def _queue_filester(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Queue stable Filester /d/ pages without minting expiring tokens yet."""
    match = re.search(r"filester\.(?:me|sh|si|gg)/(f|d)/([\w\-]+)", url, re.IGNORECASE)
    if not match:
        return []
    kind, slug = match.groups()
    parsed = urlparse(url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc or 'filester.me'}"

    if kind == "d":
        source = f"{origin}/d/{slug}"
        return [
            ResolvedMedia(
                url=source,
                filename=f"Filester_{slug}",
                source_url=source,
                resolver="filester",
            )
        ]

    soup = _soup_of(client, url)
    folder_node = soup.select_one("h1, title")
    folder = sanitize_name(
        folder_node.get_text(strip=True) if folder_node else "filester",
    )
    queued: list[ResolvedMedia] = []
    seen: set[str] = set()
    for item in soup.select("div.file-item, a[href*='/d/']"):
        onclick = item.get("onclick", "")
        href = item.get("href", "")
        item_match = re.search(r"/d/([\w\-]+)", f"{onclick} {href}")
        if not item_match:
            continue
        item_slug = item_match.group(1)
        source = f"{origin}/d/{item_slug}"
        if source in seen:
            continue
        seen.add(source)
        name = item.get("data-name", "")
        if not name:
            name_node = item.select_one(".file-name, [data-name]")
            name = (name_node.get("data-name") if name_node else "") or (
                name_node.get_text(" ", strip=True) if name_node else ""
            )
        queued.append(
            ResolvedMedia(
                url=source,
                filename=sanitize_name(name) if name else f"Filester_{item_slug}",
                subfolder=folder,
                source_url=source,
                resolver="filester",
            )
        )
    return queued


def _discover_gofile_salt(client: HttpClient, *, force: bool = False) -> str:
    """Read a literal salt without evaluating JavaScript from the network."""
    global _gofile_salt_cache
    if _gofile_salt_cache and not force:
        return _gofile_salt_cache

    try:
        source = client.get(GOFILE_WT_SCRIPT, timeout=min(client.timeout, 20)).text
        if "generateWT" in source:
            salts = set(re.findall(r"""::([A-Za-z0-9_-]{6,64})(?=['"])""", source))
            if len(salts) == 1:
                _gofile_salt_cache = salts.pop()
                return _gofile_salt_cache
    except (*requests_exceptions(), OSError):
        pass

    logger.warning("GoFile: script format changed or unavailable; using the last known website-token salt.")
    _gofile_salt_cache = GOFILE_DEFAULT_SALT
    return _gofile_salt_cache


def _gofile_website_token(
    account_token: str,
    *,
    salt: str,
    user_agent: str = USER_AGENT,
    language: str = GOFILE_LANGUAGE,
    now: float | None = None,
) -> str:
    """Compute the current four-hour-bucket X-Website-Token."""
    bucket = int((time.time() if now is None else now) / 14_400)
    raw = f"{user_agent}::{language}::{account_token}::{bucket}::{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_gofile(
    url: str,
    client: HttpClient,
    passwords: list[str] | None = None,
) -> list[ResolvedMedia]:
    """Resolve current GoFile files/folders with dynamic website auth."""
    match = re.search(r"gofile\.io/d/([\w-]+)", url)
    if not match:
        return []
    content_id = match.group(1)
    passwords = passwords or []

    def new_account() -> str:
        account = client.session.post(
            "https://api.gofile.io/accounts",
            json={},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=client.timeout,
        ).json()
        token = account["data"]["token"]
        client._gofile_token = token
        client.session.cookies.set("accountToken", token, domain=".gofile.io", path="/")
        try:
            client.session.get(
                "https://api.gofile.io/accounts/website",
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                timeout=client.timeout,
            )
        except requests_exceptions():
            pass
        return token

    try:
        with getattr(client, "_gofile_lock", _GOFILE_LOCK):
            token = getattr(client, "_gofile_token", None) or new_account()
    except (*requests_exceptions(), KeyError, ValueError, json.JSONDecodeError):
        return []

    def fetch(content: str, password_hash: str | None = None, *, refresh_salt: bool = False, page: int = 1) -> dict:
        salt = _discover_gofile_salt(client, force=refresh_salt)
        params: dict[str, object] = {
            "contentFilter": "",
            "page": page,
            "pageSize": 1000,
            "sortField": "createTime",
            "sortDirection": -1,
        }
        if password_hash:
            params["password"] = password_hash
        website_token = _gofile_website_token(token, salt=salt)
        response = client.session.get(
            f"https://api.gofile.io/contents/{content}",
            params=params,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Website-Token": website_token,
                "X-BL": GOFILE_LANGUAGE,
            },
            timeout=client.timeout,
        )
        if response.status_code in (401, 403):
            return {"status": "error-unauthorized"}
        return response.json()

    def fetch_with_password(content: str, page: int = 1) -> dict:
        nonlocal token
        try:
            result = fetch(content, page=page)
            status = str(result.get("status") or result.get("message") or "").lower()
            if any(word in status for word in ("unauthorized", "invalid token", "website-token")):
                _discover_gofile_salt(client, force=True)
                with getattr(client, "_gofile_lock", _GOFILE_LOCK):
                    token = new_account()
                result = fetch(content, page=page)
            if result.get("status") == "error-passwordRequired":
                for password in passwords:
                    attempt = fetch(content, hashlib.sha256(password.encode("utf-8")).hexdigest(), page=page)
                    if attempt.get("status") == "ok":
                        return attempt
            return result
        except (*requests_exceptions(), KeyError, ValueError, json.JSONDecodeError):
            return {}

    root = fetch_with_password(content_id)
    if root.get("status") != "ok" or not isinstance(root.get("data"), dict):
        return []

    root_data = root["data"]
    folder = sanitize_name(root_data.get("name", content_id))

    items: list[ResolvedMedia] = []
    seen_folders: set[str] = set()
    seen_urls: set[str] = set()

    def add_file(obj: dict) -> None:
        file_id = str(obj.get("id") or obj.get("code") or "")
        candidates = [obj.get("directLink"), obj.get("link"), obj.get("downloadLink")]
        link = next((str(item) for item in candidates if item and "/download/direct/" in str(item)), None)
        if not link:
            link = next((str(item) for item in candidates if item), None)
        if not link and file_id:
            encoded_name = quote(str(obj.get("name") or file_id), safe="")
            link = f"https://gofile.io/download/web/{file_id}/{encoded_name}"
        if not link or link in seen_urls:
            return
        seen_urls.add(link)
        items.append(
            ResolvedMedia(
                url=link,
                filename=sanitize_name(str(obj.get("name") or "")) or filename_from_url(link),
                subfolder=folder,
                headers={"Cookie": f"accountToken={token}", "Referer": "https://gofile.io/"},
                source_url=f"https://gofile.io/d/{file_id}" if file_id else None,
                resolver="gofile" if file_id else None,
            )
        )

    def walk(data: dict, folder_id: str) -> None:
        if data.get("type") == "file":
            add_file(data)
            return
        seen_children = set()
        for page in range(1, 10001):
            children = data.get("children") or {}
            if not isinstance(children, dict):
                break
            fresh = 0
            for key, child in children.items():
                if key in seen_children or not isinstance(child, dict):
                    continue
                seen_children.add(key)
                fresh += 1
                if child.get("type") == "file":
                    add_file(child)
                elif child.get("type") == "folder":
                    child_id = str(child.get("id") or child.get("code") or "")
                    if child_id and child_id not in seen_folders:
                        seen_folders.add(child_id)
                        nested = fetch_with_password(child_id)
                        if nested.get("status") == "ok" and isinstance(nested.get("data"), dict):
                            walk(nested["data"], child_id)
            if not fresh or len(children) < 1000:
                break
            next_page = fetch_with_password(folder_id, page + 1)
            if next_page.get("status") != "ok" or not isinstance(next_page.get("data"), dict):
                break
            data = next_page["data"]

    walk(root_data, content_id)
    return items


def _resolve_redgifs(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """redgifs exposes an public API for HD mp4s."""
    match = re.search(r"redgifs\.com/(?:ifr|watch)/(\w+)", url, re.IGNORECASE)
    if not match:
        return []
    gif_id = match.group(1).lower()

    try:
        auth = client.get_json("https://api.redgifs.com/v2/auth/temporary")
        token = auth.get("token")
        response = client.session.get(
            f"https://api.redgifs.com/v2/gifs/{gif_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=client.timeout,
        ).json()
    except (*requests_exceptions(), KeyError, json.JSONDecodeError):
        return []

    urls = response.get("gif", {}).get("urls", {})
    src = urls.get("hd") or urls.get("sd")
    if not src:
        return []
    return [
        ResolvedMedia(
            url=src,
            filename=f"{gif_id}.mp4",
            headers={"Referer": "https://www.redgifs.com/"},
        ),
    ]


def _resolve_saint(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """saint.to embed pages carry a <source> tag."""
    soup = _soup_of(client, url)
    node = soup.select_one("video source[src], source[src]")
    src = node.get("src") if node else _meta_content(soup, "og:video")
    if not src:
        return []
    src = urljoin(url, src)
    return [
        ResolvedMedia(
            url=src,
            filename=filename_from_url(src),
            headers={"Referer": url},
        ),
    ]


def _resolve_imgur(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """imgur direct links and single pages."""
    if re.match(r"https?://i\.imgur\.(com|io)/", url):
        return [ResolvedMedia(url=url, filename=filename_from_url(url))]
    return _resolve_meta_image(url, client)


def _resolve_turbo(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Resolve Turbo singles/albums with bounded retries and legacy fallback."""
    if "turbocdn.st" in url and is_direct_media(url):
        return _resolve_direct(url, client)

    if re.search(r"turbo\.cr/a/", url, re.IGNORECASE):
        soup = _soup_of(client, url)
        folder_node = soup.select_one("h1, title")
        folder = sanitize_name(folder_node.get_text(" ", strip=True) if folder_node else "turbo")
        items: list[ResolvedMedia] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href*='/embed/'], a[href*='/v/'], a[href*='/d/']"):
            item_url = urljoin(url, anchor.get("href", ""))
            if not item_url or item_url in seen:
                continue
            seen.add(item_url)
            for media in _resolve_turbo(item_url, client):
                media.subfolder = folder
                items.append(media)
        return items

    match = re.search(r"turbo\.cr/(?:embed|v|d)/([\w\-]+)", url, re.IGNORECASE)
    if not match:
        return []
    video_id = match.group(1)

    referer = f"https://turbo.cr/embed/{video_id}"
    resp: dict = {}
    for attempt in range(3):
        for endpoint in ("api/sign", "sign"):
            try:
                response = client.session.get(
                    f"https://turbo.cr/{endpoint}?v={video_id}",
                    headers={"User-Agent": USER_AGENT, "Referer": referer, "Accept": "application/json"},
                    timeout=min(client.timeout, 5),
                )
                if response.status_code == 200:
                    candidate = response.json()
                    if candidate.get("url") and (
                        candidate.get("success") is None or _truthy(candidate.get("success"))
                    ):
                        resp = candidate
                        break
            except (*requests_exceptions(), json.JSONDecodeError):
                continue
        if resp:
            break
        if attempt < 2:
            time.sleep(random.uniform(0.7, 1.4))

    signed = str(resp.get("url") or "")
    if not signed:
        return []

    return [
        ResolvedMedia(
            url=signed,
            filename=(
                resp.get("original_filename")
                or resp.get("filename")
                or filename_from_url(signed)
                or f"{video_id}.mp4"
            ),
            headers={"Referer": "https://turbo.cr/"},
        ),
    ]


def _queue_turbo(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Keep Turbo page IDs stable and sign each file only when a worker starts."""
    if "turbocdn.st" in url and is_direct_media(url):
        return _resolve_direct(url, client)

    if re.search(r"turbo\.cr/a/", url, re.IGNORECASE):
        soup = _soup_of(client, url)
        folder_node = soup.select_one("h1, title")
        folder = sanitize_name(
            folder_node.get_text(" ", strip=True) if folder_node else "turbo",
        )
        queued: list[ResolvedMedia] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href*='/embed/'], a[href*='/v/'], a[href*='/d/']"):
            source = urljoin(url, anchor.get("href", ""))
            match = re.search(r"turbo\.cr/(?:embed|v|d)/([\w\-]+)", source, re.IGNORECASE)
            if not match or source in seen:
                continue
            seen.add(source)
            image = anchor.select_one("img")
            poster = (image.get("data-src") or image.get("src")) if image else None
            queued.append(
                ResolvedMedia(
                    url=source,
                    filename=f"Turbo_{match.group(1)}",
                    subfolder=folder,
                    source_url=source,
                    resolver="turbo",
                    thumbnail_url=urljoin(url, poster) if poster else None,
                )
            )
        return queued

    match = re.search(r"turbo\.cr/(?:embed|v|d)/([\w\-]+)", url, re.IGNORECASE)
    if not match:
        return []
    source = f"https://turbo.cr/v/{match.group(1)}"
    return [
        ResolvedMedia(
            url=source,
            filename=f"Turbo_{match.group(1)}",
            source_url=source,
            resolver="turbo",
        )
    ]


def refresh_media(media: ResolvedMedia, client: HttpClient, *, prefer_download_page: bool = False) -> ResolvedMedia | None:
    """Mint a fresh direct URL for renewable hosts immediately before download."""
    source = media.source_url or media.url
    if media.resolver == "filester":
        resolved = _resolve_filester(source, client)
    elif media.resolver == "turbo":
        resolved = _resolve_turbo(source, client)
    elif media.resolver == "bunkr":
        resolved = _resolve_bunkr(source, client, prefer_download_page=True) if prefer_download_page else _resolve_bunkr(source, client)
    elif media.resolver == "gofile":
        resolved = _resolve_gofile(source, client)
    else:
        return media
    if not resolved:
        return None
    fresh = resolved[0]
    fresh.subfolder = media.subfolder or fresh.subfolder
    fresh.source_url = source
    fresh.resolver = media.resolver
    fresh.origin_url = media.origin_url
    fresh.post_label = media.post_label
    fresh.thumbnail_url = media.thumbnail_url or fresh.thumbnail_url
    return fresh


def _resolve_goodbox(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Resolve Goonbox with API-shape tolerance and page thumbnail fallback.

    The userscript can open a first-party browser tab to cross Cloudflare.  A
    CLI cannot reuse that browser context, so it first visits the real page to
    establish cookies, then calls the API with first-party headers.  If the API
    is still challenged, the embedded medium-resolution image is returned
    instead of silently dropping the resource.
    """
    image_headers = goonbox_image_headers(url)
    try:
        page_soup = _soup_of(client, url)
    except requests_exceptions():
        page_soup = BeautifulSoup("", "html.parser")
    fallback = _meta_content(page_soup, "og:image", "twitter:image")
    if fallback:
        fallback = urljoin(url, fallback)
    if not fallback:
        image = page_soup.select_one("img[src*='cuckcapital'], img[src*='cdn.cr'], main img[src]")
        fallback = urljoin(url, image.get("src")) if image and image.get("src") else None

    def api_json(path: str) -> dict | None:
        try:
            response = client.session.get(
                f"https://goonbox.cr{path}",
                headers={
                    "Accept": "application/json",
                    "Origin": "https://goonbox.cr",
                    "Referer": url,
                    "User-Agent": USER_AGENT,
                },
                timeout=client.timeout,
            )
            if response.status_code != 200:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except (*requests_exceptions(), json.JSONDecodeError, ValueError):
            return None

    if "/a/" in url:
        match = re.search(r"goonbox\.cr/a/([\w\-]+)", url)
        if not match:
            return []
        slug = match.group(1)
        items: list[ResolvedMedia] = []
        seen: set[str] = set()
        page = 1
        while True:
            resp = api_json(f"/api/albums/{slug}/images?page={page}")
            if not resp:
                break
            imgs = resp.get("images") or []
            if not imgs:
                break
            for img in imgs:
                src = _first_url_for_keys(img, ("original_url", "originalUrl", "original", "url"))
                if src and src not in seen:
                    seen.add(src)
                    items.append(
                        ResolvedMedia(
                            url=src,
                            filename=filename_from_url(src),
                            subfolder=f"goonbox_{slug}",
                            headers=image_headers,
                        )
                    )
            last = (resp.get("pagination") or {}).get("last_page")
            if not last or page >= int(last):
                break
            page += 1
        if not items:
            for node in page_soup.select("a[href], img[src]"):
                src = node.get("href") or node.get("src")
                if src:
                    src = urljoin(url, src)
                    if is_direct_media(src) and src not in seen:
                        seen.add(src)
                        items.append(ResolvedMedia(url=src, filename=filename_from_url(src),
                                                   subfolder=f"goonbox_{slug}", headers=image_headers))
        return items

    match = re.search(r"goonbox\.cr/img/([\w\-]+)", url)
    if not match:
        return []
    img_id = match.group(1)
    resp = api_json(f"/api/images/{img_id}")
    src = _first_url_for_keys(resp, ("original_url", "originalUrl", "original")) if resp else None
    if not src:
        src = fallback
    if not src:
        return []

    # Only replace an original after both HEAD and a ranged GET confirm failure.
    if src != fallback:
        reachable = False
        try:
            head = client.session.head(src, headers=image_headers, timeout=min(client.timeout, 10), allow_redirects=True)
            reachable = 200 <= head.status_code < 400
        except requests_exceptions():
            pass
        if not reachable:
            try:
                probe = client.session.get(
                    src,
                    headers={**image_headers, "Range": "bytes=0-0"},
                    timeout=min(client.timeout, 10),
                    stream=True,
                    allow_redirects=True,
                )
                reachable = 200 <= probe.status_code < 400
                probe.close()
            except requests_exceptions():
                pass
        if not reachable and fallback:
            src = fallback
    warning = "Goonbox: original indisponível; este arquivo é uma miniatura, com resolução reduzida." if src == fallback else None
    return [ResolvedMedia(url=src, filename=filename_from_url(src), headers=image_headers, warning=warning)]


def goonbox_image_headers(referer: str = "https://goonbox.cr/") -> dict[str, str]:
    return {
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": referer,
        "User-Agent": USER_AGENT,
    }


def _resolve_coomer(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Resolve Coomer profile pages and direct data/thumbnail links."""
    if re.search(r"coomer\.(?:st|party|su)/(?:data|thumbnail)/", url, re.IGNORECASE):
        return _resolve_direct(url, client)
    if "/user/" not in url:
        return []

    profile_id = urlparse(url).path.rstrip("/").split("/")[-1]
    current = url.split("?", 1)[0]
    post_links: list[str] = []
    seen_pages: set[str] = set()
    for _ in range(100):
        if current in seen_pages:
            break
        seen_pages.add(current)
        soup = _soup_of(client, current)
        for anchor in soup.select(".card-list__items article .post-card__heading a[href], article a[href*='/post/']"):
            post_url = urljoin(current, anchor.get("href", ""))
            if post_url and post_url not in post_links:
                post_links.append(post_url)
        next_node = soup.select_one("a[title='Next page'][href], a[rel='next'][href]")
        if not next_node:
            break
        current = urljoin(current, next_node.get("href", ""))

    items: list[ResolvedMedia] = []
    seen: set[str] = set()
    for post_url in post_links:
        soup = _soup_of(client, post_url)
        post_id = urlparse(post_url).path.rstrip("/").split("/")[-1]
        for anchor in soup.select(".post__thumbnail a[href], .post__attachment-link[href], .fileThumb[href]"):
            direct = urljoin(post_url, anchor.get("href", ""))
            if direct in seen or not is_direct_media(direct):
                continue
            seen.add(direct)
            items.append(
                ResolvedMedia(
                    url=direct,
                    filename=filename_from_url(direct),
                    subfolder=sanitize_name(f"{profile_id}/{post_id}"),
                )
            )
    return items


def _resolve_video_page(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """Resolve common adult/video pages instead of treating HTML as media."""
    soup = _soup_of(client, url)

    if "noodlemagazine.com" in url:
        iframe = soup.select_one("#iplayer[src], iframe[src*='/player/']")
        if iframe and iframe.get("src"):
            playlist = urljoin(url, iframe["src"]).replace("/player/", "/playlist/")
            try:
                payload = client.get_json(playlist, headers={"Referer": url})
                sources = payload.get("sources") or []
                if sources and sources[0].get("file"):
                    src = urljoin(playlist, sources[0]["file"])
                    return [ResolvedMedia(url=src, filename=filename_from_url(src), headers={"Referer": url})]
            except (*requests_exceptions(), KeyError, ValueError, json.JSONDecodeError):
                pass

    node = soup.select_one("video source[src], video[src]")
    src = (node.get("src") if node else None) or _meta_content(
        soup,
        "og:video",
        "og:video:url",
        "og:video:secure_url",
        "twitter:player:stream",
    )
    if not src:
        source = str(soup)
        candidates = re.findall(
            r"https?:\\?/\\?/[^^\s\"'<>]+?\.(?:mp4|m3u8|webm)(?:\?[^\s\"'<>]*)?",
            source,
            re.IGNORECASE,
        )
        if candidates:
            src = candidates[-1].replace(r"\/", "/").replace("&amp;", "&")
    if not src:
        return []
    src = urljoin(url, src)
    return [ResolvedMedia(url=src, filename=filename_from_url(src), headers={"Referer": url})]


def _resolve_box(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """box.com shared file links.

    The public share page redirects to a download URL; the final URL carries
    the real file. Mirrors ForumPostDownloader's @connect box.com / boxcloud.com.
    """
    try:
        resp = client.session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=client.timeout,
            allow_redirects=True,
        )
    except requests_exceptions():
        return []
    final = resp.url
    # Box serves the file via a /download URL or a content-disposition; prefer final.
    if any(final.lower().endswith(ext) for ext in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + ARCHIVE_EXTENSIONS):
        return [ResolvedMedia(url=final, filename=filename_from_url(final))]
    # Try the explicit download endpoint.
    dl = url.rstrip("/") + "/download"
    try:
        r2 = client.session.head(dl, headers={"User-Agent": USER_AGENT}, timeout=client.timeout, allow_redirects=True)
        if r2.status_code < 400 and "content-disposition" in r2.headers:
            return [ResolvedMedia(url=r2.url, filename=filename_from_url(r2.url))]
    except requests_exceptions():
        pass
    return []


def _resolve_pomf2(url: str, client: HttpClient) -> list[ResolvedMedia]:
    """pomf2.lain.la and similar pomf clones — direct file host.

    The /f/<id> or /<id> page exposes the real file at /data/<id>.ext or via a
    download link. Fall back to treating the URL itself if it ends in an ext.
    """
    try:
        resp = client.session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=client.timeout,
            allow_redirects=True,
        )
    except requests_exceptions():
        return []
    final = resp.url
    if any(final.lower().endswith(ext) for ext in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + ARCHIVE_EXTENSIONS):
        return [ResolvedMedia(url=final, filename=filename_from_url(final))]
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if any(h.lower().endswith(ext) for ext in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + ARCHIVE_EXTENSIONS):
            full = h if h.startswith("http") else urljoin(url, h)
            return [ResolvedMedia(url=full, filename=filename_from_url(full))]
    return []


def requests_exceptions():  # noqa: ANN201
    """Lazy import to keep the exception tuple in one place."""
    import requests

    return (requests.RequestException, ValueError)


# --- Registry ----------------------------------------------------------------

RESOLVERS: list[tuple[str, object]] = [
    (r"nitter\.[^/]+/pic/", _resolve_nitter),
    (r"imagevenue\.com", _resolve_imagevenue),
    (r"(cdn|i|stream)\d*\.bunkr|bunkrr?\.|bunkrrr\.org|bunkr-cache\.se|apidl\.bunkr\.ru|get\.bunkrr\.su|bunkr\.(ac|ax|black|cat|ci|cr|fi|is|media|nu|red|ru|se|si|site|pk|ph|ps|sk|ws)", _queue_bunkr),
    (r"cyberdrop\.[a-z]{2,}|fs-\d+\.cyberdrop|cyberfile\.(su|me)", _resolve_cyberdrop),
    (r"(?:pixeldrain\.(?:com|net)|pixeldra\.in)/[lu]/", _resolve_pixeldrain),
    (r"erome\.com/a/", _resolve_erome),
    (r"filester\.(me|sh|si|gg)/(f|d)/", _queue_filester),
    (r"gofile\.io/d/", _resolve_gofile),
    (r"redgifs\.com/(ifr|watch)/", _resolve_redgifs),
    (r"saint\d?\.(to|su|pk|cr)", _resolve_saint),
    (r"imgur\.(com|io)", _resolve_imgur),
    (r"turbo\.cr/(?:embed|v|d|a)/[\w\-]+|turbocdn\.st", _queue_turbo),
    (r"goonbox\.cr/(?:img|a)/", _resolve_goodbox),
    (r"(postimg|pixxxels)\.cc", _resolve_postimg),
    (r"pixhost\.to", _resolve_pixhost),
    (r"imgbox\.com", _resolve_imgbox),
    (r"ibb\.co/album/|jpe?g\d?\.\w+/(?:a|album)/|pixl\.(li|is)/album/|img\.kiwi/album/|imgvb\.com/album", _resolve_gallery),
    (r"ibb\.co|jpe?g\d?\.(church|fish|fishing|pet|su|cr|homes)|pixl\.(li|is)|img\.kiwi|imgvb\.com|imagebam\.com",
     _resolve_meta_image),
    (r"box\.com|boxcloud\.com", _resolve_box),
    (r"pomf2?\.lain\.la|pomf\.lain\.la", _resolve_pomf2),
    (r"coomer\.(st|party|su)", _resolve_coomer),
    (r"kemono\.(cr|su|party)", _resolve_direct),
    (r"cuckcapital\.cr|scdn\.st|cache8\.st|cdn\.cr|simp\d?\.cuckcapital\.cr", _resolve_direct),
    (r"onlyfans\.com|pbs\.twimg\.com|media\.tumblr\.com|reddit\.com|twitter\.com", _resolve_direct),
    (r"pornhub\.com|spankbang\.com|xvideos\.com|rule34\.xxx|noodlemagazine\.com|pvvstream\.pro", _resolve_video_page),
    (r"sb-cd\.com", _resolve_direct),
]


def resolve(
    url: str,
    client: HttpClient,
    passwords: list[str] | None = None,
) -> list[ResolvedMedia]:
    """Resolve a single host URL into concrete downloadable media."""
    for pattern, resolver in RESOLVERS:
        if re.search(pattern, url, re.IGNORECASE):
            try:
                if resolver is _resolve_gofile:
                    resolved = resolver(url, client, passwords=passwords)  # type: ignore[operator]
                else:
                    resolved = resolver(url, client)  # type: ignore[operator]
            except Exception:  # noqa: BLE001 - a broken host must not kill the run
                resolved = []
            if resolved:
                return resolved
            break

    return _resolve_direct(url, client)
