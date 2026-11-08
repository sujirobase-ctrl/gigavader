"""Batch entry point: read thread URLs from URLs.txt and download each one.

Usage:
    python main.py [options]
"""

from __future__ import annotations

import sys
from pathlib import Path

from downloader import download_url, parse_arguments, setup_cookies
from src.config import SESSION_LOG, URLS_FILE
from src.file_utils import read_urls_file
from src.managers import initialize_managers


def log_session_start(log_file: str = SESSION_LOG) -> None:
    """Append a session marker to the log file."""
    try:
        with Path(log_file).open("a", encoding="utf-8") as handle:
            handle.write(f"\n{'=' * 60}\nNew session\n{'=' * 60}\n")
    except OSError:
        pass


def log_failed_urls(urls: list[str]) -> None:
    """Report URLs that ended with at least one failed file."""
    print("\nURLs with failures:")
    for url in urls:
        print(f"  - {url}")


def main() -> None:
    """Process every URL listed in URLs.txt."""
    args = parse_arguments(common_only=True)
    log_file = getattr(args, "log_file", None) or SESSION_LOG
    log_session_start(log_file)

    urls = read_urls_file(URLS_FILE)
    if not urls:
        print(f"No URLs found. Create '{URLS_FILE}' with one thread URL per line.")
        sys.exit(1)

    # Prompt for cookies once, before the live UI takes over the screen.
    args._resolved_cookies = setup_cookies(args)  # noqa: SLF001
    args._cookies_resolved = True  # noqa: SLF001

    live_manager = initialize_managers(
        disable_ui=args.disable_ui,
        log_file=log_file,
        verbose=getattr(args, "verbose", False),
    )
    failed_urls: list[str] = []

    try:
        live_manager.start()
        for url in urls:
            live_manager.update_log(event="Processing URL", details=url)
            if download_url(url, args, live_manager=live_manager):
                failed_urls.append(url)
    finally:
        live_manager.stop()

    if failed_urls:
        log_failed_urls(failed_urls)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
