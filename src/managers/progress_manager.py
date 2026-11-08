"""Progress bars: an overall panel plus a per-file panel, side by side."""

from __future__ import annotations

import shutil
import threading

from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from src.config import (
    PROGRESS_COLUMNS_SEPARATOR,
    PROGRESS_MANAGER_COLORS,
    ProgressConfig,
)


class ProgressManager:
    """Track an overall task (a post/thread) and its individual file tasks."""

    def __init__(self, task_name: str, item_description: str) -> None:
        self.config = ProgressConfig(task_name, item_description)
        self.overall_progress = self._create_progress_bar()
        self.task_progress = self._create_progress_bar(show_time=True)
        self.num_tasks = 0
        self.current_overall_task_id: TaskID | None = None
        self._buffered_overall_task_ids: set[TaskID] = set()
        self._lock = threading.Lock()

    def get_panel_width(self) -> int:
        """Return the configured panel width."""
        return self.config.panel_width

    def add_overall_task(self, description: str, num_tasks: int) -> None:
        """Register the overall progress bar for a post/album."""
        with self._lock:
            self.num_tasks = num_tasks
            self.current_overall_task_id = self.overall_progress.add_task(
                f"[{self.config.color}]{self._adjust_description(description)}",
                total=max(num_tasks, 1),
                completed=0,
            )

    def add_task(self, current_task: int = 0, total: int = 100) -> TaskID:
        """Register an individual file progress bar."""
        description = (
            f"[{self.config.color}]{self.config.item_description} "
            f"{current_task + 1}/{self.num_tasks}"
        )
        return self.task_progress.add_task(description, total=total)

    def update_task(
        self,
        task_id: TaskID,
        completed: int | None = None,
        advance: int = 0,
        *,
        visible: bool = True,
    ) -> None:
        """Advance a file task and roll the change into the overall bar."""
        with self._lock:
            self.task_progress.update(
                task_id,
                completed=completed if completed is not None else None,
                advance=advance if completed is None else None,
                visible=visible,
            )
            self._update_overall_task(task_id)

    def create_progress_table(self, min_panel_width: int = 30) -> Table:
        """Build the two-panel progress grid."""
        terminal_width, _ = shutil.get_terminal_size()
        panel_width = max(min_panel_width, terminal_width // 2)
        self.config.panel_width = panel_width

        progress_table = Table.grid()
        progress_table.add_row(
            Panel.fit(
                self.overall_progress,
                title=f"[bold {self.config.color}]Overall Progress",
                border_style=PROGRESS_MANAGER_COLORS["overall_border_color"],
                padding=(1, 1),
                width=panel_width,
            ),
            Panel.fit(
                self.task_progress,
                title=f"[bold {self.config.color}]{self.config.task_name} Progress",
                border_style=PROGRESS_MANAGER_COLORS["task_border_color"],
                padding=(1, 1),
                width=panel_width,
            ),
        )
        return progress_table

    # Private helpers
    def _update_overall_task(self, task_id: TaskID) -> None:
        overall_task_id = self.current_overall_task_id
        if overall_task_id is None:
            return

        current = self._get_overall_task(overall_task_id)
        if current is None:
            self.current_overall_task_id = None
            return

        if self.task_progress.tasks[task_id].finished:
            self.overall_progress.advance(overall_task_id)
            self.task_progress.update(task_id, visible=False)
            current = self._get_overall_task(overall_task_id)

        if (
            current is not None
            and current.finished
            and overall_task_id not in self._buffered_overall_task_ids
        ):
            self._buffered_overall_task_ids.add(overall_task_id)
            self.config.overall_buffer.append(current)

        self._cleanup_completed_overall_tasks()

    def _get_overall_task(self, task_id: TaskID):  # noqa: ANN202
        return next(
            (task for task in self.overall_progress.tasks if task.id == task_id),
            None,
        )

    def _cleanup_completed_overall_tasks(self) -> None:
        if len(self.config.overall_buffer) == self.config.overall_buffer.maxlen:
            completed_id = self.config.overall_buffer.popleft().id
            self.overall_progress.remove_task(completed_id)
            self._buffered_overall_task_ids.discard(completed_id)
            if completed_id == self.current_overall_task_id:
                self.current_overall_task_id = None

    @staticmethod
    def _adjust_description(description: str, max_length: int = 24) -> str:
        return (
            description[:max_length] + "..."
            if len(description) > max_length
            else description
        )

    @staticmethod
    def _create_progress_bar(*, show_time: bool = False) -> Progress:
        columns = [
            SpinnerColumn(),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        ]
        if show_time:
            columns += [PROGRESS_COLUMNS_SEPARATOR, TimeRemainingColumn()]
        return Progress("{task.description}", *columns)
