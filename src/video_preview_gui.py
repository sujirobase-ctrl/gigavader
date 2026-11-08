"""Tk playback controls for bounded, silent video previews."""

from __future__ import annotations

import queue
from threading import Event

import customtkinter as ctk

from src.video_preview import VideoPreview, fetch_video_preview


class VideoPreviewWindow(ctk.CTkToplevel):
    def __init__(self, parent, media, cookies_file, cover, cached, pool, on_result):
        super().__init__(parent)
        self.gallery = parent
        self.media = media
        self.cookies_file = cookies_file
        self.pool = pool
        self.on_result = on_result
        self.stop = Event()
        self.results = queue.Queue()
        self.closed = False
        self.playing = False
        self.frame_index = 0
        self.result = VideoPreview()
        self.frame_image = None
        self.poll_timer = self.play_timer = self.future = None
        self.title("BUIDL — Prévia do vídeo")
        self.geometry("650x500")
        self.resizable(False, False)
        self.configure(fg_color="#0D0F17")
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        ctk.CTkLabel(self, text=media.filename, font=("Segoe UI", 17, "bold"),
                     wraplength=600, height=55).pack(padx=20, pady=(12, 5))
        self.screen = ctk.CTkLabel(self, text="▶\nVídeo · capa indisponível", width=600,
                                   height=290, fg_color="#151823", corner_radius=12,
                                   text_color="#9CA5BD", font=("Segoe UI", 20))
        self.screen.pack(padx=20)
        if cover is not None:
            self._display(cover)
        self.status = ctk.CTkLabel(self, text="", wraplength=600, height=44,
                                   text_color="#9CA5BD")
        self.status.pack(padx=20, pady=5)
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(pady=5)
        self.play_button = ctk.CTkButton(controls, text="Reproduzir", width=145,
                                        command=self._toggle, state="disabled", fg_color="#6C63FF")
        self.play_button.pack(side="left", padx=5)
        self.retry_button = ctk.CTkButton(controls, text="Tentar novamente", width=145,
                                         command=self._load, state="disabled", fg_color="#303750")
        self.retry_button.pack(side="left", padx=5)
        ctk.CTkButton(controls, text="Voltar à galeria", width=145, command=self.close,
                     fg_color="#303750").pack(side="left", padx=5)
        if cached is not None:
            self._accept(cached)
        else:
            self._load()

    def _load(self):
        if self.closed or (self.future is not None and not self.future.done()):
            return
        self._pause()
        self.play_button.configure(state="disabled")
        self.retry_button.configure(state="disabled")
        self.status.configure(text="Carregando um trecho curto… Fechar cancela a prévia.")
        self.future = self.pool.submit(self._fetch)
        self.poll_timer = self.after(80, self._poll)

    def _fetch(self):
        try:
            result = fetch_video_preview(self.media, self.cookies_file, self.stop)
        except Exception:
            result = VideoPreview()
        if not self.stop.is_set():
            self.results.put(result)

    def _poll(self):
        self.poll_timer = None
        if self.closed:
            return
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.poll_timer = self.after(80, self._poll)
            return
        self.on_result(result)
        self._accept(result)

    def _accept(self, result):
        self.result = result
        self.frame_index = 0
        self.status.configure(text=result.message)
        self.retry_button.configure(state="normal")
        if result.frames:
            self._display(result.frames[0])
        if len(result.frames) > 1:
            self.play_button.configure(state="normal")
            self._toggle()

    def _display(self, frame):
        scale = min(560 / frame.width, 280 / frame.height, 2)
        size = (int(frame.width * scale), int(frame.height * scale))
        self.frame_image = ctk.CTkImage(light_image=frame, dark_image=frame, size=size)
        self.screen.configure(text="", image=self.frame_image)

    def _pause(self):
        self.playing = False
        if self.play_timer is not None:
            self.after_cancel(self.play_timer)
            self.play_timer = None
        self.play_button.configure(text="Reproduzir")

    def _toggle(self):
        if self.closed or len(self.result.frames) < 2:
            return
        if self.playing:
            self._pause()
        else:
            self.playing = True
            self.play_button.configure(text="Pausar")
            self._tick()

    def _tick(self):
        self.play_timer = None
        if self.closed or not self.playing:
            return
        self._display(self.result.frames[self.frame_index])
        self.frame_index += 1
        if self.frame_index == len(self.result.frames):
            self.frame_index = 0
            self._pause()
            self.play_button.configure(text="Repetir trecho")
            return
        self.play_timer = self.after(round(1000 / self.result.fps), self._tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self._pause()
        if self.future is not None:
            self.future.cancel()
        if self.poll_timer is not None:
            self.after_cancel(self.poll_timer)
        self.grab_release()
        self.destroy()
        if not self.gallery.closed:
            self.gallery.grab_set()
