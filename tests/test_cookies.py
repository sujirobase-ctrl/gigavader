"""Ad-hoc verification of the cookie/login work (current on-disk state).

Re-establishes evidence after the previous temp script was cleaned up.
No production code changed since that run; this re-runs the same checks.

Covers: expires=0 cookies are sent; 403 is reported as a login problem;
login-wall detection with false-positive guards; setup_cookies flag
precedence; the prompt loop; saved-path memory.
"""

from __future__ import annotations

import http.server
import io
import json
import shutil
import sys
import tempfile
import threading
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from bs4 import BeautifulSoup  # noqa: E402
from rich.console import Console  # noqa: E402

import downloader as cli  # noqa: E402
import src.cookie_setup as cs  # noqa: E402
from src.downloaders import ThreadDownloader, ThreadOptions  # noqa: E402
from src.downloaders.thread_downloader import looks_like_login_wall  # noqa: E402
from src.http_client import HttpClient  # noqa: E402

PORT = 8813
BASE = f"http://127.0.0.1:{PORT}"
MEDIA = b"\xff\xd8\xff\xe0" + b"Z" * 5_000

THREAD = f"""<html><head><title>t</title></head><body>
<h1 class="p-title-value">Members Only</h1>
<article class="message message--post" data-content="post-7">
<header><a href="/threads/g.1/post-7">#1</a></header>
<div class="message-content"><div class="message-userContent"><div class="message-body">
<a href="{BASE}/media/secret.jpg">f</a>
</div></div></div></article></body></html>"""

LOGIN_PAGE = """<html><body><div class="p-body"><h1>Log in</h1>
<p>You must be logged in to view this page.</p>
<form action="/login/login" method="post"><input name="login"></form>
</div></body></html>"""

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed  # noqa: PLW0603
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  <- ' + detail}")


