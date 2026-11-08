"""Run the real GUI callbacks against android/qa_server.py on localhost.

Unlike the preview smoke, this does not replace the downloader or HTTP client.
An optional EXE supplies the compiled GUI and src modules tested here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.util
import json
import marshal
import os
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_executable(path):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(path))
    pyz = archive.open_embedded_archive("PYZ.pyz")

    class PackagedSource(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "src" or fullname.startswith("src."):
                if fullname in pyz.toc:
                    return importlib.util.spec_from_loader(fullname, self, is_package=pyz.toc[fullname][0] == 1)
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            module.__file__ = str(ROOT / (module.__name__.replace(".", "/") + ".py"))
            exec(pyz.extract(module.__name__), module.__dict__)

    sys.meta_path.insert(0, PackagedSource())
    module = types.ModuleType("gui")
    module.__file__ = str(ROOT / "gui.py")
    sys.modules["gui"] = module
    exec(marshal.loads(archive.extract("gui")), module.__dict__)
    return module


def run(args):
    args.evidence.mkdir(parents=True, exist_ok=True)
    args.downloads.mkdir(parents=True, exist_ok=True)
    if any(args.downloads.iterdir()):
        raise ValueError("Use a new empty downloads directory for this QA run.")
    for variable, folders in (("TCL_LIBRARY", ("tcl8.6", "tcl_library")),
                              ("TK_LIBRARY", ("tk8.6", "tk_library"))):
        for folder in folders:
            library = ROOT / "build" / "tk-runtime" / folder
            if library.is_dir():
                os.environ[variable] = str(library)
                break
    if args.executable:
        gui = load_executable(args.executable)
    else:
        import gui
    import customtkinter as ctk
    import requests
    from tests.gui_preview_smoke import pump, snapshot

    checks, errors = [], []
    gui.messagebox.showerror = lambda title, message, **kwargs: errors.append((title, message))
    app = gui.BuidlApp()
    app.geometry("1120x760+30+30")

    def check(label, condition):
        assert condition, label
        checks.append(label)
        print("PASS:", label, flush=True)

    def wait(predicate, label, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            pump(app, .05)
            assert not errors, errors
        check(label, bool(predicate()))

    def open_gallery():
        app.preview_button.invoke()
        wait(lambda: app.preview_window is not None, "Galeria aberta pela interface")
        pump(app, .5)
        return app.preview_window

    try:
        pump(app, .6)
        check("Janela inicial renderizada", app.winfo_viewable())
        snapshot(app, args.evidence / "01-inicial.png")
        for entry, value in [(app.url_entry, "http://127.0.0.1:8876/threads/qa"),
                             (app.output_entry, str(args.downloads)), (app.cookies_entry, ""),
                             (app.delay_entry, "0"), (app.retries_entry, "0")]:
            app._replace_entry(entry, value)
        app.all_pages.set(True)
        app.flat.set(True)
        gallery = open_gallery()
        items = gallery.model.items
        check("Duas páginas analisadas e seis arquivos encontrados", len(items) == 6 and gallery.prepared.post_count == 2)
        gallery.kind.set("Vídeos")
        gallery._filter_changed()
        check("Filtro de vídeos", len(gallery._filtered()) == 2)
        gallery.kind.set("Todos")
        gallery._filter_changed()
        gallery.query.set("photo1")
        pump(app, .4)
        check("Busca por nome", len(gallery._filtered()) == 1)
        gallery.query.set("")
        pump(app, .4)
        video_index = next(i for i, item in enumerate(items) if item.filename == "video.mp4")
        gallery._open_video(video_index)
        player = gallery.video_window
        wait(lambda: player.playing and player.frame_index > 0, "Prévia de vídeo real reproduzindo", 30)
        player._toggle()
        paused = player.frame_index
        pump(app, .25)
        check("Pausa do vídeo", not player.playing and player.frame_index == paused)
        snapshot(player, args.evidence / "02-video.png")
        player._toggle()
        pump(app, .25)
        check("Retomada do vídeo", player.playing and player.frame_index != paused)
        player.close()
        unavailable = next(i for i, item in enumerate(items) if item.filename == "unavailable.mp4")
        gallery._open_video(unavailable)
        player = gallery.video_window
        wait(lambda: not player.future or player.future.done(), "Prévia indisponível terminou", 30)
        pump(app, .4)
        check("Prévia inválida preserva a galeria", not player.playing and app.last_counts["failed"] == 0)
        player.close()
        selected = {"photo1.jpg", "video.mp4", "pack.zip"}
        gallery._select_results(False)
        for i, item in enumerate(items):
            if item.filename in selected:
                gallery.variables[i].set(True)
                gallery._toggle(i)
        check("Seleção de três arquivos", len(gallery.model.selected) == 3)
        snapshot(gallery, args.evidence / "03-selecao.png")
        gallery._confirm(False)
        wait(lambda: not app.running, "Download da seleção finalizado", 30)
        check("Três downloads sem falhas", app.last_counts == {"completed": 3, "skipped": 0, "failed": 0})
        actual = {p.name: p for p in args.downloads.rglob('*') if p.is_file() and p.suffix in ('.jpg', '.mp4', '.zip')}
        check("Somente os arquivos escolhidos foram salvos", set(actual) == selected)
        hashes = {}
        for name, path in actual.items():
            expected = requests.get('http://127.0.0.1:8876/' + name, timeout=10).content
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            check("Integridade: " + name, hashes[name] == hashlib.sha256(expected).hexdigest())
        snapshot(app, args.evidence / "04-concluido.png")
        gallery = open_gallery()
        check("Seleção preservada ao reabrir", len(gallery.model.selected) == 3)
        gallery._confirm(False)
        wait(lambda: not app.running, "Segunda execução finalizada", 30)
        check("Arquivos existentes ignorados sem duplicação", app.last_counts == {"completed": 0, "skipped": 3, "failed": 0})
        snapshot(app, args.evidence / "05-retomada.png")
        gallery = open_gallery()
        gallery._cancel()
        wait(lambda: not app.running, "Cancelar galeria encerra o processamento")
        check("Cancelamento explícito na interface", "Cancelado" in app.status_label.cget("text"))
        check("Nenhum diálogo inesperado", not errors)
        (args.evidence / "resultado.json").write_text(json.dumps({
            "status": "PASS", "executable": str(args.executable),
            "mode": "Compiled GUI/src code extracted from EXE, real Tk event loop and real HTTP downloads",
            "checks": checks, "hashes": hashes, "downloads": str(args.downloads),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"GUI LOCAL E2E PASS: {len(checks)} checks", flush=True)
    finally:
        if app.preview_window is not None:
            app.preview_window.close()
        app.cancel_event.set()
        app.pause_event.set()
        app.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--downloads", type=Path, required=True)
    run(parser.parse_args())
