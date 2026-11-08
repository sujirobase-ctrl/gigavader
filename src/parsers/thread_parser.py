"""XenForo thread/post parsing — Python port of ForumPostDownloader's parsers.js."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from src.file_utils import sanitize_name

SPOILER_PASSWORD_PATTERN = re.compile(
    r"(?:pw|pass|passwd|password|key)\s*:?\s+([^\s<]{2,64})",
    re.IGNORECASE,
)

# Elements stripped from post content before host matching (see parsers.js).
# Avatars, emoji/smilies and reactions are forum chrome, never something the
# user posted as content, so they must never be offered for download.
NOISE_SELECTORS = (
    ".contentRow-figure",
    ".js-unfurl-favicon",
    ".button-text > span",
    "script",
    "style",
    "[class*=avatar]",
    "[class*=smilie]",
    "[class*=reaction]",
    ".message-signature",
    ".bbCodeBlock--quote",
)


@dataclass
class ParsedPost:
    """One forum post with its raw HTML content and extracted metadata."""

    post_id: str
    post_number: str
    page_number: int
    text_content: str
    html_content: str
    spoilers: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Short human label used in folder names and the progress bar."""
        return f"post-{self.post_number or self.post_id}"


class ThreadParser:
    """Parse a XenForo thread page into posts."""

    def __init__(self, soup: BeautifulSoup, url: str, *, allow_emojis: bool = False) -> None:
        self.soup = soup
        self.url = url
        self.allow_emojis = allow_emojis

    def parse_title(self) -> str:
        """Extract the thread title, stripping tags and emojis."""
        node = self.soup.select_one(".p-title-value")
        if node is None:
            node = self.soup.select_one("title")
        raw = node.get_text(" ", strip=True) if node else "thread"
        return sanitize_name(raw, allow_emojis=self.allow_emojis)

    def page_number(self) -> int:
        """Current page number, parsed from the URL."""
        match = re.search(r"/page-(\d+)", self.url)
        return int(match.group(1)) if match else 1

    def next_page_url(self) -> str | None:
        """Absolute URL of the next page, if the pagination shows one."""
        node = self.soup.select_one("a.pageNav-jump--next")
        if node is None or not node.get("href"):
            return None
        return urljoin(self.url, node["href"])

    def parse_posts(self) -> list[ParsedPost]:
        """Return every post article present on the page."""
        articles = self.soup.select("article.message, .message--post")
        posts: list[ParsedPost] = []
        page = self.page_number()

        for article in articles:
            parsed = self._parse_article(article, page)
            if parsed is not None:
                posts.append(parsed)

        return posts

    # Private helpers
    def _parse_article(self, article: Tag, page: int) -> ParsedPost | None:
        body = article.select_one(".message-body, .message-userContent")
        if body is None:
            return None

        post_id, post_number = self._parse_identity(article)
        clone = BeautifulSoup(str(body), "html.parser")
        self._strip_noise(clone)
        spoilers = self._parse_spoilers(clone)

        return ParsedPost(
            post_id=post_id,
            post_number=post_number,
            page_number=page,
            text_content=clone.get_text("\n", strip=True),
            html_content=str(clone),
            spoilers=spoilers,
        )

    @staticmethod
    def _parse_identity(article: Tag) -> tuple[str, str]:
        # XenForo renders several /post-N anchors per message (date permalink,
        # bare anchor, then the "#N" counter). Prefer the one whose text is the
        # counter; fall back to the first anchor for the id.
        anchors = article.select('a[href*="/post-"]')
        counter = next(
            (a for a in anchors if re.fullmatch(r"#\s*[\d,]+", a.get_text(strip=True))),
            None,
        )

        post_id = ""
        for anchor in (counter, *anchors):
            if anchor is None:
                continue
            match = re.search(r"/post-(\w+)", anchor.get("href", ""))
            if match:
                post_id = match.group(1)
                break

        if not post_id:
            raw = article.get("data-content") or article.get("id") or ""
            match = re.search(r"(\d+)", raw)
            post_id = match.group(1) if match else "0"

        post_number = (
            counter.get_text(strip=True).lstrip("#").replace(",", "").strip()
            if counter
            else ""
        )

        return post_id, post_number or post_id

    @staticmethod
    def _strip_noise(clone: BeautifulSoup) -> None:
        for selector in NOISE_SELECTORS:
            for node in clone.select(selector):
                node.decompose()

        # Only drop blockquotes that quote another post.
        for quote in clone.select("blockquote"):
            if quote.select_one(".bbCodeBlock-title"):
                quote.decompose()

    @staticmethod
    def _parse_spoilers(clone: BeautifulSoup) -> list[str]:
        found: list[str] = []

        selectors = (
            ".bbCodeBlock--spoiler > .bbCodeBlock-content",
            ".bbCodeInlineSpoiler",
        )
        for selector in selectors:
            for node in clone.select(selector):
                if node.select_one(".bbCodeBlock--unfurl"):
                    continue
                text = node.get_text(" ", strip=True)
                if text:
                    found.append(text)

        for match in SPOILER_PASSWORD_PATTERN.finditer(clone.get_text("\n", strip=True)):
            found.append(match.group(1).strip())

        cleaned: list[str] = []
        for item in found:
            value = re.sub(
                r"^\s*(?:p|pw|pass|passwd|password|key)\s*:?\s*",
                "",
                item.strip(),
                flags=re.IGNORECASE,
            )
            value = value.strip(" :")
            if value and value not in cleaned:
                cleaned.append(value)

        return cleaned
