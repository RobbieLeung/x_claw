"""Workspace bootstrap and target repository context helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Mapping
from uuid import uuid4

from . import protocol as constants
from .models import TaskContext
from .protocol import Stage, TaskStatus, owner_for_stage

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_TASK_REQUIRED_SECTIONS: tuple[str, ...] = (
    "Basic Info",
    "Runtime State",
    "Current Artifacts",
    "Recovery Notes",
)
_EVENT_LOG_REQUIRED_SECTIONS: tuple[str, ...] = ("Event Log",)
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        constants.TASK_STATUS_COMPLETED,
        constants.TASK_STATUS_FAILED,
        constants.TASK_STATUS_TERMINATED,
    },
)


class WorkspaceError(RuntimeError):
    """Base error for workspace bootstrap and lookup operations."""


class TargetRepositoryNotFoundError(WorkspaceError):
    """Raised when the target repository path does not exist."""


class TargetRepositoryAccessError(WorkspaceError):
    """Raised when the target repository path is not a readable directory."""


class TaskWorkspaceNotFoundError(WorkspaceError):
    """Raised when a task workspace cannot be found by task id."""


class WorkspaceAlreadyExistsError(WorkspaceError):
    """Raised when trying to create a workspace for an existing task id."""


class ActiveTaskDiscoveryError(WorkspaceError):
    """Raised when workspace scan finds multiple live active tasks."""


@dataclass(frozen=True)
class RepositoryContext:
    """Collected target repository facts persisted into task context."""

    target_repo_path: str
    target_repo_git_root: str | None
    target_repo_head: str | None
    target_repo_dirty: bool
    is_git_repository: bool


@dataclass(frozen=True)
class WorkspaceBootstrapResult:
    """Return payload from workspace bootstrap."""

    task_id: str
    workspace_root_path: str
    task_workspace_path: str
    task_file_path: str
    event_log_file_path: str
    repository_context: RepositoryContext
    task_context: TaskContext


@dataclass(frozen=True)
class ActiveTaskWorkspace:
    """One live task workspace discovered from task.md state."""

    task_id: str
    task_workspace_path: str
    status: str
    gateway_pid: int


@dataclass(frozen=True)
class TaskWorkspaceSummary:
    """One discovered task workspace regardless of current runtime liveness."""

    task_id: str
    task_workspace_path: str
    status: str
    gateway_pid: int | None


def project_root() -> Path:
    """Return xclaw repository root path."""

    return Path(__file__).resolve().parents[2]


def resolve_workspace_root(workspace_root: str | Path | None = None) -> Path:
    """Resolve workspace root path relative to the current invocation directory."""

    base_dir = Path.cwd().resolve()
    if workspace_root is None:
        return base_dir / constants.DEFAULT_WORKSPACE_ROOT

    candidate = Path(workspace_root).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_dir / candidate).resolve()


def generate_task_id() -> str:
    """Generate a stable task id with UTC timestamp and entropy suffix."""

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"task-{timestamp}-{uuid4().hex[:8]}"


def resolve_task_workspace_path(
    task_id: str,
    *,
    workspace_root: str | Path | None = None,
    require_exists: bool = True,
) -> Path:
    """Resolve a task workspace path by task id."""

    normalized_task_id = _normalize_task_id(task_id)
    root = resolve_workspace_root(workspace_root)
    task_workspace = root / normalized_task_id
    if require_exists and not task_workspace.is_dir():
        raise TaskWorkspaceNotFoundError(
            f"Task workspace for {normalized_task_id!r} does not exist under {root}.",
        )
    return task_workspace


def collect_target_repo_context(target_repo_path: str | Path) -> RepositoryContext:
    """Collect absolute repo path and git metadata for intake stage."""

    repo_path = _resolve_target_repo_path(target_repo_path)
    if shutil.which("git") is None:
        return RepositoryContext(
            target_repo_path=str(repo_path),
            target_repo_git_root=None,
            target_repo_head=None,
            target_repo_dirty=False,
            is_git_repository=False,
        )

    is_git = _git_is_work_tree(repo_path)
    if not is_git:
        return RepositoryContext(
            target_repo_path=str(repo_path),
            target_repo_git_root=None,
            target_repo_head=None,
            target_repo_dirty=False,
            is_git_repository=False,
        )

    git_root = _git_read_single_line(
        repo_path,
        "rev-parse",
        "--show-toplevel",
    )
    head = _git_read_single_line(repo_path, "rev-parse", "HEAD")
    dirty_output = _git_run(repo_path, "status", "--porcelain")
    dirty = bool(dirty_output.strip()) if dirty_output is not None else False

    return RepositoryContext(
        target_repo_path=str(repo_path),
        target_repo_git_root=git_root,
        target_repo_head=head,
        target_repo_dirty=dirty,
        is_git_repository=True,
    )


def initialize_task_workspace(
    *,
    target_repo_path: str | Path,
    task_description: str,
    task_id: str | None = None,
    workspace_root: str | Path | None = None,
    initial_stage: Stage | str = Stage.INTAKE,
    bootstrap_plan_source_path: str | Path | None = None,
) -> WorkspaceBootstrapResult:
    """Create task workspace and seed initial task/event markdown files."""

    description = _normalize_non_empty(task_description, "task_description")
    normalized_initial_stage = Stage(initial_stage)
    repository_context = collect_target_repo_context(target_repo_path)
    normalized_bootstrap_plan_source_path = None
    if bootstrap_plan_source_path is not None:
        normalized_bootstrap_plan_source_path = str(
            Path(bootstrap_plan_source_path).expanduser().resolve()
        )

    root = resolve_workspace_root(workspace_root)
    root.mkdir(parents=True, exist_ok=True)

    if task_id is None:
        task_id_value = _next_available_task_id(root)
    else:
        task_id_value = _normalize_task_id(task_id)

    task_workspace = root / task_id_value
    if task_workspace.exists():
        raise WorkspaceAlreadyExistsError(
            f"Task workspace already exists: {task_workspace}",
        )

    task_workspace.mkdir(parents=True, exist_ok=False)
    for directory_name in constants.WORKSPACE_REQUIRED_DIRS:
        (task_workspace / directory_name).mkdir(parents=True, exist_ok=False)

    created_at = _utc_now_iso()
    task_file_path = task_workspace / constants.TASK_FILENAME
    event_log_file_path = task_workspace / constants.EVENT_LOG_FILENAME

    recovery_note_parts = [f"{normalized_initial_stage.value} workspace initialized"]
    if normalized_bootstrap_plan_source_path is not None:
        recovery_note_parts.append(
            f"bootstrap_plan_source_path={normalized_bootstrap_plan_source_path}"
        )
    if not repository_context.is_git_repository:
        recovery_note_parts.append("target repository is not a git worktree")
    recovery_note_parts.append(f"task={description}")
    recovery_note = "; ".join(recovery_note_parts)
    template_context: dict[str, str] = {
        "task_id": task_id_value,
        "current_stage": normalized_initial_stage.value,
        "status": constants.TASK_STATUS_RUNNING,
        "task_version": "1",
        "event_log_version": "1",
        "created_at": created_at,
        "supersedes": "null",
        "target_repo_path": repository_context.target_repo_path,
        "bootstrap_plan_source_path": normalized_bootstrap_plan_source_path or "-",
        "task_workspace_path": str(task_workspace),
        "target_repo_git_root": repository_context.target_repo_git_root or "-",
        "target_repo_head": repository_context.target_repo_head or "-",
        "target_repo_dirty": _template_scalar(repository_context.target_repo_dirty),
        "current_owner": owner_for_stage(normalized_initial_stage),
        "gateway_pid": "-",
        "recovery_note": recovery_note,
    }

    task_rendered = render_template("task.md.j2", template_context)
    event_log_rendered = render_template("event_log.md.j2", template_context)

    task_file_path.write_text(task_rendered, encoding="utf-8")
    event_log_file_path.write_text(event_log_rendered, encoding="utf-8")

    task_context = TaskContext(
        task_id=task_id_value,
        task_workspace_path=str(task_workspace),
        bootstrap_plan_source_path=normalized_bootstrap_plan_source_path,
        target_repo_path=repository_context.target_repo_path,
        target_repo_git_root=repository_context.target_repo_git_root,
        target_repo_head=repository_context.target_repo_head,
        target_repo_dirty=repository_context.target_repo_dirty,
        current_stage=normalized_initial_stage,
        status=TaskStatus.RUNNING,
        current_owner=owner_for_stage(normalized_initial_stage),
        latest_event_seq=1,
        recovery_notes=(recovery_note,),
        gateway_pid=None,
    )

    return WorkspaceBootstrapResult(
        task_id=task_id_value,
        workspace_root_path=str(root),
        task_workspace_path=str(task_workspace),
        task_file_path=str(task_file_path),
        event_log_file_path=str(event_log_file_path),
        repository_context=repository_context,
        task_context=task_context,
    )


def find_active_task_workspace(
    workspace_root: str | Path | None = None,
) -> ActiveTaskWorkspace | None:
    """Return the single live active task discovered from task.md files."""

    from .task_store import TaskStore, TaskStoreError

    root = resolve_workspace_root(workspace_root)
    if not root.exists():
        return None

    active_candidates: list[ActiveTaskWorkspace] = []
    stale_candidates: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        task_file = child / constants.TASK_FILENAME
        if not task_file.is_file():
            continue
        try:
            context = TaskStore(child).load_task_context()
        except TaskStoreError:
            continue
        if context.status.value in _TERMINAL_STATUSES:
            continue
        if context.gateway_pid is None:
            stale_candidates.append(child)
            continue
        if not _pid_is_alive(context.gateway_pid):
            stale_candidates.append(child)
            continue
        active_candidates.append(
            ActiveTaskWorkspace(
                task_id=context.task_id,
                task_workspace_path=context.task_workspace_path,
                status=context.status.value,
                gateway_pid=context.gateway_pid,
            ),
        )

    for stale_path in stale_candidates:
        try:
            TaskStore(stale_path).update_runtime_state(gateway_pid=None)
        except Exception:
            continue

    if len(active_candidates) > 1:
        summary = ", ".join(
            f"{candidate.task_id} ({candidate.task_workspace_path})"
            for candidate in active_candidates
        )
        raise ActiveTaskDiscoveryError(
            f"multiple active tasks found in {root}: {summary}",
        )
    if not active_candidates:
        return None
    return active_candidates[0]


def find_latest_task_workspace(
    workspace_root: str | Path | None = None,
) -> TaskWorkspaceSummary | None:
    """Return the most recently updated task workspace under the selected root."""

    from .task_store import TaskStore, TaskStoreError

    root = resolve_workspace_root(workspace_root)
    if not root.exists():
        return None

    latest_candidate: tuple[str, TaskWorkspaceSummary] | None = None
    stale_candidates: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        task_file = child / constants.TASK_FILENAME
        if not task_file.is_file():
            continue
        try:
            context = TaskStore(child).load_task_context()
        except TaskStoreError:
            continue

        gateway_pid = context.gateway_pid
        if gateway_pid is not None and not _pid_is_alive(gateway_pid):
            stale_candidates.append(child)
            gateway_pid = None

        candidate = TaskWorkspaceSummary(
            task_id=context.task_id,
            task_workspace_path=context.task_workspace_path,
            status=context.status.value,
            gateway_pid=gateway_pid,
        )
        sort_key = context.task_id
        if latest_candidate is None or sort_key > latest_candidate[0]:
            latest_candidate = (sort_key, candidate)

    for stale_path in stale_candidates:
        try:
            TaskStore(stale_path).update_runtime_state(gateway_pid=None)
        except Exception:
            continue

    if latest_candidate is None:
        return None
    return latest_candidate[1]


def render_template(
    template_name: str,
    context: Mapping[str, object],
) -> str:
    """Render a package template file using simple ``{{ key }}`` placeholders."""

    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping.")

    template_text = resources.files("xclaw").joinpath("templates", template_name).read_text(
        encoding="utf-8",
    )
    missing_keys = {
        placeholder
        for placeholder in _PLACEHOLDER_RE.findall(template_text)
        if placeholder not in context
    }
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise WorkspaceError(f"Template context missing keys: {missing}.")

    def _replace(match: re.Match[str]) -> str:
        return _template_scalar(context[match.group(1)])

    return _PLACEHOLDER_RE.sub(_replace, template_text)


def task_required_sections() -> tuple[str, ...]:
    """Return required task.md section headings."""

    return _TASK_REQUIRED_SECTIONS


def event_log_required_sections() -> tuple[str, ...]:
    """Return required event_log.md section headings."""

    return _EVENT_LOG_REQUIRED_SECTIONS


def _resolve_target_repo_path(target_repo_path: str | Path) -> Path:
    repo_path = Path(target_repo_path).expanduser()
    try:
        resolved = repo_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise TargetRepositoryNotFoundError(
            f"Target repository path does not exist: {repo_path}",
        ) from exc

    if not resolved.is_dir():
        raise TargetRepositoryAccessError(
            f"Target repository path must be a directory: {resolved}",
        )
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise TargetRepositoryAccessError(
            f"Target repository path is not accessible: {resolved}",
        )
    return resolved


def _normalize_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty.")
    return normalized


def _normalize_task_id(task_id: str) -> str:
    normalized = _normalize_non_empty(task_id, "task_id")
    if not _TASK_ID_RE.match(normalized):
        raise ValueError(
            "task_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$.",
        )
    return normalized


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _next_available_task_id(workspace_root: Path) -> str:
    for _ in range(10):
        candidate = generate_task_id()
        if not (workspace_root / candidate).exists():
            return candidate
    raise WorkspaceError("Unable to generate a unique task_id after 10 attempts.")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _template_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _git_is_work_tree(repo_path: Path) -> bool:
    output = _git_run(repo_path, "rev-parse", "--is-inside-work-tree")
    if output is None:
        return False
    return output.strip().lower() == "true"


def _git_read_single_line(repo_path: Path, *args: str) -> str | None:
    output = _git_run(repo_path, *args)
    if output is None:
        return None
    line = output.strip()
    if not line:
        return None
    return line


def _git_run(repo_path: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


__all__ = [
    "ActiveTaskDiscoveryError",
    "ActiveTaskWorkspace",
    "RepositoryContext",
    "TargetRepositoryAccessError",
    "TargetRepositoryNotFoundError",
    "TaskWorkspaceNotFoundError",
    "WorkspaceAlreadyExistsError",
    "WorkspaceBootstrapResult",
    "WorkspaceError",
    "collect_target_repo_context",
    "event_log_required_sections",
    "find_active_task_workspace",
    "generate_task_id",
    "initialize_task_workspace",
    "project_root",
    "render_template",
    "resolve_task_workspace_path",
    "resolve_workspace_root",
    "task_required_sections",
]
