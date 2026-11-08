"""Combine progress table, log panel and footer into one live-updating view."""

from __future__ import annotations

import datetime
import threading
import time
from contextlib import nullcontext
from enum import IntEnum

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from src.config import (
    REFRESH_PER_SECOND,
    SESSION_LOG,
    TASK_REASON_MAPPING,
    TaskResult,
    get_version_string,
)

from .log_manager import LoggerTable
from .progress_manager import ProgressManager
from .summary_manager import SummaryManager


class LiveManager:
    """Own the Rich Live context and expose a small facade to the downloaders."""

    def __init__(
        self,
        progress_manager: ProgressManager,
        logger_table: LoggerTable,
        summary_manager: SummaryManager,
        *,
        disable_ui: bool = False,
    ) -> None:
        self.progress_manager = progress_manager
        self.progress_table = self.progress_manager.create_progress_table()
        self.logger_table = logger_table
        self.summary_manager = summary_manager
        self.disable_ui = disable_ui
        self._live_lock = threading.Lock()
        self._started = False

        # A real console keeps the layout working; when stdout is not a
        # terminal (redirected / wrapped) Rich cannot update in place, so the
        # Live view would print one frame per event and stack vertically. In
        # that case we silently fall back to plain line-by-line logging.
        self.console = Console(highlight=False)
        if not self.console.is_terminal:
            self.disable_ui = True

        self.live = (
            Live(
                self._render_live_view(),
                refresh_per_second=REFRESH_PER_SECOND,
                console=self.console,
                screen=True,
                auto_refresh=True,
                redirect_stderr=True,
            )
            if not self.disable_ui
            else nullcontext()
        )
        self.start_time = time.time()
        self.update_log(
            event="Script started",
            details="The script has started execution.",
        )

    def add_overall_task(self, description: str, num_tasks: int) -> None:
        """Proxy to ProgressManager."""
        self.progress_manager.add_overall_task(description, num_tasks)

    def add_task(self, current_task: int = 0, total: int = 100):  # noqa: ANN201
        """Proxy to ProgressManager."""
        return self.progress_manager.add_task(current_task, total)

    def update_task(
        self,
        task_id,  # noqa: ANN001
        completed: int | None = None,
        advance: int = 0,
        *,
        visible: bool = True,
    ) -> None:
        """Proxy to ProgressManager."""
        self.progress_manager.update_task(task_id, completed, advance, visible=visible)

    def update_log(self, *, event: str, details: str, verbose_only: bool = False) -> None:
        """Append an event to the log table (and the log file).

        Deliberately does NOT call ``live.update()`` here. The Rich ``Live``
        runs with ``auto_refresh=True``, so its own refresh tick repaints the
        whole view (progress + log + footer) on a fixed cadence. Having
        ``update_log`` also push an immediate ``update()`` creates a *second*
        draw path that interleaves with the tick and, under bursts of events
        from the download workers, is what stacks/duplicates the panels. The
        single tick-driven repaint is the only draw path and cannot stack.
        """
        self.logger_table.log(
            event,
            details,
            disable_ui=self.disable_ui,
            verbose_only=verbose_only,
        )

    def update_summary(self, task_result: TaskResult, reason: IntEnum | None = None) -> None:
        """Record a finished task in the summary."""
        self.summary_manager.update_result(task_result, reason)

    def start(self) -> None:
        """Enter the live display."""
        if not self.disable_ui:
            self._started = True
            self.live.start()

    def stop(self) -> None:
        """Log timing plus the results summary, then leave the live display."""
        execution_time = self._compute_execution_time()
        self.update_log(
            event="Script ended",
            details=(
                "The script has finished execution.\n"
                f"Execution time: {execution_time}"
            ),
        )
        self._log_results_summary()
        if not self.disable_ui:
            with self._live_lock:
                # Repaint the final frame (Script ended + summary) once,
                # atomically, so it is visible in the alternate screen before
                # we tear it down. Then stop() leaves the alternate buffer.
                self.live.refresh()
                self.live.stop()
            # Re-print the final frame on the normal screen so the summary
            # stays visible after we leave the alternate screen buffer.
            self.console.print(self._render_live_view())
            self._started = False

    # Private helpers
    def _render_live_view(self) -> Group:
        panel_width = self.progress_manager.get_panel_width()
        footer = Align.left(Text(get_version_string(), style="dim"))
        return Group(
            self.progress_table,
            self.logger_table.render_log_panel(panel_width=2 * panel_width),
            footer,
        )

    def _compute_execution_time(self) -> str:
        delta = datetime.timedelta(seconds=time.time() - self.start_time)
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        seconds = delta.seconds % 60
        return f"{hours:02} hrs {minutes:02} mins {seconds:02} secs"

    def _log_results_summary(self) -> None:
        max_len = max(len(result.name) for result in TaskResult)
        details: list[str] = []

        for task_result in TaskResult:
            count = self.summary_manager.get_result_count(task_result)
            details.append(f"{task_result.name.capitalize():<{max_len}}: {count}")

            reason_class = TASK_REASON_MAPPING.get(task_result)
            if reason_class is None or len(reason_class) <= 1:
                continue

            for reason in reason_class:
                num = self.summary_manager.get_result_count(task_result, reason)
                if num > 0:
                    label = reason.name.replace("_", " ").capitalize()
                    details.append(f"- {label}: {num}")

        self.update_log(event="Results summary", details="\n".join(details))


def initialize_managers(
    *,
    disable_ui: bool = False,
    log_file: str = SESSION_LOG,
    verbose: bool = False,
) -> LiveManager:
    """Build the standard manager trio used by the CLIs."""
    progress_manager = ProgressManager(task_name="File", item_description="File")
    return LiveManager(
        progress_manager,
        LoggerTable(log_file=log_file, verbose=verbose),
        SummaryManager(),
        disable_ui=disable_ui,
    )
