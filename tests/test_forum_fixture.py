"""Synthetic XenForo HTML covering decorative markup and media host links."""
from __future__ import annotations

import os
import sys
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from src.parsers.thread_parser import ThreadParser
from src.parsers.host_parser import parse_hosts, _is_decorative

BASE = "https://simpcity.cr/threads/qa-example.10001/"

PAGE1_HTML = """
<html><body>
<h1 class="p-title-value">
  <span class="label label--requests">Request</span><span class="label-append">&nbsp;</span>
  <span class="label label--patreon">Patreon</span><span class="label-append">&nbsp;</span>
  <span class="label label--twitch">Twitch</span><span class="label-append">&nbsp;</span>Example thread
</h1>
<article class="message--post" data-content="post-10011" id="js-post-10011">
  <a href="https://simpcity.cr/threads/qa-example.10001/post-10011">#1</a>
  <div class="message-userContent lbContainer js-lbContainer">
    <div class="bbWrapper">Example forum post
      <a href="https://goonbox.cr/img/fixtureGbx" class="link">
        <img src="https://simp4.cuckcapital.cr/00000000-0000-4000-8000-000000000001-profile_banner-480.md.png"
             class="bbImage" alt="profile_banner">
      </a>
      <img class="smilie smilie--sprite" alt=":COPIUM:" src="data:image/gif;base64,AAAA">
    </div>
  </div>
</article>

<article class="message--post" data-content="post-10012" id="js-post-10012">
  <a href="https://simpcity.cr/threads/qa-example.10001/post-10012">#2</a>
  <div class="message-userContent lbContainer js-lbContainer">
    <div class="bbWrapper">Some stuff
      <a href="https://bunkr.cr/a/fixtureAlbum" class="link">https://bunkr.cr/a/fixtureAlbum</a>
    </div>
  </div>
</article>

<article class="message--post" data-content="post-10019" id="js-post-10019">
  <a href="https://simpcity.cr/threads/qa-example.10001/post-10019">#9</a>
  <div class="message-userContent lbContainer js-lbContainer">
    <div class="bbWrapper">Example media link
      <a href="https://bunkr.ph/f/fixtureFile" class="link">https://bunkr.ph/f/fixtureFile</a>
    </div>
    <aside class="message-signature"><div class="bbWrapper">support the creators</div></aside>
  </div>
</article>

<article class="message--post" data-content="post-10020" id="js-post-10020">
  <a href="https://simpcity.cr/threads/qa-example.10001/post-10020">#10</a>
  <div class="message-userContent lbContainer js-lbContainer">
    <div class="bbWrapper">VODs
      <div class="bbCodeBlock bbCodeBlock--unfurl js-unfurl">
        <a href="https://filester.me/f/0000000000000001" class="link">Example videos</a>
      </div>
    </div>
  </div>
</article>

<article class="message--post" data-content="post-10021" id="js-post-10021">
  <a href="https://simpcity.cr/threads/qa-example.10001/post-10021">#11</a>
  <div class="message-userContent lbContainer js-lbContainer">
    <div class="bbWrapper">patreon audios
      <div class="bbCodeBlock bbCodeBlock--unfurl js-unfurl">
        <a href="https://filester.me/f/0000000000000002" class="link">audios</a>
        <a href="https://filester.me/f/0000000000000003" class="link">clips</a>
      </div>
      <img class="smilie smilie--emoji" alt=":star:" src="x.png">
    </div>
  </div>
</article>
</body></html>
"""


def test_title_and_post_count():
    soup = BeautifulSoup(PAGE1_HTML, "html.parser")
    parser = ThreadParser(soup, BASE)
    assert parser.parse_title() == "Request Patreon Twitch Example thread"
    posts = parser.parse_posts()
    assert len(posts) == 5
    # post numbers parsed from the #N counter
    assert [p.post_number for p in posts] == ["1", "2", "9", "10", "11"]


def test_decorative_profile_banner_excluded():
    assert _is_decorative(
        "https://simp4.cuckcapital.cr/00000000-0000-4000-8000-000000000001-profile_banner-480.md.png"
    )
    soup = BeautifulSoup(PAGE1_HTML, "html.parser")
    parser = ThreadParser(soup, BASE)
    posts = parser.parse_posts()
    post1 = posts[0]
    matches = parse_hosts(post1.html_content)
    assert all("profile_banner" not in m.url for m in matches)
    # goonbox.cr IS now a supported host (added to HOSTS), so it should be detected
    assert any("goonbox.cr" in m.url for m in matches), "goonbox.cr should now be detected"


def test_emoji_signature_quote_excluded():
    soup = BeautifulSoup(PAGE1_HTML, "html.parser")
    parser = ThreadParser(soup, BASE)
    posts = parser.parse_posts()
    # post 9 already has a signature; ensure it contributes no media on its own
    # beyond the bunkr link it actually contains.
    post9 = posts[2]
    matches = parse_hosts(post9.html_content)
    assert any(m.url == "https://bunkr.ph/f/fixtureFile" for m in matches)
    # no smilie/emoji data-URIs leak as media
    assert all("data:image" not in m.url for m in matches)


def test_user_posted_folders_captured():
    soup = BeautifulSoup(PAGE1_HTML, "html.parser")
    parser = ThreadParser(soup, BASE)
    posts = {p.post_number: p for p in parser.parse_posts()}

    # post 2: bunkr album
    urls2 = {m.url for m in parse_hosts(posts["2"].html_content)}
    assert "https://bunkr.cr/a/fixtureAlbum" in urls2

    # post 9: bunkr /f/ folder
    urls9 = {m.url for m in parse_hosts(posts["9"].html_content)}
    assert "https://bunkr.ph/f/fixtureFile" in urls9

    # posts 10 & 11: filester.me folders
    urls10 = {m.url for m in parse_hosts(posts["10"].html_content)}
    assert "https://filester.me/f/0000000000000001" in urls10

    urls11 = {m.url for m in parse_hosts(posts["11"].html_content)}
    assert "https://filester.me/f/0000000000000002" in urls11
    assert "https://filester.me/f/0000000000000003" in urls11


def test_total_capture_count():
    soup = BeautifulSoup(PAGE1_HTML, "html.parser")
    parser = ThreadParser(soup, BASE)
    total = 0
    for p in parser.parse_posts():
        total += len(parse_hosts(p.html_content))
    # 2 (bunkr) + 3 (filester) + 1 (goonbox) = 6 posted-media links across 5 posts
    assert total == 6


if __name__ == "__main__":
    test_title_and_post_count()
    test_decorative_profile_banner_excluded()
    test_emoji_signature_quote_excluded()
    test_user_posted_folders_captured()
    test_total_capture_count()
    print("forum fixture tests — passed 5, failed 0")
