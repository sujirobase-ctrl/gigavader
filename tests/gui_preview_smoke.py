"""Manual/automated desktop smoke test using synthetic, non-network previews."""

# ruff: noqa: E402 -- Tcl/Tk and the repo path must be bootstrapped before GUI imports.

from __future__ import annotations

import os
import sys
import time
import tempfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
os.environ["BUIDL_RECOVERY_DIR"] = tempfile.mkdtemp(prefix="buidl-qa-recovery-")
sys.path.insert(0, str(ROOT))
for variable, folders in (("TCL_LIBRARY", ("tcl8.6", "tcl_library")),
                          ("TK_LIBRARY", ("tk8.6", "tk_library"))):
    for folder in folders:
        library = ROOT / "build" / "tk-runtime" / folder
        if library.is_dir():
            os.environ[variable] = str(library)
            break

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageGrab

import gui
import src.preview_gui as preview_gui
import src.video_preview_gui as video_gui
from gui import BuidlApp
from src.downloaders import PreparedThread
from src.preview import PreviewSession
from src.resolvers import ResolvedMedia
from src.video_preview import VideoPreview


def fixture_thumbnail(media, _cookies, stop):
    if stop.is_set() or media.filename.endswith(".zip"):
        return None
    image = Image.new("RGB", (240, 150), "#2E4A72")
    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 20, 222, 130), outline="#85B1F2", width=2)
    draw.text((36, 66), media.filename, fill="white")
    return image


def pump(app, seconds=0.5):
    # Use Tk's actual event loop so background-worker notifications and Tk
    # object cleanup behave as they do in the shipped application.
    app.after(max(1, int(seconds * 1000)), app.quit)
    app.mainloop()


def snapshot(window, path):
    # Capture only this test window, even when another app is in front of it.
    ImageGrab.grab(window=int(window.wm_frame(), 16)).save(path)


