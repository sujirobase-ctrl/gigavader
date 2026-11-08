"""Local mock XenForo forum + media host, used to test the downloader end-to-end.

Serves:
  /threads/test-thread.123/          a thread page with 2 posts and pagination
  /threads/test-thread.123/page-2    the second page
  /media/<name>                      real binary files
  /host/img/<id>                     an image-host page with og:image meta
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("MOCK_PORT", "8777"))
BASE = f"http://127.0.0.1:{PORT}"

# Deterministic fake binaries of known sizes.
MEDIA_FILES = {
    "photo1.jpg": b"\xff\xd8\xff\xe0" + b"A" * 20_000,
    "photo2.png": b"\x89PNG\r\n\x1a\n" + b"B" * 35_000,
    "clip.mp4": b"\x00\x00\x00\x18ftypmp42" + b"C" * 120_000,
    "track.mp3": b"ID3" + b"Z" * 5_000,
    "hosted.jpg": b"\xff\xd8\xff\xe0" + b"D" * 15_000,
    "album1.jpg": b"\xff\xd8\xff\xe0" + b"E" * 8_000,
    "album2.jpg": b"\xff\xd8\xff\xe0" + b"F" * 9_000,
}

THREAD_PAGE_1 = f"""<!DOCTYPE html>
<html><head><title>Test Thread</title></head><body>
<h1 class="p-title-value">My Test Thread \U0001f525 Pack</h1>
<div class="block-body">
  <article class="message message--post" id="js-post-1001">
    <ul class="message-attribution-opposite">
      <li><a href="/threads/test-thread.123/post-1001">#1</a></li>
    </ul>
    <div class="message-content">
      <div class="message-userContent">
        <div class="message-body">
          <p>First post with direct links.</p>
          <a href="{BASE}/media/photo1.jpg">photo1</a>
          <img src="{BASE}/media/photo2.png">
          <a href="{BASE}/media/clip.mp4">clip</a>
          <a href="{BASE}/media/track.mp3">track</a>
          <div class="bbCodeBlock--spoiler"><div class="bbCodeBlock-content">password: hunter2</div></div>
        </div>
      </div>
    </div>
    <footer></footer>
  </article>
  <article class="message message--post" id="js-post-1002">
    <ul class="message-attribution-opposite">
      <li><a href="/threads/test-thread.123/post-1002">#2</a></li>
    </ul>
    <div class="message-content">
      <div class="message-userContent">
        <div class="message-body">
          <p>Second post with a host page.</p>
          <a href="{BASE}/host/img/abc123">image host</a>
          <blockquote class="bbCodeBlock"><div class="bbCodeBlock-title">Quoted</div>
            <a href="{BASE}/media/should-not-download.jpg">quoted link</a>
          </blockquote>
        </div>
      </div>
    </div>
    <footer></footer>
  </article>
</div>
<nav><a class="pageNav-jump pageNav-jump--next" href="/threads/test-thread.123/page-2">Next</a></nav>
</body></html>"""

THREAD_PAGE_2 = f"""<!DOCTYPE html>
<html><head><title>Test Thread</title></head><body>
<h1 class="p-title-value">My Test Thread \U0001f525 Pack</h1>
<article class="message message--post" id="js-post-1003">
  <ul class="message-attribution-opposite">
    <li><a href="/threads/test-thread.123/post-1003">#3</a></li>
  </ul>
  <div class="message-content"><div class="message-userContent"><div class="message-body">
    <p>Page two post.</p>
    <a href="{BASE}/media/album1.jpg">a1</a>
    <a href="{BASE}/media/album2.jpg">a2</a>
  </div></div></div>
  <footer></footer>
</article>
</body></html>"""

HOST_PAGE = f"""<!DOCTYPE html>
<html><head>
<meta property="og:image" content="{BASE}/media/hosted.jpg">
<title>Image host</title></head>
<body><img id="main-image" src="{BASE}/media/hosted.jpg"></body></html>"""


class Handler(BaseHTTPRequestHandler):
    """Serve the mock pages and media."""

    def log_message(self, *_args) -> None:  # noqa: ANN002
        """Silence request logging."""

    def do_GET(self) -> None:  # noqa: N802
        """Route GET requests."""
        path = self.path.split("?")[0]

        if path.rstrip("/") == "/threads/test-thread.123":
            self._send_html(THREAD_PAGE_1)
        elif path.rstrip("/") == "/threads/test-thread.123/page-2":
            self._send_html(THREAD_PAGE_2)
        elif path.startswith("/host/img/"):
            self._send_html(HOST_PAGE)
        elif path.startswith("/media/"):
            self._send_media(path.rsplit("/", 1)[-1])
        else:
            self.send_error(404)

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_media(self, name: str) -> None:
        data = MEDIA_FILES.get(name)
        if data is None:
            self.send_error(404)
            return

        content_type = (
            "image/jpeg" if name.endswith(".jpg")
            else "image/png" if name.endswith(".png")
            else "audio/mpeg" if name.endswith(".mp3")
            else "video/mp4"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve() -> HTTPServer:
    """Start the mock server on a background thread."""
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    httpd = serve()
    print(f"Mock forum on {BASE}/threads/test-thread.123/")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
