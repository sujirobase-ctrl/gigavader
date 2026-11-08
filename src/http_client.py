"""A small requests wrapper with retries, cookies and forum-friendly headers."""

from __future__ import annotations

import time
import threading
from http.cookiejar import DefaultCookiePolicy, LoadError, MozillaCookieJar
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from src.browser_session import SESSION_SUFFIX, cookie_objects, load_session
from src.config import HTTP_TIMEOUT, RATE_429_BACKOFF, RETRY_BACKOFF, USER_AGENT


def _consume_retry_after(response: requests.Response | None) -> bool:
    """Sleep the server's ``Retry-After`` (seconds) if present; return True if we did.

    Used for 429/503 so we honour the CDN's own backoff window instead of our
    fixed exponential curve. Caps at 60s to avoid an absurd server value.
    """
    if response is None:
        return False
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return False
    try:
        time.sleep(min(int(retry_after), 60))
        return True
    except (TypeError, ValueError):
        return False


class _Session(requests.Session):
    strict_cookie_hosts = False

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        if self.strict_cookie_hosts and not any(
            key.lower() == "cookie" for headers in (self.headers, request.headers or {}) for key in headers
        ):
            # Requests merges cookies into a fresh jar during preparation.
            # Preserve host-only rules in that jar, including across redirects.
            jar = prepared._cookies
            jar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
            prepared.headers.pop("Cookie", None)
            prepared.prepare_cookies(jar)
        return prepared


class HttpClient:
    """Thin session wrapper: GET html/json/bytes with bounded retries."""

    def __init__(
        self,
        cookies_file: str | None = None,
        user_agent: str = USER_AGENT,
        timeout: int = HTTP_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        self.session = _Session()
        self._user_agent = user_agent
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self._cookies_lock = threading.RLock()
        self._cookies_path = None
        self._cookies_stamp = None
        self._imported_cookies = set()
        self._gofile_lock = threading.RLock()
        self._gofile_token = None
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        if cookies_file:
            self.load_cookies(cookies_file)

    def load_cookies(self, cookies_file: str) -> None:
        """Load a Netscape-format cookies.txt (for login-gated forums).

        Session cookies exported from a browser carry `expires=0`, which
        http.cookiejar treats as *already expired*: `ignore_expires` only
        applies during load(), so copying such a cookie straight into the
        session makes requests silently drop it and every gated page comes
        back 403. Non-expiring cookies are therefore given a far-future
        expiry before being handed to the session.
        """
        path = Path(cookies_file)
        if not path.exists():
            msg = f"Cookies file not found: {cookies_file}"
            raise FileNotFoundError(msg)

        captured = path.suffix.lower() == SESSION_SUFFIX
        agent = self._user_agent
        if captured:
            snapshot = load_session(path)
            new_cookies = cookie_objects(snapshot)
            agent = snapshot["agent"]
        else:
            jar = MozillaCookieJar(str(path))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except LoadError:
                raise ValueError("Arquivo de cookies Netscape inválido. Exporte novamente.") from None
            new_cookies = list(jar)
            far_future = int(time.time()) + 10 * 365 * 24 * 3600
            for cookie in new_cookies:
                if not cookie.expires:
                    cookie.expires = far_future
                cookie.discard = False
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        if not new_cookies:
            msg = (
                f"No cookies found in '{cookies_file}'. Export it in "
                f"Netscape format with TAB-separated fields."
            )
            raise ValueError(msg)
        with self._cookies_lock:
            for domain, cookie_path, name in self._imported_cookies:
                try:
                    self.session.cookies.clear(domain, cookie_path, name)
                except KeyError:
                    pass
            for cookie in new_cookies:
                self.session.cookies.set_cookie(cookie)
            self.session.strict_cookie_hosts = captured
            self.session.headers["User-Agent"] = agent
            self._imported_cookies = {(c.domain, c.path, c.name) for c in new_cookies}
            self._cookies_path, self._cookies_stamp = path, stamp

    def refresh_cookies_if_changed(self) -> bool:
        """Pick up a newly exported cookie file without restarting a long job."""
        with self._cookies_lock:
            if self._cookies_path is None:
                return False
            try:
                stat = self._cookies_path.stat()
                if (stat.st_mtime_ns, stat.st_size) == self._cookies_stamp:
                    return False
                self.load_cookies(str(self._cookies_path))
                return True
            except (OSError, ValueError):
                # Browser exports may replace the file in two steps. Retain
                # working cookies and try again on the next request.
                return False

    def fork(self):
        """Give a resolver worker its own connection pool and cookie jar."""
        with self._cookies_lock:
            clone = HttpClient(timeout=self.timeout, max_retries=self.max_retries, user_agent=self._user_agent)
            clone.session.headers.update(self.session.headers)
            clone.session.cookies.update(self.session.cookies)
            clone._cookies_path = self._cookies_path
            clone._cookies_stamp = self._cookies_stamp
            clone._imported_cookies = set(self._imported_cookies)
            clone.session.strict_cookie_hosts = self.session.strict_cookie_hosts
            return clone

    def get(self, url: str, **kwargs) -> requests.Response:  # noqa: ANN003
        """GET with retry/backoff on transient failures.

        HTTP 429 (Too Many Requests) gets special treatment: the server is
        rate-limiting us, so we back off *longer* than for a generic 5xx and
        honour its ``Retry-After`` header when present. CDNs like SimpCity's
        c3wi-b.cdn.cr emit 429 without Retry-After, so we fall back to an
        exponential backoff (RATE_429_BACKOFF ** attempt) capped at 60s.
        """
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        self.refresh_cookies_if_changed()

        last_error: Exception | None = None
        attempts = 1 if kwargs.get("stream") else self.max_retries
        for attempt in range(attempts):
            try:
                response = self.session.get(url, **kwargs)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}",
                        response=response,
                    )
            except requests.HTTPError as error:
                # Server returned a retryable status. Sleep by status, then retry.
                last_error = error
                status = getattr(error.response, "status_code", None)
                if attempt < attempts - 1:
                    if status == 429:
                        waited = _consume_retry_after(error.response)
                        if not waited:
                            time.sleep(min(RATE_429_BACKOFF ** (attempt + 1), 60))
                    elif status == 503:
                        waited = _consume_retry_after(error.response)
                        if not waited:
                            time.sleep(RETRY_BACKOFF ** min(attempt, 4))
                    else:
                        time.sleep(RETRY_BACKOFF ** min(attempt, 4))
                if kwargs.get("stream"):
                    if error.response is not None:
                        error.response.close()
                    raise
                continue
            except requests.RequestException as error:
                # Connection drop / timeout: short backoff.
                last_error = error
                if attempt < attempts - 1:
                    time.sleep(RETRY_BACKOFF ** min(attempt, 4))
                continue
            else:
                return response

        msg = f"GET failed after {attempts} attempts: {url} ({last_error})"
        raise requests.RequestException(msg) from last_error

    def get_soup(self, url: str, **kwargs) -> BeautifulSoup:  # noqa: ANN003
        """GET and parse as HTML."""
        response = self.get(url, **kwargs)
        return BeautifulSoup(response.text, "html.parser")

    def get_json(self, url: str, **kwargs) -> dict:  # noqa: ANN003
        """GET and parse as JSON."""
        response = self.get(url, **kwargs)
        return response.json()

    def head(self, url: str, **kwargs) -> requests.Response:  # noqa: ANN003
        """HEAD request (used for size / range probing)."""
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        return self.session.head(url, **kwargs)

    def close(self) -> None:
        """Close the underlying session."""
        self.session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