def run():
    ctk.set_appearance_mode("dark")
    preview_gui.fetch_thumbnail = fixture_thumbnail
    app = BuidlApp()
    app.geometry("1120x760+20+20")
    root = ROOT / "Downloads" / "Thread de demonstração"
    items = [
        ResolvedMedia(f"https://media.example/Photo-{i:02}.jpg", f"Photo-{i:02}.jpg", post_label=f"post-{i // 6 + 1}")
        for i in range(24)
    ]
    items += [ResolvedMedia("https://media.example/video.mp4", "Video exclusivo.mp4", post_label="post-5")]
    items += [ResolvedMedia("https://media.example/pack.zip", "Coleção completa.zip", post_label="post-5")]
    prepared = PreparedThread("Thread de demonstração — escolha o que baixar", root, [(item, root) for item in items], 5)
    decisions = []
    session = PreviewSession(prepared, ())
    dialog = preview_gui.PreviewWindow(app, prepared, None, decisions.append, lambda: decisions.append("cancel"),
                                       session=session, on_refresh=lambda: decisions.append("refresh"))
    dialog.geometry("1040x780+30+30")
    try:
        pump(app, 1)
        dialog._select_results(False)
        dialog.variables[0].set(True)
        dialog._toggle(0)
        dialog._change_page(1)
        pump(app)
        dialog.variables[20].set(True)
        dialog._toggle(20)
        dialog._change_page(-1)
        assert dialog.variables[0].get()
        assert len(dialog.model.selected) == 2
        dialog.kind.set("Vídeos")
        dialog._filter_changed()
        assert dialog._filtered() == [24]
        assert len(dialog.model.selected) == 2
        video_requests = []

        def fixture_video(media, _cookies, _stop):
            video_requests.append(media.state_key)
            return VideoPreview([Image.new("RGB", (320, 180), color) for color in
                                 ("#203D63", "#456395", "#6573B5", "#6C63FF")] * 6,
                                message="Trecho de até 6 segundos · sem áudio")

        video_gui.fetch_video_preview = fixture_video
        dialog._open_video(24)
        pump(app, 0.3)
        player = dialog.video_window
        assert player.playing and player.frame_index > 0
        player._toggle()
        paused = player.frame_index
        pump(app, 0.3)
        assert not player.playing and player.frame_index == paused
        snapshot(player, ROOT / "build" / "qa-video-1.3.0.png")
        player._toggle()
        pump(app, 0.2)
        assert player.frame_index != paused
        player.close()
        assert player.stop.is_set()
        assert len(dialog.model.selected) == 2 and dialog.cache[24] is not None
        dialog._open_video(24)
        pump(app, 0.2)
        assert video_requests == [items[24].state_key]
        dialog.video_window.close()

        # A failed playback keeps the cover and is not a download failure.
        video_gui.fetch_video_preview = lambda *_args: VideoPreview()
        dialog.videos.clear()
        late_cover = dialog.cache.pop(24)
        dialog._open_video(24)
        pump(app, 0.3)
        player = dialog.video_window
        assert player.frame_image is None
        dialog._show_image(24, late_cover)
        dialog.cache[24] = late_cover
        assert not player.playing and player.frame_image is not None
        assert player.play_button.cget("state") == "disabled"
        assert "ainda pode ser baixado" in player.status.cget("text")
        player.close()

        # Closing while the worker is active discards the eventual result.
        started = []

        def waiting_video(_media, _cookies, stop):
            started.append(stop)
            stop.wait(3)
            return fixture_video(_media, _cookies, stop)

        video_gui.fetch_video_preview = waiting_video
        dialog.videos.clear()
        dialog._open_video(24)
        pump(app, 0.2)
        assert started
        player = dialog.video_window
        player.close()
        pump(app, 0.2)
        assert started[0].is_set() and not dialog.videos
        assert len(dialog.model.selected) == 2
        dialog.kind.set("Todos")
        dialog._filter_changed()
        pump(app)
        shot = ROOT / "build" / "qa-preview-1.3.0.png"
        snapshot(dialog, shot)
        dialog._confirm(False)
        assert decisions == [{items[0].state_key, items[20].state_key}]

        def unexpected_fetch(*_args):
            raise AssertionError("cached thumbnail was fetched again")

        preview_gui.fetch_thumbnail = unexpected_fetch
        dialog = preview_gui.PreviewWindow(app, prepared, None, decisions.append, lambda: None, session=session)
        pump(app)
        assert len(dialog.images) == 18
        assert dialog.model.selected == {items[0].state_key, items[20].state_key}
        dialog._change_page(1)
        dialog.query.set("Photo")
        pump(app)
        dialog._change_page(1)
        pump(app)
        dialog.close()
        dialog = preview_gui.PreviewWindow(app, prepared, None, decisions.append, lambda: None, session=session)
        pump(app)
        assert dialog.page == 1 and dialog.query.get() == "Photo"
        dialog.close()
        preview_gui.fetch_thumbnail = fixture_thumbnail

        # Drive the real main-window callbacks twice, using a synthetic scan.
        # Only the first click may invoke prepare_thread.
        scans, downloads = [], []
        original_downloader = gui.ThreadDownloader

        class FakeDownloader:
            def __init__(self, *_args): pass
            def prepare_thread(self, url):
                scans.append(url)
                return prepared
            def download_prepared(self, queue, keys):
                assert queue is prepared
                downloads.append(keys)
                return False

        gui.ThreadDownloader = FakeDownloader
        try:
            with TemporaryDirectory(prefix="buidl-preview-smoke-") as output:
                app._replace_entry(app.url_entry, "https://forum.example/thread")
                app._replace_entry(app.output_entry, output)
                app._replace_entry(app.cookies_entry, "")
                for selected_index in (1, 2):
                    app._start_download(preview=True)
                    deadline = time.monotonic() + 5
                    while app.preview_window is None and time.monotonic() < deadline:
                        pump(app, 0.1)
                    assert app.preview_window is not None
                    assert len(scans) == 1
                    app.preview_window._select_results(False)
                    app.preview_window.variables[selected_index].set(True)
                    app.preview_window._toggle(selected_index)
                    app.preview_window._confirm(False)
                    deadline = time.monotonic() + 5
                    while app.running and time.monotonic() < deadline:
                        pump(app, 0.1)
                    assert not app.running
                assert downloads == [{items[1].state_key}, {items[2].state_key}]
        finally:
            gui.ThreadDownloader = original_downloader
        app.geometry("980x680+20+20")
        app._set_running_ui(True)
        assert app.preview_button.cget("state") == "disabled"
        app._set_running_ui(False)
        pump(app)
        assert app.start_button.winfo_rootx() + app.start_button.winfo_width() <= app.winfo_rootx() + app.winfo_width()
        metrics = app.failure_button.master
        assert app.failure_button.winfo_rooty() + app.failure_button.winfo_height() <= metrics.winfo_rooty() + metrics.winfo_height()
        snapshot(app, ROOT / "build" / "qa-main-1.3.0.png")
        print("GUI SMOKE PASS: video play/pause/cache/cover/cancel, selection, two downloads/one scan, layout")
        print(f"Screenshot: {shot}")
    finally:
        dialog.close()
        app.destroy()


if __name__ == "__main__":
    run()