class Gated(http.server.BaseHTTPRequestHandler):
    """403 unless the session cookie is present."""

    def log_message(self, *_a) -> None:  # noqa: ANN002
        pass

    def do_GET(self) -> None:  # noqa: N802
        if "session=letmein" not in self.headers.get("Cookie", ""):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, ctype = ((MEDIA, "image/jpeg") if self.path.startswith("/media/")
                       else (THREAD.encode(), "text/html; charset=utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeLive:
    """Minimal LiveManager stand-in recording log events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def update_log(self, *, event: str, details: str, verbose_only: bool = False) -> None:
        if verbose_only:
            return
        self.events.append((event, details))

    def add_overall_task(self, *_a, **_k) -> None: ...
    def update_task(self, *_a, **_k) -> None: ...
    def update_summary(self, *_a, **_k) -> None: ...
    def add_task(self, *_a, **_k) -> int: return 0

    def text(self) -> str:
        return " | ".join(f"{e}: {d}" for e, d in self.events)


def write_cookies(path: Path) -> None:
    """Netscape cookies.txt with expires=0 -- the browser session-cookie case."""
    path.write_text("# Netscape HTTP Cookie File\n\n"
                    "127.0.0.1\tFALSE\t/\tFALSE\t0\tsession\tletmein\n"
                    "127.0.0.1\tFALSE\t/\tFALSE\t0\txf_user\t42\n", encoding="utf-8")


def drive(answers: list[str], *, required: bool = False) -> tuple[str | None, str]:
    """Run prompt_for_cookies with scripted answers (no TTY available)."""
    buf = io.StringIO()
    supply = iter(answers)
    with patch.object(cs, "can_prompt", return_value=True), \
         patch("builtins.input", lambda: next(supply)):
        result = cs.prompt_for_cookies(Console(file=buf, width=100), required=required)
    return result, buf.getvalue()


def wall(html: str) -> bool:
    return looks_like_login_wall(BeautifulSoup(html, "html.parser"))


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", PORT), Gated)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="hermes-verify-ck-"))
    home = tmp / "home"
    home.mkdir()
    jar = tmp / "cookies.txt"
    write_cookies(jar)
    url = f"{BASE}/threads/g.1/"
    original = (cs.CONFIG_DIR, cs.CONFIG_FILE)
    cs.CONFIG_DIR, cs.CONFIG_FILE = home / ".fpd", home / ".fpd" / "config.json"

    try:
        print("[1. expires=0 session cookies are actually sent]")
        anon = HttpClient()
        check("403 anonymous (gate works)", anon.get(url).status_code == 403)
        anon.close()
        client = HttpClient(cookies_file=str(jar))
        check("both cookies loaded",
              {c.name for c in client.session.cookies} == {"session", "xf_user"})
        check("expiry rewritten to future",
              all(c.expires and c.expires > 0 for c in client.session.cookies))
        check("200 with cookies (was 403 pre-fix)", client.get(url).status_code == 200)
        client.close()

        print("\n[2. a 403 explains itself instead of 'Empty thread']")
        live = FakeLive()
        anon2 = HttpClient()
        ThreadDownloader(anon2, live,
                         ThreadOptions(custom_path=str(tmp / "x"))).download_thread(url)
        anon2.close()
        log = live.text()
        check("logs 'Access denied'", "Access denied" in log, log[:150])
        check("mentions 403 and --cookies", "403" in log and "--cookies" in log, log[:200])

        print("\n[3. login-wall detection + false-positive guards]")
        check("login page recognised", wall(LOGIN_PAGE))
        check("permission notice recognised",
              wall("<html><body><div class='p-body'>You do not have permission "
                   "to view this page.</div></body></html>"))
        check("real thread NOT flagged", not wall(THREAD))
        check("thread TITLED 'Members Only' NOT flagged",
              not wall("<html><body><h1 class='p-title-value'>Members Only Club</h1>"
                       "<div class='p-body'>Regular discussion.</div></body></html>"))
        check("header login link NOT flagged",
              not wall("<html><body><div class='p-nav'><a href='/login/'>Log in</a></div>"
                       "<div class='p-body'>Normal.</div></body></html>"))

        print("\n[4. flag precedence]")
        check("--disable-ui still resolves cookies",
              cli.setup_cookies(Namespace(cookies=str(jar), no_cookies=False,
                                          reset_cookies=False, disable_ui=True))
              == cs.normalize(str(jar)))
        check("--no-cookies returns None",
              cli.setup_cookies(Namespace(cookies=None, no_cookies=True,
                                          reset_cookies=False, disable_ui=False)) is None)

        print("\n[5. end-to-end download through the gate]")
        out = tmp / "dl"
        authed = HttpClient(cookies_file=str(jar))
        failure = ThreadDownloader(authed, FakeLive(),
                                   ThreadOptions(custom_path=str(out))).download_thread(url)
        authed.close()
        check("no failure reported", failure is False)
        got = list((out / "Downloads").rglob("*.jpg")) if (out / "Downloads").exists() else []
        check("secret.jpg @5004B", len(got) == 1 and got[0].stat().st_size == 5_004,
              str([(p.name, p.stat().st_size) for p in got]))

        print("\n[6. validation]")
        ok, msg = cs.validate(str(jar))
        check("good file accepted", ok and "2 cookies" in msg, msg)
        check("missing rejected", not cs.validate(str(tmp / "ghost.txt"))[0])
        bad = tmp / "bad.txt"
        bad.write_text("not a cookie file\n", encoding="utf-8")
        bad_ok, bad_msg = cs.validate(str(bad))
        check("malformed rejected, mentions TAB", not bad_ok and "TAB" in bad_msg, bad_msg)
        empty = tmp / "empty.txt"
        empty.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        check("empty rejected", not cs.validate(str(empty))[0])
        check("strips quotes", cs.normalize(f'"{jar}"') == str(jar))

        print("\n[7. prompt loop]")
        result, out_text = drive([str(jar)])
        check("accepts good path", result == str(jar))
        check("shows instructions", "How to export cookies" in out_text)
        result, out_text = drive([str(tmp / "ghost.txt"), str(jar)])
        check("re-asks after bad path, then accepts", result == str(jar))
        check("prompted twice", out_text.count("Path to cookies.txt") == 2)
        check("Enter skips when optional", drive([""])[0] is None)
        check("Enter refused when required", drive(["", str(jar)], required=True)[0] == str(jar))

        print("\n[8. saved-path memory]")
        cs.save_path(str(jar))
        check("saved and reloaded", cs.load_saved_path() == str(jar.resolve()))
        check("json key present",
              "cookies_file" in json.loads(cs.CONFIG_FILE.read_text(encoding="utf-8")))
        cs.forget_path()
        check("forget clears", cs.load_saved_path() is None)
        stale = tmp / "stale.txt"
        write_cookies(stale)
        cs.save_path(str(stale))
        stale.unlink()
        check("stale path dropped", cs.load_saved_path() is None)

        print("\n[9. non-interactive safety]")
        with patch.object(cs, "can_prompt", return_value=False):
            buf = io.StringIO()
            check("returns None immediately",
                  cs.prompt_for_cookies(Console(file=buf)) is None)
            check("prints nothing", buf.getvalue() == "")
    finally:
        cs.CONFIG_DIR, cs.CONFIG_FILE = original
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 52}\nad-hoc verification — passed {passed}, failed {failed}\n{'=' * 52}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
