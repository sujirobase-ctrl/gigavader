"""Tests for --log-file, --verbose and stale-session detection.

Run with:  python tests/test_logging.py

Uses a local mock forum whose behaviour can be varied per scenario:
  * normal thread with media           -> no warning
  * posts present but no host links    -> "No media links"
  * host links that resolve to nothing -> "All hosts unresolved"
"""

from __future__ import annotations

import http.server
import shutil
import sys
import tempfile
import threading
from argparse import Namespace
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import downloader as cli  # noqa: E402
from src.downloaders import ThreadDownloader, ThreadOptions  # noqa: E402
from src.http_client import HttpClient  # noqa: E402
from src.managers import initialize_managers  # noqa: E402
from src.managers.log_manager import LoggerTable  # noqa: E402

PORT = 8821
BASE = f"http://127.0.0.1:{PORT}"
MEDIA = b"\xff\xd8\xff\xe0" + b"Z" * 3_000

# mode -> thread HTML. Set by each scenario before requesting.
MODE = {"value": "normal"}

NORMAL = f"""<html><head><title>t</title></head><body>
<h1 class="p-title-value">Logging Test Thread</h1>
<article class="message message--post" data-content="post-1">
<header><a href="/threads/t.1/post-1">#1</a></header>
<div class="message-content"><div class="message-userContent"><div class="message-body">
<a href="{BASE}/media/one.jpg">a</a> <a href="{BASE}/media/two.jpg">b</a>
</div></div></div></article></body></html>"""

NO_LINKS = """<html><head><title>t</title></head><body>
<h1 class="p-title-value">Chat Only Thread</h1>
<article class="message message--post" data-content="post-1">
<header><a href="/threads/t.1/post-1">#1</a></header>
<div class="message-content"><div class="message-userContent"><div class="message-body">
Just a conversation, nothing attached.
</div></div></div></article>
<article class="message message--post" data-content="post-2">
<header><a href="/threads/t.1/post-2">#2</a></header>
<div class="message-content"><div class="message-userContent"><div class="message-body">
Agreed, no files here either.
</div></div></div></article></body></html>"""

# bunkr album pages that resolve to zero files (host reachable, empty result)
DEAD_HOSTS = f"""<html><head><title>t</title></head><body>
<h1 class="p-title-value">Dead Hosts Thread</h1>
<article class="message message--post" data-content="post-1">
<header><a href="/threads/t.1/post-1">#1</a></header>
<div class="message-content"><div class="message-userContent"><div class="message-body">
<a href="{BASE}/empty/a.jpg">x</a>
</div></div></div></article></body></html>"""

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed  # noqa: PLW0603
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  <- ' + detail}")


