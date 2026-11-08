"""Interactive cookie setup: ask for a cookies.txt, validate it, remember it.

Forums behind a login return 403 to anonymous requests, so the CLI asks for a
cookies file on startup instead of failing halfway through a run. The chosen
path is remembered in a small JSON file so the prompt only appears once.
"""

from __future__ import annotations

import json
import os
import sys
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

CONFIG_DIR = Path.home() / ".forumpostdownloader"
CONFIG_FILE = CONFIG_DIR / "config.json"

BROWSER_HELP = """[bold]How to export cookies[/bold]

  1. Install a cookies.txt extension in the browser where you are logged in:
     • Chrome/Edge — "Get cookies.txt LOCALLY"
     • Firefox     — "cookies.txt"
  2. Open the forum and make sure you are logged in.
  3. Click the extension → Export / Download → save the file.
  4. Paste the full path to that file below.

[dim]The file must be Netscape format with TAB-separated fields.[/dim]"""


def load_saved_path() -> str | None:
    """Return the remembered cookies path, if it still exists."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    saved = data.get("cookies_file")
    if saved and Path(saved).is_file():
        return saved
    return None


def save_path(cookies_file: str) -> None:
    """Remember the cookies path for future runs."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data["cookies_file"] = str(Path(cookies_file).expanduser().resolve())
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def forget_path() -> None:
    """Drop the remembered cookies path."""
    try:
        if not CONFIG_FILE.exists():
            return
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        data.pop("cookies_file", None)
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def validate(cookies_file: str) -> tuple[bool, str]:
    """Check a cookies file. Returns (ok, message)."""
    path = Path(cookies_file).expanduser()

    if not path.exists():
        return False, f"File not found: {path}"
    if path.is_dir():
        return False, f"That is a directory, not a file: {path}"

    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except LoadError as error:
        return False, (
            f"Not a valid Netscape cookies.txt ({error}). "
            "Fields must be TAB-separated — re-export from the extension."
        )
    except OSError as error:
        return False, f"Could not read the file: {error}"

    cookies = list(jar)
    if not cookies:
        return False, "The file parsed but contains no cookies. Re-export it."

    domains = sorted({c.domain.lstrip('.') for c in cookies})
    preview = ", ".join(domains[:4]) + ("…" if len(domains) > 4 else "")
    return True, f"{len(cookies)} cookies for {len(domains)} domain(s): {preview}"


def normalize(cookies_file: str) -> str:
    """Expand ~, strip quotes a drag-and-drop shell adds, and resolve."""
    cleaned = cookies_file.strip().strip('"').strip("'")
    return str(Path(cleaned).expanduser())


def can_prompt() -> bool:
    """Whether we can realistically ask the user something.

    stdin.isatty() alone is too strict: piping answers in (`printf ... |`),
    or running under a wrapper that redirects stdin, still leaves a usable
    stdin — we can read a line even though it is not a TTY. What we cannot
    do is prompt when stdin is closed or when output is being captured with
    no terminal at all on either stream.
    """
    if os.environ.get("FPD_NO_PROMPT"):
        return False
    try:
        if sys.stdin is None or sys.stdin.closed:
            return False
    except (AttributeError, ValueError):
        return False
    return sys.stdin.isatty() or sys.stdout.isatty() or sys.stderr.isatty()


def prompt_for_cookies(
    console: Console | None = None,
    *,
    required: bool = False,
) -> str | None:
    """Ask the user for a cookies.txt path.

    Returns the validated path, or None if the user chooses to continue
    without cookies (only allowed when `required` is False).
    """
    console = console or Console()

    if not can_prompt():
        return None

    console.print()
    console.print(
        Panel(
            BROWSER_HELP,
            title="[bold light_cyan3]Cookies needed",
            border_style="bright_blue",
            padding=(1, 2),
        ),
    )

    while True:
        suffix = "" if required else "  [dim](or press Enter to skip)[/dim]"
        console.print(f"[light_cyan3]Path to cookies.txt[/light_cyan3]{suffix}: ", end="")
        try:
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None

        if not raw:
            if required:
                console.print("[yellow]  A cookies file is required for this site.[/yellow]")
                continue
            console.print("[dim]  Continuing without cookies.[/dim]\n")
            return None

        candidate = normalize(raw)
        ok, message = validate(candidate)
        if ok:
            console.print(f"[green]  OK[/green] — {message}\n")
            return candidate

        console.print(f"[red]  {message}[/red]")


def resolve_cookies(
    cli_value: str | None,
    console: Console | None = None,
    *,
    ask: bool = True,
    remember: bool = True,
) -> str | None:
    """Work out which cookies file to use.

    Order: --cookies flag → remembered path → interactive prompt.
    """
    console = console or Console()

    if cli_value:
        ok, message = validate(normalize(cli_value))
        if not ok:
            console.print(f"[red]Cookies file rejected: {message}[/red]")
            sys.exit(2)
        path = normalize(cli_value)
        if remember:
            save_path(path)
        return path

    saved = load_saved_path()
    if saved:
        ok, _ = validate(saved)
        if ok:
            return saved
        forget_path()

    if not ask:
        return None

    chosen = prompt_for_cookies(console)
    if chosen and remember:
        save_path(chosen)
    return chosen
