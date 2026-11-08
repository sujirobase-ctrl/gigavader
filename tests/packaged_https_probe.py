"""Read-only HTTPS probe built with the same GUI bundling configuration."""

import json
import os
import ssl
import sys
import traceback
from pathlib import Path

import gui  # Include the same dependencies and hooks as the main GUI.
import certifi
import requests.adapters
from src.http_client import HttpClient


def run():
    report = Path(sys.executable).parent.parent / "Documentacao" / "diagnostico-https.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    result = {"frozen": bool(getattr(sys, "frozen", False)), "cwd": str(Path.cwd()),
              "openssl": ssl.OPENSSL_VERSION, "ca_exists": Path(certifi.where()).is_file(),
              "requests_ca_exists": Path(requests.adapters.DEFAULT_CA_BUNDLE_PATH).is_file(),
              "environment_paths": {}, "requests": []}
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"):
        value = os.environ.get(name)
        result["environment_paths"][name] = {"set": value is not None,
                                             "exists": Path(value).exists() if value else None,
                                             "parent_exists": Path(value).parent.exists() if value else None}
    for url in ("https://example.com/", "https://simpcity.cr/"):
        with HttpClient(timeout=15, max_retries=1) as client:
            item = {"url": url}
            try:
                response = client.session.get(url, timeout=15)
                item["status"] = response.status_code
                item["bytes"] = len(response.content)
            except Exception as error:
                item["error"] = str(error)
                item["traceback"] = traceback.format_exc()
            result["requests"].append(item)
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run()
