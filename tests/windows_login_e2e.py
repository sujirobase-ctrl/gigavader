"""Drive the real native WebView2 cookie dialog using a synthetic local login."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time

import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import windows_login


class LoginSite(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        signed_in = self.path == "/signed-in"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if signed_in:
            self.send_header("Set-Cookie", "xf_user=fixture; HttpOnly; Max-Age=3600; Path=/")
            self.send_header("Set-Cookie", "xf_session=temporary; HttpOnly; Path=/")
        self.end_headers()
        logged = "true" if signed_in else "false"
        self.wfile.write(f'<html data-logged-in="{logged}"><body>Login QA</body></html>'.encode())


def main():
    if len(sys.argv) > 1:
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(sys.argv[1]).open_embedded_archive("PYZ.pyz")
        assert "src.browser_session" in archive.toc
        exec(archive.extract("src.windows_login"), windows_login.__dict__)
    server = ThreadingHTTPServer(("127.0.0.1", 0), LoginSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}/"
    # Only the harness permits its local HTTP fixture; production trusts SimpCity HTTPS.
    windows_login.trusted_origin = lambda url: origin if url.startswith(origin) else None
    windows_login.FORUM_HOSTS = {"127.0.0.1"}
    window = webview.create_window("BUIDL cookie QA", html="<p>Login QA</p>")
    errors = []

    def drive():
        from System import Action
        from System.Windows.Forms import Application, Timer

        timer = Timer()
        timer.Interval = 100
        deadline = time.monotonic() + 25
        tested_guest = False

        def interact(_sender, _event):
            nonlocal tested_guest
            dialog = next((form for form in Application.OpenForms if "Entrar no SimpCity" in form.Text), None)
            if dialog is None:
                return
            if time.monotonic() > deadline:
                errors.append("O diálogo não concluiu a captura.")
                timer.Stop()
                dialog.Close()
                return
            toolbar = next(control for control in dialog.Controls if control.GetType().Name == "FlowLayoutPanel")
            button, _, hint = list(toolbar.Controls)
            browser = next(control for control in dialog.Controls if control.GetType().Name == "WebView2")
            if not button.Enabled:
                return
            if browser.CoreWebView2.Source.endswith("/login/"):
                if not tested_guest:
                    tested_guest = True
                    button.PerformClick()
                elif hint.Text.startswith("Conclua o login"):
                    browser.CoreWebView2.Navigate(origin + "signed-in")
            elif browser.CoreWebView2.Source.endswith("/signed-in"):
                button.PerformClick()

        timer.Tick += interact
        try:
            assert window.events.loaded.wait(10), "Janela de teste não carregou"
            window.native.Invoke(Action(timer.Start))
            captured = windows_login.capture_in_window(window.native, origin)
            assert tested_guest and captured, errors
            cookies = {item["name"]: item for item in captured["cookies"]}
            assert cookies["xf_user"]["expires"] > time.time()
            assert cookies["xf_session"]["expires"] is None
            assert cookies["xf_user"]["value"] == "fixture"
            print("PASS WebView2 recusou visitante e capturou cookies HttpOnly e validade.", flush=True)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            window.native.Invoke(Action(timer.Stop))
            window.destroy()

    webview.start(drive, gui="edgechromium", private_mode=True)
    server.shutdown()
    if errors:
        raise RuntimeError(errors)


if __name__ == "__main__":
    main()
