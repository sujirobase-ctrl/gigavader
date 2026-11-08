"""Paginated visual selection dialog. No network or decoding runs on Tk."""

from __future__ import annotations

import math
import queue
import threading
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import customtkinter as ctk

from src.downloaders import PreparedThread
from src.preview import PreviewSelection, fetch_thumbnail, media_kind, thumbnail_source
from src.video_preview_gui import VideoPreviewWindow

ACCENT = "#6C63FF"
SURFACE = "#151823"
MUTED = "#9CA5BD"
PAGE_SIZE = 18


class PreviewWindow(ctk.CTkToplevel):
    """Select any subset of a prepared queue, or explicitly download it all."""

    def __init__(self, parent, prepared: PreparedThread, cookies_file, on_confirm, on_cancel,
                 *, session=None, on_refresh=None):
        super().__init__(parent)
        self.prepared = prepared
        self.model = PreviewSelection([media for media, _ in prepared.plan])
        self.session = session
        self.on_refresh = on_refresh
        if session is not None:
            self.model.selected = session.selected
        self.cookies_file = cookies_file
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.closed = False
        self.page = session.page if session is not None else 0
        self.generation = 0
        self.page_stop = threading.Event()
        self.results = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="buidl-preview")
        self.video_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="buidl-video")
        self.video_window = None
        self.videos = session.videos if session is not None else OrderedDict()
        self.futures = []
        self.cache = session.thumbnails if session is not None else OrderedDict()
        self.labels = {}
        self.variables = {}
        self.images = []
        self.query = ctk.StringVar(value=session.query if session is not None else "")
        self.kind = ctk.StringVar(value=session.kind if session is not None else "Todos")
        self.search_timer = None

        self.title("BUIDL — Visualizar e escolher")
        self.geometry("1040x780")
        self.minsize(920, 650)
        self.configure(fg_color="#0D0F17")
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            self, text=prepared.title, font=("Segoe UI", 23, "bold"),
            anchor="w", wraplength=850,
        ).grid(row=0, column=0, padx=24, pady=(18, 2), sticky="ew")
        ctk.CTkLabel(
            self, text=(
                f"{len(prepared.plan)} arquivos encontrados · {prepared.post_count} posts\n"
                "Marque o que deseja baixar. Clique em Prévia do vídeo para ver um trecho sem áudio."
            ), text_color=MUTED, justify="left", anchor="w",
        ).grid(row=1, column=0, padx=24, pady=(0, 10), sticky="ew")

        filters = ctk.CTkFrame(self, fg_color="transparent")
        filters.grid(row=2, column=0, padx=24, sticky="ew")
        filters.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            filters, text="Buscar nome, post ou host", text_color=MUTED, height=20, anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            filters, text="Tipo de arquivo", text_color=MUTED, height=20, anchor="w",
        ).grid(row=0, column=1, sticky="w")
        self.search_entry = ctk.CTkEntry(
            filters, textvariable=self.query, placeholder_text="Buscar nome, post ou host…", height=34,
        )
        self.search_entry.grid(row=1, column=0, padx=(0, 10), sticky="ew")
        if on_refresh is not None:
            self.refresh_button = ctk.CTkButton(
                filters, text="Atualizar thread", width=130, fg_color="#353052",
                command=self._refresh,
            )
            self.refresh_button.grid(row=1, column=2, padx=(10, 0))
        ctk.CTkOptionMenu(
            filters, variable=self.kind, values=["Todos", "Imagens", "Vídeos", "Áudios", "Arquivos"],
            command=lambda _value: self._filter_changed(), fg_color="#252A3D",
        ).grid(row=1, column=1)
        self.query.trace_add("write", self._search_changed)

        selection = ctk.CTkFrame(self, fg_color="transparent")
        selection.grid(row=3, column=0, padx=24, pady=10, sticky="ew")
        selection.grid_columnconfigure(2, weight=1)
        ctk.CTkButton(
            selection, text="Marcar resultados", width=145, fg_color="#252A3D",
            command=lambda: self._select_results(True),
        ).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(
            selection, text="Desmarcar resultados", width=155, fg_color="#252A3D",
            command=lambda: self._select_results(False),
        ).grid(row=0, column=1)
        self.selection_label = ctk.CTkLabel(selection, text="", text_color="#A8A2FF")
        self.selection_label.grid(row=0, column=2, sticky="e")

        self.gallery = ctk.CTkScrollableFrame(self, fg_color=SURFACE, corner_radius=12)
        self.gallery.grid(row=4, column=0, padx=24, sticky="nsew")
        for column in range(3):
            self.gallery.grid_columnconfigure(column, weight=1, uniform="cards")

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=5, column=0, padx=24, pady=(8, 16), sticky="ew")
        footer.grid_columnconfigure(1, weight=1)
        self.previous = ctk.CTkButton(
            footer, text="← Anterior", width=110, fg_color="#252A3D",
            command=lambda: self._change_page(-1),
        )
        self.previous.grid(row=0, column=0)
        self.page_label = ctk.CTkLabel(footer, text="", text_color=MUTED)
        self.page_label.grid(row=0, column=1)
        self.next = ctk.CTkButton(
            footer, text="Próxima →", width=110, fg_color="#252A3D",
            command=lambda: self._change_page(1),
        )
        self.next.grid(row=0, column=2)
        ctk.CTkLabel(
            footer, text=f"Destino: {prepared.root}", text_color=MUTED,
            anchor="w", wraplength=850, justify="left",
        ).grid(row=1, column=0, columnspan=3, pady=(6, 2), sticky="ew")
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        actions.grid(row=2, column=0, columnspan=3, sticky="ew")
        actions.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            actions, text="Voltar sem baixar", width=150, fg_color="#252A3D", command=self._cancel,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            actions, text=f"Baixar todos ({len(prepared.plan)})", width=165,
            fg_color="#353052", command=lambda: self._confirm(True),
        ).grid(row=0, column=1, padx=10)
        self.selected_button = ctk.CTkButton(
            actions, text="Baixar selecionados", width=200, height=38,
            fg_color=ACCENT, command=lambda: self._confirm(False),
        )
        self.selected_button.grid(row=0, column=2)
        self._render_page()
        self.restore_timer = self.after(100, self._restore_scroll)
        self.poll_timer = self.after(80, self._poll)

    def _restore_scroll(self):
        self.restore_timer = None
        if self.session is not None and not self.closed:
            self.gallery._parent_canvas.yview_moveto(self.session.scroll)

    def _filtered(self):
        return self.model.filtered_indices(self.query.get(), self.kind.get())

    def _search_changed(self, *_args):
        if self.search_timer:
            self.after_cancel(self.search_timer)
        self.search_timer = self.after(250, self._filter_changed)

    def _filter_changed(self):
        self.search_timer = None
        self.page = 0
        self._render_page()

    def _change_page(self, delta):
        self.page += delta
        self._render_page()

    def _select_results(self, enabled):
        self.model.select(self._filtered(), enabled)
        for index, variable in self.variables.items():
            variable.set(self.model.items[index].state_key in self.model.selected)
        self._update_selection()

    def _toggle(self, index):
        self.model.select([index], bool(self.variables[index].get()))
        self._update_selection()

    def _update_selection(self):
        count = len(self.model.selected)
        visible = {self.model.items[i].state_key for i in self._filtered()}
        hidden = count - len(self.model.selected & visible)
        self.selection_label.configure(
            text=f"{count} selecionados" + (f" · {hidden} fora do filtro" if hidden else ""),
        )
        self.selected_button.configure(
            text=f"Baixar selecionados ({count})", state="normal" if count else "disabled",
        )

    def _render_page(self):
        self.page_stop.set()
        for future in self.futures:
            future.cancel()
        self.futures.clear()
        self.page_stop = threading.Event()
        self.generation += 1
        for widget in self.gallery.winfo_children():
            widget.destroy()
        self.labels.clear()
        self.variables.clear()
        self.images.clear()
        indices = self._filtered()
        pages = max(1, math.ceil(len(indices) / PAGE_SIZE))
        self.page = max(0, min(self.page, pages - 1))
        self.page_label.configure(text=f"Página {self.page + 1} / {pages} · {len(indices)} resultados")
        self.previous.configure(state="normal" if self.page else "disabled")
        self.next.configure(state="normal" if self.page + 1 < pages else "disabled")
        for slot, index in enumerate(indices[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]):
            self._card(slot, index)
        if not indices:
            ctk.CTkLabel(self.gallery, text="Nenhum arquivo corresponde a este filtro.").grid(
                row=0, column=0, columnspan=3, pady=50,
            )
        self.gallery._parent_canvas.yview_moveto(0)
        self._update_selection()

    def _card(self, slot, index):
        media = self.model.items[index]
        card = ctk.CTkFrame(self.gallery, fg_color="#202537", corner_radius=10)
        card.grid(row=slot // 3, column=slot % 3, padx=6, pady=6, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        variable = ctk.BooleanVar(value=media.state_key in self.model.selected)
        self.variables[index] = variable
        kind = media_kind(media)
        ctk.CTkCheckBox(
            card, text=f"{kind} · {media.post_label or 'Arquivo'}", variable=variable,
            command=lambda: self._toggle(index), fg_color=ACCENT, font=("Segoe UI", 11),
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")
        label = ctk.CTkLabel(
            card, text="Carregando prévia…" if thumbnail_source(media) else (
                "▶\nVídeo · clique em Prévia do vídeo" if kind == "Vídeos" else f"{kind}\nSem prévia disponível"
            ),
            height=150, text_color=MUTED, fg_color="#101521", corner_radius=8,
        )
        label.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        self.labels[index] = label
        name = media.filename or "Nome disponível ao iniciar o download"
        ctk.CTkLabel(
            card, text=name if len(name) <= 100 else name[:97] + "…", wraplength=245,
            height=44, anchor="w", justify="left", font=("Segoe UI", 11),
        ).grid(row=2, column=0, padx=10, sticky="ew")
        source = media.origin_url or media.state_key
        ctk.CTkButton(
            card, text=f"Abrir origem · {urlparse(source).hostname or 'link'}", height=25,
            font=("Segoe UI", 10), fg_color="#303750", command=lambda: self._open_source(source),
        ).grid(row=3, column=0, padx=10, pady=(4, 10), sticky="ew")
        if kind == "Vídeos":
            ctk.CTkButton(
                card, text="▶ Prévia do vídeo", height=28, fg_color=ACCENT,
                command=lambda: self._open_video(index),
            ).grid(row=4, column=0, padx=10, pady=(0, 10), sticky="ew")
        if index in self.cache:
            self._show_image(index, self.cache[index])
            self.cache.move_to_end(index)
        elif thumbnail_source(media):
            self.futures.append(self.pool.submit(
                self._fetch, self.generation, index, media, self.page_stop,
            ))

    def _fetch(self, generation, index, media, stop):
        thumbnail = fetch_thumbnail(media, self.cookies_file, stop)
        if not stop.is_set():
            self.results.put((generation, index, thumbnail))

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                generation, index, thumbnail = self.results.get_nowait()
                if generation != self.generation:
                    continue
                if index in self.videos and self.videos[index].frames:
                    # A slow poster fetch must not replace an already decoded cover.
                    continue
                self.cache[index] = thumbnail
                while len(self.cache) > 96:
                    self.cache.popitem(last=False)
                self._show_image(index, thumbnail)
        except queue.Empty:
            pass
        self.poll_timer = self.after(80, self._poll)

    def _show_image(self, index, thumbnail):
        if index not in self.labels:
            return
        if (thumbnail is not None and self.video_window is not None
                and not self.video_window.closed
                and self.video_window.media is self.model.items[index]
                and not self.video_window.result.frames):
            self.video_window._display(thumbnail)
        if thumbnail is None:
            self.labels[index].configure(text="Prévia indisponível\nO arquivo ainda pode ser baixado")
            return
        image = ctk.CTkImage(light_image=thumbnail, dark_image=thumbnail, size=thumbnail.size)
        self.images.append(image)
        self.labels[index].configure(text="", image=image)

    def _open_video(self, index):
        if self.video_window is not None and not self.video_window.closed:
            self.video_window.lift()
            return
        self.video_window = VideoPreviewWindow(
            self, self.model.items[index], self.cookies_file, self.cache.get(index),
            self.videos.get(index), self.video_pool,
            lambda result: self._video_ready(index, result),
        )

    def _video_ready(self, index, result):
        self.videos[index] = result
        self.videos.move_to_end(index)
        while len(self.videos) > 2:
            self.videos.popitem(last=False)
        if result.frames:
            cover = result.frames[0].copy()
            cover.thumbnail((240, 150))
            self.cache[index] = cover
            while len(self.cache) > 96:
                self.cache.popitem(last=False)
            self._show_image(index, cover)

    @staticmethod
    def _open_source(source):
        if urlparse(source).scheme in {"http", "https"}:
            webbrowser.open(source)

    def _confirm(self, all_files):
        keys = None if all_files else set(self.model.selected)
        if keys is not None and not keys:
            return
        self.close()
        self.on_confirm(keys)

    def _cancel(self):
        self.close()
        self.on_cancel()

    def _refresh(self):
        self.close()
        if self.on_refresh is not None:
            self.on_refresh()

    def close(self):
        if self.closed:
            return
        if self.session is not None:
            self.session.page = self.page
            self.session.query = self.query.get()
            self.session.kind = self.kind.get()
            self.session.scroll = self.gallery._parent_canvas.yview()[0]
        self.closed = True
        if self.video_window is not None:
            self.video_window.close()
        self.video_pool.shutdown(wait=False, cancel_futures=True)
        self.page_stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.search_timer:
            self.after_cancel(self.search_timer)
        if self.restore_timer:
            self.after_cancel(self.restore_timer)
        self.after_cancel(self.poll_timer)
        self.grab_release()
        self.destroy()
