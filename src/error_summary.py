"""Turn a raw requests/connection error string into a short, human summary.

The raw errors raised by ``requests`` / ``http_client`` embed the full URL and
the whole exception chain, which is useless on a progress panel. This module
collapses them into one Portuguese line plus an optional host hint so the user
can tell at a glance *why* a file failed (timeout, DNS, 403, rate-limit, ...).
"""

from __future__ import annotations

import re

# Known hosts get a friendlier explanation so the user knows who is at fault.
_HOST_HINTS: dict[str, str] = {
    "prxp-b.cdn.cr": "host do bunkr (prxp) parece instável/offline",
    "bunkr": "host do bunkr parece instável/offline",
    "fsc": "CDN do SimpCity barrou este link (403)",
    "cdn.cr": "CDN do SimpCity (cdn.cr)",
    "filester": "filester parece instável/offline",
    "imgboe": "imgboe parece instável/offline",
}


def _host_from(raw: str) -> str | None:
    """Extract a hostname from a URL embedded anywhere in the raw string."""
    match = re.search(r"https?://([^/\s'\"\)]+)", raw)
    if not match:
        return None
    host = match.group(1).split(":")[0]
    return host


def _host_hint(host: str | None) -> str:
    if not host:
        return ""
    lowered = host.lower()
    for key, hint in _HOST_HINTS.items():
        if key in lowered:
            return f" — {hint}"
    return f" (host: {host})"


def summarize_error(raw: str) -> str:
    """Return a short Portuguese explanation of a download failure.

    ``raw`` is typically ``str(exception)`` — possibly the chained
    ``GET failed after N attempts: <url> (Caused by ...)`` produced by
    ``http_client``.
    """
    if not raw:
        return "erro de rede desconhecido"

    raw_l = raw.lower()
    host = _host_from(raw)
    suffix = _host_hint(host)

    # DNS / name resolution
    if (
        "no address associated with hostname" in raw_l
        or "name or service not known" in raw_l
        or "failed to resolve" in raw_l
        or "getaddrinfo" in raw_l
    ):
        return f"DNS falhou — domínio offline ou indisponível{suffix}"

    # Connection-level
    if "connection refused" in raw_l:
        return f"conexão recusada pelo servidor{suffix}"
    if "connection reset" in raw_l or "connection aborted" in raw_l:
        return f"conexão resetada pelo servidor{suffix}"
    if "timed out" in raw_l or "connecttimeout" in raw_l or "readtimeout" in raw_l:
        return f"timeout de conexão — host lento ou fora do ar{suffix}"

    # Retries exhausted (recurse into the cause)
    if "max retries exceeded" in raw_l:
        cause = re.search(r"\(caused by (.+)\)\s*$", raw_l)
        if cause:
            return summarize_error(cause.group(1))
        return f"esgotou as tentativas de download{suffix}"

    # TLS
    if "ssl" in raw_l or "certificate" in raw_l or "tlsv" in raw_l:
        return f"erro de SSL/certificado{suffix}"

    # HTTP statuses
    if "desafio anti-bot" in raw_l:
        return "O site exigiu verificação anti-bot. Confira no navegador."
    if "ao renovar o link" in raw_l:
        return raw
    if "403" in raw_l or "forbidden" in raw_l:
        return "O servidor recusou o download do app (HTTP 403). Confira no navegador."
    if "429" in raw_l:
        return "rate-limit (429) — aguarde alguns minutos e tente de novo"
    if "401" in raw_l or "unauthorized" in raw_l:
        return "O servidor exige autenticação para o download (HTTP 401). Confira a sessão."
    if "404" in raw_l or "not found" in raw_l:
        return "O servidor não encontrou o endereço solicitado pelo app (HTTP 404)."
    if "410" in raw_l or "gone" in raw_l:
        return "O servidor marcou o endereço solicitado como indisponível (HTTP 410)."
    if re.search(r"\bhttp 523\b", raw_l):
        return "A Cloudflare não conseguiu acessar o servidor de origem (HTTP 523)."
    server_error = re.search(r"\bhttp (5\d{2})\b", raw_l)
    if server_error:
        return f"O servidor retornou um erro ao entregar o arquivo (HTTP {server_error[1]})."
    if any(code in raw_l for code in ("500", "502", "503", "504")) or "gateway" in raw_l:
        return "erro temporário do servidor (5xx) — tente de novo mais tarde"

    # HTML where a file was expected
    if "html" in raw_l and "instead of a file" in raw_l:
        return "O servidor entregou uma página ou mensagem, em vez do arquivo."
    if "incomplete response" in raw_l:
        return "download interrompido antes de receber o arquivo completo"

    return f"falha de rede{suffix}"
