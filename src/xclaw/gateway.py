"""Single-task gateway service."""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from pathlib import Path

from .artifact_store import ArtifactStore
from .executor import StageExecutor, StageOutcome
from .human_io import ensure_supervision_artifacts, publish_progress_update
from .protocol import TaskStatus
from .task_store import TaskStore

_TERMINAL_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.TERMINATED,
)
_WAITING_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.WAITING_APPROVAL,
)


@dataclass(frozen=True)
class GatewayRunConfig:
    """Bootstrap settings for one gateway worker."""

    task_workspace_path: str
    workspace_root: str | None = None
    command_launch_dir: str | None = None
    poll_interval_seconds: float = 1.0


class TaskGateway:
    """Long-running single-task gateway loop."""

    def __init__(self, config: GatewayRunConfig) -> None:
        self.config = config
        self.task_workspace_path = Path(config.task_workspace_path).expanduser().resolve()
        self.task_store = TaskStore(self.task_workspace_path)
        self.artifact_store = ArtifactStore(self.task_workspace_path)
        self.executor = StageExecutor(
            self.task_workspace_path,
            agents_dir=_resolve_launch_dir_child(config.command_launch_dir, "agents"),
            skills_dir=_resolve_launch_dir_child(config.command_launch_dir, "skills"),
        )

    def run_forever(self) -> int:
        context = self.task_store.load_task_context()
        self.task_store.update_runtime_state(gateway_pid=os.getpid())
        ensure_supervision_artifacts(
            task_store=self.task_store,
            artifact_store=self.artifact_store,
        )
        publish_progress_update(
            task_store=self.task_store,
            artifact_store=self.artifact_store,
            latest_update="Gateway worker is online.",
            timeline_title="Worker Online",
            timeline_body=(
                f"- task_id: {context.task_id}\n"
                f"- gateway_pid: {os.getpid()}\n"
                f"- current_stage: {context.current_stage.value}"
            ),
        )

        while True:
            context = self.task_store.load_task_context()
            if context.status in _TERMINAL_STATUSES:
                self._finalize_terminal_state(context.status)
                return 0 if context.status == TaskStatus.COMPLETED else 1

            if context.status in _WAITING_STATUSES:
                time.sleep(self.config.poll_interval_seconds)
                continue

            outcome = self.executor.advance()
            self._record_execution_result(outcome=outcome)
            time.sleep(0.1)

    def _record_execution_result(self, *, outcome: StageOutcome) -> None:
        title = "Pipeline Update"
        if outcome.task_status == TaskStatus.WAITING_APPROVAL:
            title = "Awaiting Human Review"
        elif outcome.task_status in _TERMINAL_STATUSES:
            title = "Pipeline Finished"
        next_stage = outcome.next_stage.value if outcome.next_stage is not None else "-"
        published = ",".join(outcome.published_artifacts) if outcome.published_artifacts else "-"
        publish_progress_update(
            task_store=self.task_store,
            artifact_store=self.artifact_store,
            latest_update=outcome.message,
            timeline_title=title,
            timeline_body="\n".join(
                [
                    f"- stage_executed: {outcome.stage_executed.value}",
                    f"- next_stage: {next_stage}",
                    f"- task_status: {outcome.task_status.value}",
                    f"- current_owner: {outcome.current_owner}",
                    f"- published_artifacts: {published}",
                    f"- message: {outcome.message}",
                ],
            ),
            needs_human_review=outcome.task_status == TaskStatus.WAITING_APPROVAL,
        )

    def _finalize_terminal_state(self, status: TaskStatus) -> None:
        self.task_store.update_runtime_state(gateway_pid=None)
        publish_progress_update(
            task_store=self.task_store,
            artifact_store=self.artifact_store,
            latest_update=f"Task finished with status: {status.value}",
            timeline_title="Gateway Offline",
            timeline_body=f"Final task status: {status.value}",
            needs_human_review=False,
        )


def _resolve_launch_dir_child(command_launch_dir: str | None, dirname: str) -> str | None:
    if command_launch_dir is None:
        return None
    candidate = Path(command_launch_dir).expanduser().resolve() / dirname
    if not candidate.is_dir():
        return None
    return str(candidate)
