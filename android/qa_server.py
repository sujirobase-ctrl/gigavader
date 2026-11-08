"""Synthetic forum and media for ADB reverse tests; localhost only."""

from __future__ import annotations

import io
import json
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import av
from PIL import Image, ImageDraw

QA = Path(__file__).parent / "qa"
QA.mkdir(exist_ok=True)
TITLE = "BUIDL Android QA " + time.strftime("%H%M%S")
DATA = {}
for number, color in [(1, "#5650c6"), (2, "#237884"), (3, "#c67840")]:
    image = Image.new("RGB", (480, 300), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 450, 270), outline="white", width=5)
    draw.text((130, 140), f"BUIDL / ANDROID / {number}", fill="white")
    output = io.BytesIO()
    image.save(output, "JPEG")
    DATA[f"/photo{number}.jpg"] = output.getvalue(), "image/jpeg"
output = io.BytesIO()
with av.open(output, "w", format="mp4") as container:
    stream = container.add_stream("libx264", rate=12)
    stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
    for number in range(156):
        image = Image.new("RGB", (320, 180), (40, 60 + number % 180, 180))
        for packet in stream.encode(av.VideoFrame.from_image(image)):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
DATA["/video.mp4"] = output.getvalue(), "video/mp4"
DATA["/unavailable.mp4"] = b"<html>Preview not supported</html>", "text/html"
output = io.BytesIO()
with zipfile.ZipFile(output, "w") as archive:
    archive.writestr("readme.txt", "Synthetic BUIDL Android test. No user files.")
DATA["/pack.zip"] = output.getvalue(), "application/zip"
output = io.BytesIO()
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
    archive.writestr("resume.bin", bytes(range(256)) * 32768)
DATA["/resume.zip"] = output.getvalue(), "application/zip"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        with (QA / "requests.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "range": self.headers.get("Range"), "time": time.time()}) + "\n")
        base = "http://127.0.0.1:8876"
        if self.path.startswith("/threads/resume"):
            data=(f'<h1 class="p-title-value">BUIDL Crash QA</h1><article class="message" id="post-1"><div class="message-body"><a href="{base}/resume.zip">resume.zip</a></div></article>').encode()
            kind="text/html"
        elif self.path.startswith("/threads/qa"):
            second = "page-2" in self.path
            links = ["photo3.jpg", "pack.zip"] if second else ["photo1.jpg", "photo2.jpg", "video.mp4", "unavailable.mp4"]
            anchors = "".join(f'<a href="{base}/{name}">{name}</a>' if name.endswith('.mp4') else f'<a href="{base}/{name}"><img src="{base}/photo1.jpg">{name}</a>' for name in links)
            more = "" if second else f'<a class="pageNav-jump--next" href="{base}/threads/qa/page-2">Next</a>'
            data = (f'<h1 class="p-title-value">{TITLE}</h1>{more}'
                    f'<article class="message" id="post-{2 if second else 1}"><div class="message-body">{anchors}</div></article>').encode()
            kind = "text/html"
        else:
            data, kind = DATA.get(self.path, (b"not found", "text/plain"))
        header = self.headers.get("Range")
        if header and kind != "text/html":
            start, end = header[6:].split("-")
            start, end = int(start), min(int(end) if end else len(data)-1, len(data)-1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            data = data[start:end+1]
        else:
            self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"qa-v1"')
        self.end_headers()
        try:
            if self.path == '/resume.zip':
                for start in range(0,len(data),32768):
                    self.wfile.write(data[start:start+32768]);self.wfile.flush();time.sleep(.025)
            else:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


if __name__ == "__main__":
    print(f"Local QA server: http://127.0.0.1:8876/threads/qa — {TITLE}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8876), Handler).serve_forever()
