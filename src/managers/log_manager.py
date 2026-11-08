"""Rolling event log rendered as a Rich panel under the progress bars."""

from __future__ import annotations

import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.config import LOG_COLORS, MAX_LOG_ROWS, SESSION_LOG
from src.file_utils import redact_sensitive_urls


class LoggerTable:
    """Keep the last N events on screen and mirror everything to a log file.

    `verbose` controls detail level, not destination: verbose-only events are
    written to the file and shown on screen only when enabled, so the default
    log stays readable while `--verbose` records every resolved link.
    """

    def __init__(
        self,
        max_rows: int = MAX_LOG_ROWS,
        log_file: str = SESSION_LOG,
        *,
        verbose: bool = False,
    ) -> None:
        self.events: deque[tuple[str, str, str]] = deque(maxlen=max_rows)
        self.log_file = Path(log_file)
        self.verbose = verbose
        self._write_failed = False

    def log(
        self,
        event: str,
        details: str,
        *,
        disable_ui: bool = False,
        verbose_only: bool = False,
    ) -> bool:
        """Record an event and append it to the log file.

        Returns True when the event was recorded (False when it was a
        verbose-only event and verbose mode is off).
        """
        if verbose_only and not self.verbose:
            return False

        details = redact_sensitive_urls(details)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.events.append((timestamp, event, details))
        self._write_to_file(timestamp, event, details)

        if disable_ui:
            print(f"[{timestamp}] {event}: {details}")
        return True

    def render_log_panel(self, panel_width: int = 80) -> Panel:
        """Render the events as a bordered panel."""
        table = Table.grid(padding=(0, 1))
        table.add_column(style=LOG_COLORS["timestamp"], no_wrap=True)
        table.add_column(style=LOG_COLORS["event"], no_wrap=True)
        table.add_column(style=LOG_COLORS["details"], overflow="fold")

        for timestamp, event, details in self.events:
            first, *rest = details.split("\n")
            table.add_row(Text(timestamp), Text(event), Text(first))
            for line in rest:
                table.add_row(Text(""), Text(""), Text(line))

        return Panel(
            table,
            title=f"[bold {LOG_COLORS['event']}]Log Messages",
            border_style=LOG_COLORS["border"],
            padding=(1, 1),
            width=panel_width,
        )

    def _write_to_file(self, timestamp: str, event: str, details: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d")
        flat = details.replace("\n", " | ")
        try:
            parent = self.log_file.parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp} {timestamp}] {event}: {flat}\n")
        except OSError as error:
            # Warn once: a silently missing log is worse than a noisy one.
            if not self._write_failed:
                self._write_failed = True
                print(
                    f"Warning: cannot write to log file '{self.log_file}': {error}",
                    file=sys.stderr,
                )
