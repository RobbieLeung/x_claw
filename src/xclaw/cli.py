from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Sequence

from . import __version__, protocol as constants
from .artifact_store import ArtifactStore
from .gateway import GatewayRunConfig, TaskGateway
from .human_io import (
    ensure_supervision_artifacts,
    pending_human_advice_count,
    read_latest_review_request_id,
    read_progress_snapshot,
    submit_human_advice,
    submit_review_decision,
)
from .protocol import ReviewDecision, TaskStatus
from .task_store import TaskStore
from .workspace import (
    ActiveTaskDiscoveryError,
    WorkspaceError,
    find_active_task_workspace,
    initialize_task_workspace,
    resolve_workspace_root,
)


class CliCommandError(RuntimeError):
    """Raised when one CLI command fails with user-facing context."""


_ACTIVE_STEP_PATTERN = re.compile(r"^-\s*active_step_id:\s*(.+)$", re.MULTILINE)
_STATUS_FIELD_ORDER: tuple[str, ...] = (
    "active_task_id",
    "task_workspace_path",
    "worker_pid",
    "task_status",
    "current_stage",
    "current_owner",
    "active_step_id",
    "user_summary",
    "latest_update",
    "current_focus",
    "next_step",
    "risks",
    "pending_advice_count",
    "needs_human_review",
    "latest_review_request_id",
    "progress_path",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xclaw",
        description="xclaw is a single-task gateway service.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Start the single active task service")
    start_parser.add_argument("--repo", type=Path, required=True, help="Target repository path")
    start_parser.add_argument("--task", required=True, help="Task description")
    start_parser.add_argument("--task-id", help="Optional explicit task identifier")
    start_parser.add_argument("--workspace-root", type=Path, help="Optional workspace root override")

    status_parser = subparsers.add_parser("status", help="Show task progress and optionally submit supervision input")
    status_parser.add_argument("--workspace-root", type=Path, help="Optional workspace root override")
    action_group = status_parser.add_mutually_exclusive_group()
    action_group.add_argument("--advise", help="Submit one human advice message")
    action_group.add_argument("--approve", action="store_true", help="Approve the current human review request")
    action_group.add_argument("--reject", action="store_true", help="Reject the current human review request")
    status_parser.add_argument("--comment", help="Optional comment for approve; required for reject")

    stop_parser = subparsers.add_parser("stop", help="Stop the active task service")
    stop_parser.add_argument("--workspace-root", type=Path, help="Optional workspace root override")

    worker_parser = subparsers.add_parser("gateway-worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--task-workspace-path", required=True)
    worker_parser.add_argument("--workspace-root", type=Path)
    worker_parser.add_argument("--command-launch-dir", type=Path)
    worker_parser.add_argument("--poll-interval-seconds", type=float, default=1.0)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "start":
            return _handle_start(args)
        if args.command == "status":
            return _handle_status(args)
        if args.command == "stop":
            return _handle_stop(args)
        if args.command == "gateway-worker":
            return _handle_gateway_worker(args)
    except CliCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (WorkspaceError, ValueError, OSError, ActiveTaskDiscoveryError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    raise RuntimeError(f"unsupported command: {args.command!r}")


def _handle_start(args: argparse.Namespace) -> int:
    command_launch_dir = Path.cwd().resolve()
    workspace_root = _resolve_cli_workspace_root(args.workspace_root)
    active = find_active_task_workspace(workspace_root)
    if active is not None:
        raise CliCommandError(
            "active task already exists: "
            f"{active.task_id} ({active.task_workspace_path}). "
            "Use `xclaw status`.",
        )

    bootstrap = initialize_task_workspace(
        target_repo_path=args.repo,
        task_description=args.task,
        task_id=args.task_id,
        workspace_root=workspace_root,
    )
    worker = _spawn_gateway_worker(
        task_workspace_path=bootstrap.task_workspace_path,
        workspace_root=workspace_root,
        command_launch_dir=command_launch_dir,
    )
    print(f"task_id: {bootstrap.task_id}")
    print(f"task_workspace_path: {bootstrap.task_workspace_path}")
    print(f"worker_pid: {worker.pid}")
    print(f"progress_path: {Path(bootstrap.task_workspace_path) / 'current' / 'progress.md'}")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    workspace_root = _resolve_cli_workspace_root(args.workspace_root)
    active = find_active_task_workspace(workspace_root)
    if active is None:
        if args.advise or args.approve or args.reject or args.comment:
            raise CliCommandError("no active task is running.")
        _print_status_view(_idle_status_view())
        return 0

    task_store = TaskStore(active.task_workspace_path)
    artifact_store = ArtifactStore(active.task_workspace_path)
    ensure_supervision_artifacts(task_store=task_store, artifact_store=artifact_store)
    _print_action_result(_run_status_action(args, task_store=task_store, artifact_store=artifact_store))
    _print_status_view(_build_status_view(active=active, task_store=task_store, artifact_store=artifact_store))
    return 0


def _idle_status_view() -> dict[str, str]:
    return {
        "active_task_id": "-",
        "task_workspace_path": "-",
        "worker_pid": "-",
        "task_status": "idle",
        "current_stage": "-",
        "current_owner": "-",
        "active_step_id": "-",
        "user_summary": "-",
        "latest_update": "-",
        "current_focus": "-",
        "next_step": "-",
        "risks": "-",
        "pending_advice_count": "0",
        "needs_human_review": "no",
        "latest_review_request_id": "-",
        "progress_path": "-",
    }


def _build_status_view(*, active, task_store: TaskStore, artifact_store: ArtifactStore) -> dict[str, str]:
    context = task_store.load_task_context()
    progress = read_progress_snapshot(artifact_store=artifact_store)
    progress_path = _resolve_current_artifact_path(
        context.task_workspace_path,
        context.current_artifacts.get(constants.ARTIFACT_PROGRESS),
    )
    return {
        "active_task_id": active.task_id,
        "task_workspace_path": active.task_workspace_path,
        "worker_pid": str(active.gateway_pid),
        "task_status": context.status.value,
        "current_stage": context.current_stage.value,
        "current_owner": context.current_owner,
        "active_step_id": _read_active_step_id(context),
        "user_summary": _build_user_summary(progress=progress, context=context),
        "latest_update": progress.latest_update,
        "current_focus": progress.current_focus,
        "next_step": progress.next_step,
        "risks": progress.risks,
        "pending_advice_count": str(pending_human_advice_count(artifact_store=artifact_store)),
        "needs_human_review": "yes" if progress.needs_human_review else "no",
        "latest_review_request_id": read_latest_review_request_id(artifact_store=artifact_store),
        "progress_path": progress_path,
    }


def _build_user_summary(*, progress, context) -> str:
    if progress.user_summary != "-":
        return progress.user_summary
    risk_text = progress.risks if progress.risks != "-" else "No major risks are recorded."
    review_text = "Human review is required." if progress.needs_human_review else "Human review is not required right now."
    return (
        f"Current stage: {context.current_stage.value}. Latest update: {progress.latest_update} "
        f"Focus: {progress.current_focus} Next: {progress.next_step} Risks: {risk_text} {review_text}"
    )


def _run_status_action(
    args: argparse.Namespace,
    *,
    task_store: TaskStore,
    artifact_store: ArtifactStore,
) -> tuple[tuple[str, str], ...]:
    if args.advise:
        advice_id = submit_human_advice(
            task_store=task_store,
            artifact_store=artifact_store,
            text=args.advise,
        )
        return (("advice_id", advice_id),)
    if args.approve:
        result = submit_review_decision(
            task_store=task_store,
            artifact_store=artifact_store,
            decision=ReviewDecision.APPROVED.value,
            comment=args.comment or "approved",
        )
        return (
            ("review_decision_id", result.review_decision_id),
            ("review_decision", result.decision),
        )
    if args.reject:
        if not args.comment or not args.comment.strip():
            raise CliCommandError("`--reject` requires `--comment`.")
        result = submit_review_decision(
            task_store=task_store,
            artifact_store=artifact_store,
            decision=ReviewDecision.REJECTED.value,
            comment=args.comment,
        )
        return (
            ("review_decision_id", result.review_decision_id),
            ("review_decision", result.decision),
        )
    if args.comment:
        raise CliCommandError("`--comment` must be used with `--approve` or `--reject`.")
    return ()


def _print_action_result(action_result: tuple[tuple[str, str], ...]) -> None:
    for key, value in action_result:
        print(f"{key}: {value}")


def _print_status_view(status_view: dict[str, str]) -> None:
    for field in _STATUS_FIELD_ORDER:
        print(f"{field}: {status_view[field]}")


def _handle_stop(args: argparse.Namespace) -> int:
    workspace_root = _resolve_cli_workspace_root(args.workspace_root)
    active = find_active_task_workspace(workspace_root)
    if active is None:
        raise CliCommandError("no active task is running.")
    task_store = TaskStore(active.task_workspace_path)
    context = task_store.load_task_context()
    task_store.update_runtime_state(
        status=TaskStatus.TERMINATED,
        gateway_pid=None,
    )
    task_store.append_event(
        actor="system",
        action="gateway_stop_requested",
        result="terminated",
    )
    task_store.append_recovery_note("gateway stop requested from CLI")
    try:
        os.kill(active.gateway_pid, signal.SIGTERM)
    except OSError:
        pass
    print(f"task_id: {context.task_id}")
    print("status: terminated")
    return 0


def _handle_gateway_worker(args: argparse.Namespace) -> int:
    gateway = TaskGateway(
        GatewayRunConfig(
            task_workspace_path=args.task_workspace_path,
            workspace_root=str(_resolve_cli_workspace_root(args.workspace_root))
            if args.workspace_root
            else None,
            command_launch_dir=str(args.command_launch_dir.expanduser().resolve())
            if args.command_launch_dir
            else None,
            poll_interval_seconds=args.poll_interval_seconds,
        ),
    )
    return gateway.run_forever()


def _resolve_cli_workspace_root(workspace_root: Path | None) -> Path | None:
    if workspace_root is None:
        return None
    return resolve_workspace_root(workspace_root)


def _spawn_gateway_worker(
    *,
    task_workspace_path: str,
    workspace_root: Path | None,
    command_launch_dir: Path | None = None,
) -> subprocess.Popen[str]:
    if command_launch_dir is None:
        command_launch_dir = Path.cwd().resolve()
    else:
        command_launch_dir = command_launch_dir.expanduser().resolve()

    log_path = Path(task_workspace_path) / "gateway.log"
    log_handle = log_path.open("a", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "xclaw.cli",
        "gateway-worker",
        "--task-workspace-path",
        task_workspace_path,
        "--command-launch-dir",
        str(command_launch_dir),
    ]
    if workspace_root is not None:
        command.extend(["--workspace-root", str(workspace_root)])
    try:
        return subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
            cwd=str(Path(task_workspace_path).resolve()),
            env=_build_worker_environment(command_launch_dir),
        )
    finally:
        log_handle.close()


