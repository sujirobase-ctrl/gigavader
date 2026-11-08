"""First-party login in an isolated WebView2 with no app bridge."""

from __future__ import annotations

from tempfile import TemporaryDirectory

from src.browser_session import FORUM_HOSTS, trusted_origin


def capture_in_window(owner, origin):
    from System import Action, String
    from System.Collections.Generic import List
    from System.Drawing import Size
    from System.Threading.Tasks import Task, TaskScheduler
    from System.Windows.Forms import Button, DialogResult, DockStyle, FlowLayoutPanel, Form, Label
    from Microsoft.Web.WebView2.Core import CoreWebView2Cookie, CoreWebView2PermissionState
    from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2

    result = []

    def show():
        with TemporaryDirectory(prefix="buidl-login-", ignore_cleanup_errors=True) as profile:
            dialog = Form()
            dialog.Text = "BUIDL · Entrar no SimpCity"
            dialog.Size = Size(1040, 800)
            toolbar = FlowLayoutPanel()
            toolbar.Dock = DockStyle.Top
            toolbar.Height = 58
            save = Button()
            save.Text = "Salvar sessão"
            save.AutoSize = True
            save.Enabled = False
            cancel = Button()
            cancel.Text = "Cancelar"
            cancel.AutoSize = True
            cancel.DialogResult = DialogResult.Cancel
            dialog.CancelButton = cancel
            hint = Label()
            hint.AutoSize = True
            hint.Text = "Entre no SimpCity e clique em Salvar sessão."
            toolbar.Controls.Add(save)
            toolbar.Controls.Add(cancel)
            toolbar.Controls.Add(hint)

            browser = WebView2()
            props = CoreWebView2CreationProperties()
            props.UserDataFolder = profile
            props.set_IsInPrivateModeEnabled(True)
            browser.CreationProperties = props
            browser.Dock = DockStyle.Fill
            dialog.Controls.Add(browser)
            dialog.Controls.Add(toolbar)
            toolbar.BringToFront()
            scheduler = TaskScheduler.FromCurrentSynchronizationContext()
            navigation = 0

            def navigation_start(_sender, args):
                nonlocal navigation
                if not trusted_origin(args.Uri):
                    args.Cancel = True
                    hint.Text = "Esta janela permite apenas páginas HTTPS do SimpCity."
                    return
                navigation += 1
                save.Enabled = False

            def initialized(sender, args):
                if not args.IsSuccess:
                    hint.Text = "Não foi possível abrir o WebView2. Use a importação de cookies."
                    return
                core = sender.CoreWebView2
                core.Settings.IsWebMessageEnabled = False
                core.Settings.AreHostObjectsAllowed = False
                core.Settings.AreDevToolsEnabled = False
                if hasattr(core.Profile, "IsPasswordAutosaveEnabled"):
                    core.Profile.IsPasswordAutosaveEnabled = False
                core.NavigationStarting += navigation_start
                core.NavigationCompleted += lambda _s, e: setattr(save, "Enabled", e.IsSuccess and bool(trusted_origin(core.Source)))
                core.NewWindowRequested += lambda _s, e: setattr(e, "Handled", True)
                core.DownloadStarting += lambda _s, e: setattr(e, "Cancel", True)
                core.PermissionRequested += lambda _s, e: setattr(e, "State", CoreWebView2PermissionState.Deny)
                core.Navigate(origin + "login/")

            def capture(_sender, _event):
                core = browser.CoreWebView2
                current = trusted_origin(core.Source)
                if not current:
                    return
                started_at = navigation
                save.Enabled = False
                hint.Text = "Conferindo se o login terminou…"

                def page_unchanged():
                    return not dialog.IsDisposed and navigation == started_at and trusted_origin(core.Source) == current

                def failed(message):
                    if page_unchanged():
                        save.Enabled = True
                        hint.Text = message

                def got_cookies(task):
                    if not page_unchanged():
                        return
                    if task.IsFaulted or task.IsCanceled:
                        failed("Não foi possível ler os cookies. Tente novamente.")
                        return
                    cookies = [{
                        "name": cookie.Name, "value": cookie.Value, "domain": cookie.Domain.lower(),
                        "path": cookie.Path, "expires": None if cookie.IsSession else
                        int(cookie.Expires.ToFileTimeUtc()) // 10_000_000 - 11_644_473_600,
                    } for cookie in task.Result if cookie.Domain.lower().removeprefix(".") in FORUM_HOSTS]
                    if not cookies:
                        failed("Nenhum cookie da sessão foi encontrado.")
                        return
                    result.append({"origin": current, "agent": core.Settings.UserAgent, "cookies": cookies})
                    dialog.Close()

                def checked_login(task):
                    if not page_unchanged():
                        return
                    if task.IsFaulted or task.IsCanceled or task.Result != "true":
                        failed("Conclua o login e espere a página da conta carregar.")
                        return
                    core.CookieManager.GetCookiesAsync(current).ContinueWith(
                        Action[Task[List[CoreWebView2Cookie]]](got_cookies), scheduler)

                script = ("document.documentElement.dataset.loggedIn === 'true' || "
                          "!!document.querySelector('.p-navgroup-link--user')")
                core.ExecuteScriptAsync(script).ContinueWith(Action[Task[String]](checked_login), scheduler)

            save.Click += capture
            browser.CoreWebView2InitializationCompleted += initialized
            try:
                browser.EnsureCoreWebView2Async(None)
                dialog.ShowDialog(owner)
            finally:
                dialog.Dispose()

    owner.Invoke(Action(show))
    return result[0] if result else None
