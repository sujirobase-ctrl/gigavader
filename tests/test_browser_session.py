import sys
import time
from types import SimpleNamespace

import pytest
from requests import Request

from src.browser_session import FORUM_ORIGIN, SESSION_SUFFIX, expiry_info, save_session, trusted_origin
from src.desktop import DesktopController
from src.http_client import HttpClient


def snapshot():
    return {
        "origin": FORUM_ORIGIN, "agent": "BUIDL Test Browser/1",
        "cookies": [
            {"name": "xf_user", "value": "synthetic-auth-value", "domain": "simpcity.cr",
             "path": "/", "expires": int(time.time()) + 3600},
            {"name": "xf_session", "value": "temporary-session", "domain": ".simpcity.cr",
             "path": "/", "expires": None},
        ],
    }


@pytest.mark.parametrize("url", [
    "http://simpcity.cr/", "https://simpcity.cr.example/", "https://simpcity.cr:444/",
    "https://account@simpcity.cr/", "https://example.org/?https://simpcity.cr/",
])
def test_capture_blocks_other_origins(url):
    assert trusted_origin(url) is None
    assert trusted_origin("https://www.simpcity.cr/threads/example") == "https://www.simpcity.cr/"


def test_session_cookie_has_no_fixed_expiration():
    data = snapshot()
    assert expiry_info(data)["login"] is not None
    data["cookies"].pop(0)
    assert expiry_info(data)["login"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_captured_cookies_are_encrypted_and_kept_in_scope(tmp_path):
    path = tmp_path / ("simpcity" + SESSION_SUFFIX)
    save_session(path, snapshot())
    assert b"synthetic-auth-value" not in path.read_bytes()

    with HttpClient(cookies_file=str(path)) as client, client.fork() as clone:
        def headers(url):
            return clone.session.prepare_request(Request("GET", url)).headers

        primary = headers(FORUM_ORIGIN)
        assert primary["User-Agent"] == "BUIDL Test Browser/1"
        assert "xf_user=synthetic-auth-value" in primary["Cookie"]
        assert "xf_session=temporary-session" in primary["Cookie"]
        assert headers("https://www.simpcity.cr/")["Cookie"] == "xf_session=temporary-session"
        assert "Cookie" not in headers("https://example.org/")
        assert "Cookie" not in headers("http://simpcity.cr/")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
@pytest.mark.parametrize(("body", "http", "expected"), [
    (b'<html data-logged-in="true">', 200, "valid"),
    (b'<html data-logged-in="false">', 200, "signed_out"),
    (b"<html>challenge</html>", 403, "unknown"),
])
def test_capture_reports_expiry_and_verification_without_exposing_values(tmp_path, monkeypatch, body, http, expected):
    import src.windows_login as login

    monkeypatch.setattr(login, "capture_in_window", lambda owner, origin: snapshot())

    class Response:
        status_code = http
        url = FORUM_ORIGIN
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, _size): yield body

    monkeypatch.setattr(HttpClient, "get", lambda *args, **kwargs: Response())
    controller = DesktopController(tmp_path)
    controller.window = SimpleNamespace(native=object())
    info = controller.capture_cookies()
    assert info["state"] == expected and info["checked_at"] and info["expires"]["login"]
    assert "synthetic-auth-value" not in str(info)
    assert DesktopController(tmp_path).bootstrap()["cookie_status"] == info
