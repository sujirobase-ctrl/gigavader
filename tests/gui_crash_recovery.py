"""Kill a GUI mid-transfer, then resume in the actual packaged executable.

The second process displays the normal recovery question; answer Yes through
the UI. A localhost server checks hashes, Range resumption and skipped files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def child(config_path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    os.environ["BUIDL_RECOVERY_DIR"] = config["recovery"]
    for variable, folders in (("TCL_LIBRARY", ("tcl8.6", "tcl_library")),
                              ("TK_LIBRARY", ("tk8.6", "tk_library"))):
        for folder in folders:
            library = ROOT / "build" / "tk-runtime" / folder
            if library.is_dir():
                os.environ[variable] = str(library)
                break
    if config.get("resume_executable"):
        from tests.gui_local_e2e import load_executable
        gui = load_executable(Path(config["resume_executable"]))
        def answer(title, message, **kwargs):
            (config_path.parent / "recovery-question.json").write_text(json.dumps({"title": title, "message": message, "answer": True}), encoding="utf-8")
            return True
        gui.messagebox.askyesnocancel = answer
    else:
        import gui
    app = gui.BuidlApp()
    def start():
        for entry, value in [(app.url_entry, config["url"]), (app.output_entry, config["output"]),
                             (app.cookies_entry, ""), (app.delay_entry, "0"),
                             (app.workers_entry, "2"), (app.connections_entry, "1")]:
            app._replace_entry(entry, value)
        app.start_button.invoke()
    if config.get("resume_executable"):
        def finish():
            if (config_path.parent / "recovery-question.json").exists() and not app.running:
                from tests.gui_preview_smoke import snapshot
                snapshot(app, config_path.parent / "recovered-gui.png")
                app.destroy()
            else:
                app.after(100, finish)
        app.after(500, finish)
    else:
        app.after(100, start)
    app.mainloop()


def parent(args):
    from src.file_utils import load_state
    from src.recovery import RecoveryStore
    args.evidence.mkdir(parents=True, exist_ok=True)
    workspace = args.evidence / "runtime"
    if workspace.exists():
        raise ValueError("Use a new evidence folder for each crash test.")
    workspace.mkdir()
    blobs = {i: hashlib.sha256(str(i).encode()).digest() * 16384 for i in range(20)}
    requests = []
    phase = ["before-crash"]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path.startswith("/threads/"):
                base = f"http://127.0.0.1:{self.server.server_port}"
                links = ''.join(f'<a href="{base}/{i}.zip">{i}</a>' for i in blobs)
                data = (f'<h1 class="p-title-value">Crash QA</h1><article class="message" id="post-1">'
                        f'<div class="message-body">{links}</div></article>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            match = re.fullmatch(r"/(\d+)\.zip", self.path)
            if not match:
                self.send_error(404)
                return
            number = int(match[1])
            data = blobs[number]
            start = 0
            header = self.headers.get("Range")
            if header:
                start = int(header[6:].split('-')[0])
            requests.append({"file": number, "range": header, "phase": phase[0]})
            self.send_response(206 if header else 200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data) - start))
            self.send_header("Accept-Ranges", "bytes")
            if header:
                self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
            self.end_headers()
            try:
                for offset in range(start, len(data), 16384):
                    self.wfile.write(data[offset:offset+16384])
                    self.wfile.flush()
                    time.sleep(.02)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output, recovery = workspace / "downloads", workspace / "recovery"
    config = {"url": f"http://127.0.0.1:{server.server_port}/threads/crash", "output": str(output),
              "recovery": str(recovery)}
    config_path = workspace / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    first = second = None
    try:
        with (workspace / "first-process.log").open("w", encoding="utf-8") as log:
            first = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                                      "--child", str(config_path)], stdout=log, stderr=log, cwd=workspace)
            root = output / "Crash QA"
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                assert first.poll() is None, "Initial GUI exited unexpectedly"
                state = load_state(root)
                completed = {int(Path(value["path"]).stem) for value in state.values()
                             if isinstance(value, dict) and value.get("status") == "completed"}
                parts = [p for p in root.rglob("*.part") if p.stat().st_size > 0]
                if completed and parts:
                    break
                time.sleep(.03)
            else:
                raise AssertionError("Did not reach checkpoint plus partial download")
            first.kill()
            first.wait(timeout=10)
        before = load_state(root)
        completed = {int(Path(value["path"]).stem) for value in before.values()
                     if isinstance(value, dict) and value.get("status") == "completed"}
        assert 0 < len(completed) < 20
        assert RecoveryStore(recovery).pending(), "Recovery snapshot was not saved before the crash"
        assert parts, "No partial file survived the crash"
        print(f"FORCED CRASH VERIFIED: {len(completed)} completed; partial files preserved", flush=True)
        phase[0] = "after-restart"
        env = dict(os.environ, BUIDL_RECOVERY_DIR=str(recovery))
        if args.automatic:
            config["resume_executable"] = str(args.executable.resolve())
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with (workspace / "recovered-process.log").open("w", encoding="utf-8") as log:
                second = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                                           "--child", str(config_path)], cwd=workspace, env=env, stdout=log, stderr=log)
            print("AUTOMATIC CONFIRMATION: compiled GUI/src code loaded from EXE", flush=True)
        else:
            second = subprocess.Popen([str(args.executable.resolve())], cwd=workspace, env=env)
            print("READY_FOR_GUI_CONFIRMATION: answer Yes in the packaged GUI", flush=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = load_state(root)
            all_completed = [v for v in state.values() if isinstance(v, dict) and v.get("status") == "completed"]
            if len(all_completed) == 20 and not RecoveryStore(recovery).pending():
                break
            time.sleep(.1)
        else:
            raise AssertionError("Recovery did not finish in the packaged GUI")
        actual = list(root.rglob('*.zip'))
        assert len(actual) == 20, "Duplicate or missing output files"
        for path in actual:
            assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(blobs[int(path.stem)]).digest()
        assert any(r["range"] for r in requests if r["phase"] == "after-restart"), "Partial files were not resumed with Range"
        assert not any(r["file"] in completed for r in requests if r["phase"] == "after-restart"), "Completed files were re-downloaded"
        report = {"status": "PASS", "forced_process_termination": True, "completed_before_crash": len(completed),
                  "partial_files_before_crash": len(parts), "files_after_recovery": 20,
                  "all_hashes_match": True, "range_resume": True, "no_redownload_of_completed_files": True,
                  "recovery_marker_cleared_on_success": True, "actual_executable": str(args.executable),
                  "confirmation_mode": "compiled GUI with automated answer" if args.automatic else "native executable dialog",
                  "requests": requests}
        (args.evidence / "crash-recovery-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("CRASH RECOVERY PASS: 20 correct hashes; no duplicates", flush=True)
    finally:
        if first is not None and first.poll() is None:
            first.kill()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--automatic', action='store_true')
    args = parser.parse_args()
    if args.child:
        child(args.child)
    else:
        parent(args)
