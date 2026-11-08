"""Protected Windows snapshots of first-party SimpCity cookies."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from http.cookiejar import Cookie
from urllib.parse import urlsplit


SESSION_SUFFIX = ".buidl-session"
FORUM_ORIGIN = "https://simpcity.cr/"
FORUM_HOSTS = {"simpcity.cr", "www.simpcity.cr"}


def trusted_origin(url):
    try:
        parsed = urlsplit(url or "")
        if (parsed.scheme == "https" and parsed.hostname in FORUM_HOSTS
                and parsed.username is None and parsed.password is None and parsed.port in (None, 443)):
            return f"https://{parsed.hostname}/"
    except ValueError:
        pass
    return None


def logged_in(soup):
    html = soup.find("html")
    if html and html.get("data-logged-in") in ("true", "false"):
        return html["data-logged-in"] == "true"
    if soup.select_one(".p-navgroup-link--user"):
        return True
    if soup.select_one(".p-navgroup-link--logIn, form[action*='login/login']"):
        return False
    return None


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise ValueError("Sessões capturadas no Windows precisam do mesmo usuário do Windows.")
    import ctypes as c
    from ctypes import wintypes as w

    class Blob(c.Structure):
        _fields_ = [("size", w.DWORD), ("data", c.POINTER(c.c_ubyte))]

    crypt = c.WinDLL("crypt32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    buffer = (c.c_ubyte * len(data)).from_buffer_copy(data)
    incoming, outgoing = Blob(len(data), buffer), Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p,
                   c.c_void_p, w.DWORD, c.POINTER(Blob)]
    fn.restype = w.BOOL
    if not fn(c.byref(incoming), None, None, None, None, 1, c.byref(outgoing)):
        raise OSError("O Windows não conseguiu acessar a sessão protegida.")
    try:
        return c.string_at(outgoing.data, outgoing.size)
    finally:
        c.memset(outgoing.data, 0, outgoing.size)
        kernel.LocalFree(outgoing.data)


def save_session(path, snapshot):
    # The controller serializes capture operations. Replace only after flush.
    import os
    import tempfile

    cookie_objects(snapshot)
    encrypted = _dpapi(json.dumps(snapshot, ensure_ascii=True).encode(), decrypt=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            staged = path.with_name(os.path.basename(handle.name))
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def load_session(path):
    try:
        with path.open("rb") as handle:
            data = handle.read(1024 * 1024 + 1)
        if not data or len(data) > 1024 * 1024:
            raise ValueError
        snapshot = json.loads(_dpapi(data, decrypt=True))
        cookie_objects(snapshot)
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Não foi possível ler a sessão protegida. Capture o login novamente.") from None


def cookie_objects(snapshot):
    origin = snapshot.get("origin")
    if not origin or trusted_origin(origin) != origin:
        raise ValueError("Site inválido para a sessão capturada.")
    agent = snapshot.get("agent", "")
    if not isinstance(agent, str) or not 0 < len(agent) <= 1024 or not agent.isascii() or not agent.isprintable():
        raise ValueError("Navegador inválido para a sessão capturada.")
    entries = snapshot.get("cookies", [])
    if not isinstance(entries, list) or not 0 < len(entries) <= 300:
        raise ValueError("Nenhum cookie válido foi capturado.")
    result = []
    for entry in entries:
        name, value, domain, path, expiry = (entry[key] for key in ("name", "value", "domain", "path", "expires"))
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or not value.isascii() or not value.isprintable() and value != "" or ";" in value
                or domain.removeprefix(".") not in FORUM_HOSTS or not path.startswith("/")
                or expiry is not None and (type(expiry) is not int or not 0 < expiry <= 253402300799)):
            raise ValueError("A captura retornou um cookie inválido.")
        scoped = domain.startswith(".")
        result.append(Cookie(0, name, value, None, False, domain, scoped, scoped, path, True,
                             True, expiry, expiry is None, None, None, {"HttpOnly": True}))
    return result


def expiry_info(snapshot):
    dates = {}
    for names, label in ((("xf_user", "xf_session"), "login"), (("cf_clearance",), "protection")):
        for name in names:
            selected = [cookie["expires"] for cookie in snapshot["cookies"] if cookie["name"] == name]
            if selected:
                dated = [value for value in selected if value is not None]
                dates[label] = datetime.fromtimestamp(min(dated)).astimezone().isoformat(timespec="seconds") if dated else None
                break
    return dates
