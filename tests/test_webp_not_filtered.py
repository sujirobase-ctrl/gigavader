"""Regression guard: .webp and .avif must be excluded (user wants only
traditional raster formats: jpg/jpeg/png/gif/bmp and friends, never webp).

If a future edit re-adds webp to the accepted extensions or direct-link
pattern, this test fails.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import re

from src.config import CONTENT_TYPE_EXTENSIONS, DIRECT_LINK_PATTERN, IMAGE_EXTENSIONS
from src.parsers.host_parser import parse_hosts


def test_webp_not_in_extension_lists():
    assert ".webp" not in IMAGE_EXTENSIONS
    assert ".avif" not in IMAGE_EXTENSIONS
    assert "image/webp" not in CONTENT_TYPE_EXTENSIONS


def test_webp_not_in_direct_link_pattern():
    url = "https://host.cr/images/abc123.md.webp"
    assert not re.search(DIRECT_LINK_PATTERN, url, re.IGNORECASE)
    url_avif = "https://host.cr/images/x.avif"
    assert not re.search(DIRECT_LINK_PATTERN, url_avif, re.IGNORECASE)


def test_parse_hosts_filters_webp():
    html = (
        '<img src="https://a.cr/x/y/photo.md.webp">'
        '<img src="https://b.cr/z/sweet.avif">'
        '<img src="https://c.cr/w/keep.jpg">'
        '<img src="https://d.cr/v/keep2.png">'
    )
    matches = parse_hosts(html)
    urls = [m.url for m in matches]
    assert not any(u.endswith(".webp") for u in urls), "webp was NOT filtered!"
    assert not any(u.endswith(".avif") for u in urls), "avif was NOT filtered!"
    # traditional formats still pass
    assert any(u.endswith(".jpg") for u in urls)
    assert any(u.endswith(".png") for u in urls)


def test_jpg_png_jpeg_still_pass():
    html = '<img src="https://h.cr/a/b/c.jpeg"><img src="https://h.cr/a/b/d.png">'
    matches = parse_hosts(html)
    assert len(matches) == 2


if __name__ == "__main__":
    test_webp_not_in_extension_lists()
    test_webp_not_in_direct_link_pattern()
    test_parse_hosts_filters_webp()
    test_jpg_png_jpeg_still_pass()
    print("webp-excluded tests — passed 4, failed 0")
