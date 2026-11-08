"""End-to-end tests against the local mock forum.

Run with:  python -m tests.test_e2e
"""

from __future__ import annotations

import shutil
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from downloader import download_url  # noqa: E402
from src.config import AUDIO_EXTENSIONS, TaskResult, VIDEO_EXTENSIONS  # noqa: E402
from src.file_utils import sanitize_name  # noqa: E402
from src.http_client import HttpClient  # noqa: E402
from src.managers import initialize_managers  # noqa: E402
from src.parsers import ThreadParser, parse_hosts  # noqa: E402
from tests.mock_forum import BASE, serve  # noqa: E402

THREAD_URL = f"{BASE}/threads/test-thread.123/"
EXPECTED_SIZES = {
    "photo1.jpg": 20_004,
    "photo2.png": 35_008,
    "clip.mp4": 120_012,
    "track.mp3": 5_003,
    "album1.jpg": 8_004,
    "album2.jpg": 9_004,
}

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Tiny assertion helper."""
    global PASSED, FAILED  # noqa: PLW0603
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name} {detail}")


def base_args(**overrides) -> Namespace:  # noqa: ANN003
    """Default CLI namespace for programmatic runs."""
    args = Namespace(
        custom_path=None,
        cookies=None,
        no_cookies=True,
        all_pages=True,
        max_pages=1,
        post_ids=[],
        ignore=[],
        include=[],
        workers=3,
        max_retries=1,
        delay=2.0,
        connections=1,
        rate_limit=None,
        separate_posts=False,
        flat=False,
        dry_run=False,
        disable_ui=True,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_sanitize() -> None:
    """Filename sanitising."""
    print("\n[sanitize_name]")
    check("strips emoji", sanitize_name("Hot \U0001f525 Pack") == "Hot Pack")
    check("replaces invalid chars", "/" not in sanitize_name("a/b:c*d"))
    check("empty -> untitled", sanitize_name("") == "untitled")
    check("collapses spaces", sanitize_name("a    b") == "a b")


def test_parser() -> None:
    """Thread parsing."""
    print("\n[ThreadParser]")
    client = HttpClient()
    parser = ThreadParser(client.get_soup(THREAD_URL), THREAD_URL)

    check("title parsed", parser.parse_title() == "My Test Thread Pack",
          f"got {parser.parse_title()!r}")
    check("next page found", parser.next_page_url() is not None)

    posts = parser.parse_posts()
    check("two posts on page 1", len(posts) == 2, f"got {len(posts)}")
    check("post id parsed", posts[0].post_id == "1001", f"got {posts[0].post_id}")
    check("post number parsed", posts[0].post_number == "1", f"got {posts[0].post_number}")
    check("spoiler password extracted", "hunter2" in posts[0].spoilers,
          f"got {posts[0].spoilers}")

    urls = [m.url for m in parse_hosts(posts[0].html_content)]
    check("finds 4 media links", len(urls) == 4, f"got {urls}")

    quoted = [m.url for m in parse_hosts(posts[1].html_content)]
    check("ignores quoted blockquote link",
          not any("should-not-download" in u for u in quoted), f"got {quoted}")
    client.close()


def test_download(tmp: Path) -> None:
    """Full download run."""
    print("\n[download]")
    shutil.rmtree(tmp, ignore_errors=True)
    failed = download_url(THREAD_URL, base_args(custom_path=str(tmp)))
    check("run reports no failure", failed is False)

    folder = tmp / "Downloads" / "My Test Thread Pack"
    check("thread folder created", folder.is_dir(), str(folder))

    for name, size in EXPECTED_SIZES.items():
        _ext = "." + name.rsplit(".", 1)[-1].lower()
        if _ext in AUDIO_EXTENSIONS:
            category = "Audio"
        elif _ext in VIDEO_EXTENSIONS:
            category = "Videos"
        else:
            category = "Photos"
        path = folder / category / name
        check(f"{name} exists in {category}/", path.is_file())
        if path.is_file():
            check(f"{name} size == {size}", path.stat().st_size == size,
                  f"got {path.stat().st_size}")


def test_resume(tmp: Path) -> None:
    """Second run skips everything."""
    print("\n[resume]")
    manager = initialize_managers(disable_ui=True)
    manager.start()
    download_url(THREAD_URL, base_args(custom_path=str(tmp)), live_manager=manager)
    manager.stop()

    completed = manager.summary_manager.get_result_count(TaskResult.COMPLETED)
    skipped = manager.summary_manager.get_result_count(TaskResult.SKIPPED)
    check("nothing re-downloaded", completed == 0, f"completed={completed}")
    check("all 6 skipped", skipped == 6, f"skipped={skipped}")


def test_filters(tmp: Path) -> None:
    """--include / --ignore / --separate-posts."""
    print("\n[filters]")
    include_dir = tmp / "inc"
    shutil.rmtree(include_dir, ignore_errors=True)
    download_url(THREAD_URL, base_args(custom_path=str(include_dir), include=[".jpg"]))
    files = {p.name for p in (include_dir / "Downloads").rglob("*") if p.is_file()
             and not p.name.startswith(".")}
    check("--include .jpg keeps only jpgs", files == {"photo1.jpg", "album1.jpg", "album2.jpg"},
          f"got {files}")

    sep_dir = tmp / "sep"
    shutil.rmtree(sep_dir, ignore_errors=True)
    download_url(THREAD_URL, base_args(custom_path=str(sep_dir), separate_posts=True,
                                       ignore=[".mp4"]))
    dirs = {p.name for p in (sep_dir / "Downloads" / "My Test Thread Pack").iterdir()
            if p.is_dir()}
    check("--separate-posts creates post folders", dirs == {"post-1", "post-3"}, f"got {dirs}")
    mp4s = list((sep_dir / "Downloads").rglob("*.mp4"))
    check("--ignore .mp4 excluded the video", not mp4s, f"got {mp4s}")


def test_dry_run(tmp: Path) -> None:
    """--dry-run writes nothing."""
    print("\n[dry-run]")
    dry_dir = tmp / "dry"
    shutil.rmtree(dry_dir, ignore_errors=True)
    download_url(THREAD_URL, base_args(custom_path=str(dry_dir), dry_run=True))
    written = list(dry_dir.rglob("*")) if dry_dir.exists() else []
    check("no files written", not [p for p in written if p.is_file()], f"got {written}")


def main() -> int:
    """Run the suite."""
    server = serve()
    tmp = Path(__file__).resolve().parent / "_tmp"
    try:
        test_sanitize()
        test_parser()
        test_download(tmp)
        test_resume(tmp)
        test_filters(tmp)
        test_dry_run(tmp)
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
        for leftover in ("session.log",):
            Path(leftover).unlink(missing_ok=True)

    print(f"\n{'=' * 40}\nPassed: {PASSED}   Failed: {FAILED}\n{'=' * 40}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
