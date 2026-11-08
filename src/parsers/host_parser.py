"""Match host URLs inside post HTML — port of parsers.hosts.parseHosts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

from src.config import DIRECT_LINK_PATTERN, HOSTS

ATTRIBUTE_PREFIX = r"(?:(?<=href=\")|(?<=src=\")|(?<=data-url=\")|(?<=data-src=\")|(?<=content=\")|\b)"
URL_TAIL = r"[^\s\"'<>\]\)]*"

# URL substrings that mark forum chrome rather than user-posted media
# (profile cover images, avatars, favicons). These appear as ordinary <img>
# tags inside a post body, so the class-based noise stripping never sees them.
DECORATIVE_PATH_TOKENS = (
    "profile_banner",
    "banner",
    "/avatar",
    "_avatar",
    "avatar_",
    "favicon",
)


def _is_decorative(url: str) -> bool:
    """True when the URL points at forum chrome, not posted content."""
    lowered = url.lower()
    return any(token in lowered for token in DECORATIVE_PATH_TOKENS)


@dataclass(frozen=True)
class HostMatch:
    """A URL found in a post, tagged with the host that claimed it."""

    name: str
    category: str
    kind: str  # "single" or "album"
    url: str


def _build_pattern(fragment: str) -> re.Pattern[str]:
    """Wrap a host fragment into a full absolute-URL regex."""
    return re.compile(
        rf"https?://(?:www\.)?{fragment}{URL_TAIL}",
        re.IGNORECASE | re.DOTALL,
    )


def _normalize(url: str, *, strip_query: bool = False) -> str:
    """Trim trailing junk from a scraped URL."""
    cleaned = unescape(url).strip()
    cleaned = cleaned.rstrip("\\")
    cleaned = re.sub(r"[.,;:'\")\]]+$", "", cleaned)
    if strip_query and "?" in cleaned:
        cleaned = cleaned.split("?", 1)[0]
    return cleaned.rstrip("/") if cleaned.count("/") > 3 else cleaned


def _category_label(category: str, kind: str) -> str:
    """Human-readable category, mirroring the userscript's labels."""
    if kind == "album":
        base = category or "misc"
        return f"{base.capitalize()} Albums"

    if category in ("image", "video"):
        return f"{category.capitalize()}s"
    if category.strip():
        return category.capitalize()
    return "Direct Links"


def parse_hosts(post_html: str, *, include_direct: bool = True) -> list[HostMatch]:
    """Return every host URL found in the post content, de-duplicated."""
    matches: list[HostMatch] = []
    seen: set[str] = set()

    def add(name: str, category: str, kind: str, url: str) -> None:
        normalized = _normalize(url, strip_query=kind == "album")
        if not normalized or normalized in seen:
            return
        if _is_decorative(normalized):
            return
        seen.add(normalized)
        matches.append(
            HostMatch(
                name=name,
                category=_category_label(category, kind),
                kind=kind,
                url=normalized,
            ),
        )

    for name, category, single, album in HOSTS:
        if single:
            for url in _findall_full(post_html, single):
                add(name, category, "single", url)

        if album:
            for url in _findall_full(post_html, album):
                add(name, category, "album", url)

    if include_direct:
        for url in re.findall(DIRECT_LINK_PATTERN, post_html, re.IGNORECASE):
            add("direct", "", "single", url)

    return matches


def _findall_full(text: str, fragment: str) -> list[str]:
    """Return whole-URL matches (findall with groups returns tuples otherwise)."""
    pattern = _build_pattern(fragment)
    return [match.group(0) for match in pattern.finditer(text)]


def _extract_full(text: str, fragment: str) -> str:
    """First whole match or empty string."""
    pattern = _build_pattern(fragment)
    match = pattern.search(text)
    return match.group(0) if match else ""


def album_kind(url: str) -> bool:
    """Whether a URL looks like an album/folder page rather than a single file."""
    return bool(re.search(r"/(a|album|g|gallery|d|folder|f)/", url, re.IGNORECASE))


def group_by_category(matches: list[HostMatch]) -> dict[str, list[HostMatch]]:
    """Group matches by their display category."""
    grouped: dict[str, list[HostMatch]] = {}
    for match in matches:
        grouped.setdefault(match.category, []).append(match)
    return grouped
