"""Real codec + local HTTP tests. No forum cookies or user media required."""

from __future__ import annotations

import io
import struct
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import av
import pytest
from PIL import Image

import src.video_preview as video
from src.resolvers import ResolvedMedia
from src.video_preview import (
    PreviewBudget,
    PreviewUnavailable,
    RangeReader,
    fetch_video_preview,
)


@pytest.fixture(scope="module")
def movie():
    output = io.BytesIO()
    with av.open(output, "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=8)
        stream.width, stream.height, stream.pix_fmt = 160, 90, "yuv420p"
        for index in range(24):
            image = Image.new("RGB", (160, 90), (index * 10, 40, 180))
            frame = av.VideoFrame.from_image(image)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


@pytest.fixture
def host(movie):
    requests = []
    content = {"/movie.mp4": movie}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path == "/html.mp4":
                body, kind = b"<html>login</html>", "text/html"
            else:
                body, kind = content.get(self.path, movie), "video/mp4"
            ranged = self.headers.get("Range") and self.path not in {"/no-range.mp4", "/html.mp4", "/small.mp4"}
            self.send_response(206 if ranged else 200)
            self.send_header("Content-Type", kind)
            if ranged:
                start, end = map(int, self.headers["Range"][6:].split("-"))
                end = min(end, len(body) - 1)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                body = body[start:end + 1]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", content, requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize("path", ["/movie.mp4", "/small.mp4"])
def test_real_video_frames_decode_without_disk(host, path, tmp_path):
    base, _, requests = host
    result = fetch_video_preview(ResolvedMedia(base + path, "movie.mp4"), None, Event())
    assert 16 <= len(result.frames) <= 48
    assert result.frames[0].size == (160, 90)
    assert result.frames[0].getpixel((5, 5)) != result.frames[-1].getpixel((5, 5))
    assert "sem áudio" in result.message
    assert requests[0][1]["Range"].startswith("bytes=0-")
    assert not list(tmp_path.iterdir())


def test_mp4_metadata_at_end_does_not_download_whole_file(host, movie):
    base, content, requests = host
    position = movie.rfind(b"moov") - 4
    padding_size = 26 * 1024 * 1024
    content["/tail.mp4"] = movie[:position] + struct.pack(
        ">I4s", padding_size, b"free",
    ) + bytes(padding_size - 8) + movie[position:]
    result = fetch_video_preview(ResolvedMedia(base + "/tail.mp4", "tail.mp4"), None, Event())
    assert len(result.frames) >= 16
    assert len(requests) < 10
    assert any(int(headers["Range"][6:].split("-")[0]) > 24 * 1024 * 1024 for _, headers in requests)


@pytest.mark.parametrize("path", ["/html.mp4", "/no-range.mp4", "/broken.mp4"])
def test_unsupported_hosts_return_cover_fallback(host, path):
    base, content, _ = host
    content["/no-range.mp4"] = bytes(video.BLOCK_SIZE + 1)
    content["/broken.mp4"] = b"this is not a video"
    result = fetch_video_preview(ResolvedMedia(base + path, "video.mp4"), None, Event())
    assert result.frames == []
    assert "ainda pode ser baixado" in result.message


def test_byte_limit_keeps_download_failure_out_of_preview(host, monkeypatch):
    base, _, _ = host
    monkeypatch.setattr(video, "MAX_VIDEO_BYTES", 10)
    assert not fetch_video_preview(ResolvedMedia(base + "/movie.mp4", "movie.mp4"), None, Event()).frames


def test_cancel_before_loading_performs_no_request(host):
    base, _, requests = host
    stop = Event()
    stop.set()
    assert not fetch_video_preview(ResolvedMedia(base + "/movie.mp4", "movie.mp4"), None, stop).frames
    assert not requests


def test_range_reader_seeks_and_stops(host):
    base, content, requests = host
    content["/data"] = bytes(range(256)) * 3000
    stop = Event()
    budget = PreviewBudget(stop)
    with video.PreviewClient(None, budget) as client:
        reader = RangeReader(ResolvedMedia(base + "/data", "data.mp4"), client, budget)
        assert reader.read(3) == bytes((0, 1, 2))
        reader.seek(-8, io.SEEK_END)
        assert reader.read(8) == content["/data"][-8:]
        assert reader.read(2) == b""
        reader.seek(0)
        assert reader.read(3) == bytes((0, 1, 2))
        assert len(requests) == 2
        stop.set()
        with pytest.raises(PreviewUnavailable):
            reader.read(1)


def test_expired_budget_never_connects(host):
    base, _, requests = host
    budget = PreviewBudget(Event())
    budget.deadline = time.monotonic() - 1
    with video.PreviewClient(None, budget) as client, pytest.raises(PreviewUnavailable):
        client.get(base)
    assert not requests


def test_signing_refreshes_copy_without_changing_gallery_key(host, monkeypatch):
    base, _, requests = host
    original = ResolvedMedia("https://host.invalid/v/abc", "Video", resolver="turbo",
                             source_url="https://host.invalid/v/abc", headers={"Referer": "https://forum.invalid/"})
    snapshot = replace(original, headers=dict(original.headers))

    def refresh(media, _client):
        assert media is not original
        media.url = base + "/movie.mp4"
        media.headers["Referer"] = "https://host.invalid/"
        return media

    monkeypatch.setattr(video, "refresh_media", refresh)
    assert fetch_video_preview(original, None, Event()).frames
    assert original == snapshot
    assert requests[0][1]["Referer"] == "https://host.invalid/"


@pytest.mark.parametrize('dimensions', [(160, 90), (90, 160)])
def test_ten_second_preview_preserves_portrait_and_landscape(host, dimensions):
    base, content, _ = host
    output = io.BytesIO()
    with av.open(output, 'w', format='mp4') as container:
        stream = container.add_stream('mpeg4', rate=8)
        stream.width, stream.height, stream.pix_fmt = *dimensions, 'yuv420p'
        for index in range(104):  # Thirteen seconds: preview must stop at ten.
            image = Image.new('RGB', dimensions, (index*2, 40, 180))
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                container.mux(packet)
        for packet in stream.encode(): container.mux(packet)
    content['/long.mp4'] = output.getvalue()
    result = fetch_video_preview(ResolvedMedia(base+'/long.mp4', 'long.mp4'), None, Event())
    assert len(result.frames) == 80
    assert result.frames[0].size == dimensions
    assert len(result.frames) / result.fps == 10
    assert '10 segundos' in result.message


def test_cover_only_decodes_one_frame_without_full_preview(host):
    base, _, requests = host
    result = fetch_video_preview(ResolvedMedia(base+'/movie.mp4', 'movie.mp4'), None, Event(), cover_only=True)
    assert len(result.frames) == 1
    assert result.frames[0].width <= 240 and result.frames[0].height <= 150
    assert requests and all('Range' in headers for _, headers in requests)


def test_cover_budget_is_smaller_and_cancelled_cover_never_connects(host):
    stop = Event(); stop.set()
    base, _, requests = host
    assert not fetch_video_preview(ResolvedMedia(base+'/movie.mp4', 'movie.mp4'), None, stop, cover_only=True).frames
    assert not requests
    budget = PreviewBudget(Event(), max_bytes=4*1024*1024, timeout=8)
    with pytest.raises(PreviewUnavailable): budget.account(4*1024*1024+1)