def _build_worker_environment(command_launch_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    launch_cwd = command_launch_dir.expanduser().resolve()
    source_root = Path(__file__).resolve().parents[1]
    pythonpath_entries: list[str] = [str(source_root)]

    existing_pythonpath = env.get("PYTHONPATH", "")
    for raw_entry in existing_pythonpath.split(os.pathsep):
        if not raw_entry:
            continue
        candidate = Path(raw_entry).expanduser()
        if not candidate.is_absolute():
            candidate = (launch_cwd / candidate).resolve()
        else:
            candidate = candidate.resolve()
        normalized = str(candidate)
        if normalized not in pythonpath_entries:
            pythonpath_entries.append(normalized)

    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


def _read_active_step_id(context: object) -> str:
    execution_plan_path = getattr(context, "current_artifacts", {}).get(constants.ARTIFACT_EXECUTION_PLAN)
    if not execution_plan_path:
        return "-"

    resolved_path = Path(getattr(context, "task_workspace_path")) / execution_plan_path
    try:
        content = resolved_path.read_text(encoding="utf-8")
    except OSError:
        return "-"

    match = _ACTIVE_STEP_PATTERN.search(content)
    if match is None:
        return "-"
    value = match.group(1).strip()
    return value or "-"


def _resolve_current_artifact_path(task_workspace_path: str, artifact_path: str | None) -> Path:
    if artifact_path is None:
        return Path(task_workspace_path) / constants.CURRENT_DIRNAME
    return (Path(task_workspace_path) / artifact_path).resolve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
