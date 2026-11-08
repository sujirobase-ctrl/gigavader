"""Download the media of a single forum thread or post URL.

Usage:
    python downloader.py <thread_url> [options]
"""

from __future__ import annotations

import argparse
import sys

from src.config import INTER_FILE_DELAY, MAX_RETRIES, MAX_WORKERS, SESSION_LOG
from src.cookie_setup import forget_path, resolve_cookies
from src.downloaders import ThreadDownloader, ThreadOptions
from src.http_client import HttpClient
from src.managers import initialize_managers


def parse_arguments(common_only: bool = False) -> argparse.Namespace:
    """Build the CLI. `common_only` drops the positional URL (used by main.py)."""
    parser = argparse.ArgumentParser(
        description="Download media linked in forum posts (XenForo threads).",
    )

    if not common_only:
        parser.add_argument("url", help="Thread or post URL to download from.")

    parser.add_argument(
        "--custom-path",
        type=str,
        default=None,
        help="Base directory; files land in <custom_path>/Downloads.",
    )
    parser.add_argument(
        "--cookies",
        type=str,
        default=None,
        help="Netscape cookies.txt for login-gated forums.",
    )
    parser.add_argument(
        "--no-cookies",
        action="store_true",
        help="Skip the cookie prompt and run anonymously.",
    )
    parser.add_argument(
        "--reset-cookies",
        action="store_true",
        help="Forget the saved cookies path and ask again.",
    )
    parser.add_argument(
        "--all-pages",
        action="store_true",
        help="Follow pagination to the end of the thread.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Number of pages to walk when --all-pages is not set (default: 1).",
    )
    parser.add_argument(
        "--post",
        dest="post_ids",
        action="append",
        default=[],
        help="Only download this post id/number (repeatable).",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Skip files whose name contains this substring (repeatable).",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Only download files whose name contains this substring (repeatable).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"Concurrent file downloads (default: {MAX_WORKERS}).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=MAX_RETRIES,
        help=f"Retries per file (default: {MAX_RETRIES}).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=INTER_FILE_DELAY,
        help=(
            "Seconds to wait before starting each file. CDNs like filester/"
            "bunkr rate-limit concurrent or rapid requests; a small delay "
            f"(default: {INTER_FILE_DELAY}s) keeps the run under the limit."
        ),
    )
    parser.add_argument(
        "--connections",
        type=int,
        default=1,
        help=(
            "Parallel byte-range chunks per file (default: 1 = single stream). "
            "Needs hosts that support HTTP Range; large files download faster "
            "and resume from the failed chunk instead of restarting."
        ),
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=None,
        help=(
            "Aggregate bandwidth cap in MB/s shared across all downloads "
            "(default: unlimited). Helps avoid CDN 429 rate-limiting."
        ),
    )
    parser.add_argument(
        "--separate-posts",
        action="store_true",
        help="Create one subfolder per post.",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Ignore album subfolders; put everything in the thread folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and list files without downloading.",
    )
    parser.add_argument(
        "--disable-ui",
        action="store_true",
        help="Plain log output instead of the live progress UI.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=SESSION_LOG,
        help=f"Where to append the run log (default: {SESSION_LOG}).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Also log every resolved link, queued file and filter decision.",
    )
    parser.add_argument(
        "--clean-part",
        action="store_true",
        help=(
            "Delete leftover .part temp files at the end of the run. These are "
            "incomplete downloads interrupted by a network error; without this "
            "flag they are kept (and resumed on the next run) but not reported "
            "as files."
        ),
    )

    return parser.parse_args()


def options_from_args(args: argparse.Namespace) -> ThreadOptions:
    """Translate parsed args into ThreadOptions."""
    return ThreadOptions(
        custom_path=args.custom_path,
        max_pages=args.max_pages,
        all_pages=args.all_pages,
        post_ids=args.post_ids,
        ignore=args.ignore,
        include=args.include,
        workers=args.workers,
        max_retries=args.max_retries,
        delay=args.delay,
        connections=max(1, args.connections),
        rate_limit=(args.rate_limit * 1024 * 1024) if args.rate_limit else None,
        dry_run=args.dry_run,
        separate_posts=args.separate_posts,
        flat=args.flat,
        clean_part=getattr(args, "clean_part", False),
    )


def setup_cookies(args: argparse.Namespace) -> str | None:
    """Resolve the cookies file, prompting the user when needed.

    Called once before any download starts, so a login-gated forum fails at
    the prompt rather than halfway through a run.
    """
    if getattr(args, "reset_cookies", False):
        forget_path()

    if getattr(args, "no_cookies", False):
        return None

    return resolve_cookies(args.cookies, ask=True)


def download_url(url: str, args: argparse.Namespace, live_manager=None) -> bool:  # noqa: ANN001
    """Download one thread URL. Returns True on failure."""
    owns_manager = live_manager is None
    manager = live_manager or initialize_managers(
        disable_ui=args.disable_ui,
        log_file=getattr(args, "log_file", None) or SESSION_LOG,
        verbose=getattr(args, "verbose", False),
    )

    cookies_file = getattr(args, "_resolved_cookies", None)
    if cookies_file is None and not getattr(args, "_cookies_resolved", False):
        cookies_file = setup_cookies(args)

    client = HttpClient(cookies_file=cookies_file)
    downloader = ThreadDownloader(client, manager, options_from_args(args))

    try:
        if owns_manager:
            manager.start()
        return downloader.download_thread(url)
    finally:
        client.close()
        if owns_manager:
            manager.stop()


def main() -> None:
    """Entry point for single-URL downloads."""
    args = parse_arguments()

    # Resolve cookies up front (may prompt), before the live UI takes the screen.
    args._resolved_cookies = setup_cookies(args)  # noqa: SLF001
    args._cookies_resolved = True  # noqa: SLF001

    failed = download_url(args.url, args)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
