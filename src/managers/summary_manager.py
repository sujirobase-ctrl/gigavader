"""Aggregate per-task outcomes so the run can end with a summary block."""

from __future__ import annotations

from collections import defaultdict
from enum import IntEnum

from src.config import TASK_REASON_MAPPING, TaskResult


class SummaryManager:
    """Count task results and their reasons."""

    def __init__(self) -> None:
        self._totals: dict[TaskResult, int] = defaultdict(int)
        self._reasons: dict[tuple[TaskResult, int], int] = defaultdict(int)

    def update_result(self, task_result: TaskResult, reason: IntEnum | None = None) -> None:
        """Record one finished task."""
        self._totals[task_result] += 1
        if reason is not None:
            self._reasons[(task_result, int(reason))] += 1

    def get_result_count(
        self,
        task_result: TaskResult,
        reason: IntEnum | None = None,
    ) -> int:
        """Return the number of tasks with this result (optionally by reason)."""
        if reason is None:
            return self._totals[task_result]
        return self._reasons[(task_result, int(reason))]

    def has_reasons(self, task_result: TaskResult) -> bool:
        """Whether this result type carries a reason breakdown."""
        return task_result in TASK_REASON_MAPPING