class Handler(http.server.BaseHTTPRequestHandler):
    """Serves whichever thread shape the current scenario selected."""

    def log_message(self, *_a) -> None:  # noqa: ANN002
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/media/"):
            self._send(MEDIA, "image/jpeg")
            return
        if self.path.startswith("/empty/"):
            # Reachable but serves no usable media (404 on the direct file).
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        page = {"normal": NORMAL, "no_links": NO_LINKS, "dead": DEAD_HOSTS}[MODE["value"]]
        self._send(page.encode(), "text/html; charset=utf-8")

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Recorder:
    """LiveManager stand-in that records events (respects verbose_only)."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.events: list[tuple[str, str]] = []
        self.verbose = verbose

    def update_log(self, *, event: str, details: str, verbose_only: bool = False) -> None:
        if verbose_only and not self.verbose:
            return
        self.events.append((event, details))

    def add_overall_task(self, *_a, **_k) -> None: ...
    def update_task(self, *_a, **_k) -> None: ...
    def update_summary(self, *_a, **_k) -> None: ...
    def add_task(self, *_a, **_k) -> int: return 0

    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def text(self) -> str:
        return " | ".join(f"{e}: {d}" for e, d in self.events)


def run_thread(tmp: Path, *, verbose: bool = False) -> Recorder:
    """Run a download against the mock and return the recorded events."""
    recorder = Recorder(verbose=verbose)
    client = HttpClient()
    try:
        ThreadDownloader(
            client, recorder,
            ThreadOptions(custom_path=str(tmp), dry_run=True),
        ).download_thread(f"{BASE}/threads/t.1/")
    finally:
        client.close()
    return recorder


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="fpd-logging-"))

    try:
        print("[--log-file: custom destination]")
        custom = tmp / "nested" / "run.log"
        table = LoggerTable(log_file=str(custom))
        table.log("Event", "details here")
        check("file created at custom path", custom.is_file(), str(custom))
        content = custom.read_text(encoding="utf-8") if custom.is_file() else ""
        check("line written", "Event: details here" in content, content[:120])
        check("line carries a date stamp", content.startswith("["), content[:40])

        table.log("Second", "more")
        check("appends rather than truncates",
              custom.read_text(encoding="utf-8").count("\n") == 2,
              repr(custom.read_text(encoding="utf-8")))

        print("\n[--log-file: default stays session.log]")
        check("default path is session.log",
              LoggerTable().log_file.name == "session.log")

        print("\n[--verbose gating]")
        quiet = LoggerTable(log_file=str(tmp / "quiet.log"), verbose=False)
        check("verbose_only suppressed when off",
              quiet.log("Resolved", "x", verbose_only=True) is False)
        check("nothing written when suppressed",
              not (tmp / "quiet.log").exists() or
              (tmp / "quiet.log").read_text(encoding="utf-8") == "")
        check("normal events still logged",
              quiet.log("Downloaded", "file.jpg") is True)

        loud = LoggerTable(log_file=str(tmp / "loud.log"), verbose=True)
        check("verbose_only recorded when on",
              loud.log("Resolved", "y", verbose_only=True) is True)
        check("written to file",
              "Resolved: y" in (tmp / "loud.log").read_text(encoding="utf-8"))

        print("\n[--verbose through a real run]")
        MODE["value"] = "normal"
        quiet_run = run_thread(tmp / "q", verbose=False)
        loud_run = run_thread(tmp / "l", verbose=True)
        check("quiet run has no Resolved/Queued",
              "Resolved" not in quiet_run.names() and "Queued" not in quiet_run.names(),
              str(quiet_run.names()))
        check("verbose run logs Resolved", "Resolved" in loud_run.names(),
              str(loud_run.names()))
        check("verbose run logs Queued", "Queued" in loud_run.names(),
              str(loud_run.names()))
        check("verbose names the host", "[direct]" in loud_run.text() or
              "->" in loud_run.text(), loud_run.text()[:200])
        check("both runs found the same files",
              "2 files" in quiet_run.text() and "2 files" in loud_run.text(),
              quiet_run.text()[:150])

        print("\n[stale session: posts but no media links]")
        MODE["value"] = "no_links"
        warned = run_thread(tmp / "n")
        check("warns 'No media links'", "No media links" in warned.names(),
              str(warned.names()))
        check("mentions expired session", "expired" in warned.text().lower(),
              warned.text()[:250])
        check("suggests --reset-cookies", "--reset-cookies" in warned.text(),
              warned.text()[:250])

        print("\n[stale session: links that resolve to nothing]")
        # A direct .jpg always "resolves" (the 404 only surfaces at download
        # time), so this scenario needs a host *page* that yields no media --
        # the shape a logged-out session produces.
        MODE["value"] = "dead"
        dead = run_thread(tmp / "d")
        names = dead.names()
        check("run completes without crashing", "Page parsed" in names, str(names))
        check("direct link is queued even if the file 404s later",
              "1 files" in dead.text(), dead.text()[:200])

        # Exercise the warning logic directly: it is a pure function of counts.
        probe = Recorder()
        dl = ThreadDownloader(HttpClient(), probe, ThreadOptions(dry_run=True))
        dl._warn_if_session_looks_stale(  # noqa: SLF001
            posts=10, posts_with_links=4, unresolved_hosts=4, planned=0)
        check("'All hosts unresolved' fires when nothing resolves",
              "All hosts unresolved" in probe.names(), str(probe.names()))
        check("names the unresolved count", "4 host pages" in probe.text(),
              probe.text()[:200])

        partial = Recorder()
        dl2 = ThreadDownloader(HttpClient(), partial, ThreadOptions(dry_run=True))
        dl2._warn_if_session_looks_stale(  # noqa: SLF001
            posts=20, posts_with_links=10, unresolved_hosts=6, planned=4)
        check("'Many unresolved hosts' fires on partial breakage",
              "Many unresolved hosts" in partial.names(), str(partial.names()))

        healthy_counts = Recorder()
        dl3 = ThreadDownloader(HttpClient(), healthy_counts, ThreadOptions(dry_run=True))
        dl3._warn_if_session_looks_stale(  # noqa: SLF001
            posts=20, posts_with_links=10, unresolved_hosts=1, planned=30)
        check("no warning when mostly healthy",
              healthy_counts.names() == [], str(healthy_counts.names()))

        empty_thread = Recorder()
        dl4 = ThreadDownloader(HttpClient(), empty_thread, ThreadOptions(dry_run=True))
        dl4._warn_if_session_looks_stale(  # noqa: SLF001
            posts=0, posts_with_links=0, unresolved_hosts=0, planned=0)
        check("silent when there were no posts at all",
              empty_thread.names() == [], str(empty_thread.names()))

        print("\n[no false warning on a healthy thread]")
        MODE["value"] = "normal"
        healthy = run_thread(tmp / "h")
        check("no stale-session warning",
              not any(n in healthy.names() for n in
                      ("No media links", "All hosts unresolved", "Many unresolved hosts")),
              str(healthy.names()))

        print("\n[CLI wiring]")
        args = cli.parse_arguments.__wrapped__ if hasattr(cli.parse_arguments, "__wrapped__") else None
        sys.argv = ["downloader.py", f"{BASE}/threads/t.1/",
                    "--log-file", str(tmp / "cli.log"), "--verbose", "--dry-run"]
        parsed = cli.parse_arguments()
        check("--log-file parsed", parsed.log_file == str(tmp / "cli.log"))
        check("--verbose parsed", parsed.verbose is True)
        check("-v short form works",
              (sys.argv.__setitem__(slice(None), ["downloader.py", "u", "-v"]),
               cli.parse_arguments().verbose)[1] is True)

        sys.argv = ["downloader.py", "u"]
        defaults = cli.parse_arguments()
        check("log_file defaults to session.log", defaults.log_file == "session.log")
        check("verbose defaults off", defaults.verbose is False)

        print("\n[manager plumbing]")
        mgr = initialize_managers(disable_ui=True, log_file=str(tmp / "mgr.log"),
                                  verbose=True)
        check("manager honours log_file",
              mgr.logger_table.log_file == Path(tmp / "mgr.log"))
        check("manager honours verbose", mgr.logger_table.verbose is True)
        mgr.update_log(event="Verbose", details="only when on", verbose_only=True)
        check("verbose event reaches the file",
              "Verbose: only when on" in (tmp / "mgr.log").read_text(encoding="utf-8"))

        quiet_mgr = initialize_managers(disable_ui=True, log_file=str(tmp / "qm.log"))
        quiet_mgr.update_log(event="Hidden", details="nope", verbose_only=True)
        check("verbose event withheld by default",
              "Hidden" not in (tmp / "qm.log").read_text(encoding="utf-8"))
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}\nlogging tests — passed {passed}, failed {failed}\n{'=' * 46}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
