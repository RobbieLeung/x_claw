from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xclaw import cli
from xclaw.artifact_store import ArtifactStore
from xclaw.cli import main
from xclaw.human_io import ensure_supervision_artifacts, publish_progress_update, publish_review_request
from xclaw.protocol import Stage, TaskStatus
from xclaw.task_store import TaskStore
from xclaw.workspace import initialize_task_workspace


def _invoke_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code)
    return code, stdout.getvalue(), stderr.getvalue()


class CliTest(unittest.TestCase):
    def test_start_prints_progress_path_and_has_no_interaction_flag(self) -> None:
        parser = cli.build_parser()
        self.assertNotIn('--interaction', parser.format_help())

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            fake_worker = type("Worker", (), {"pid": 321})()
            with mock.patch("xclaw.cli._spawn_gateway_worker", return_value=fake_worker):
                exit_code, stdout, stderr = _invoke_main([
                    "start",
                    "--repo", str(repo),
                    "--task", "demo task",
                    "--workspace-root", str(root / "workspace"),
                    "--task-id", "task-cli-start",
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("progress_path:", stdout)

    def test_status_reports_supervision_fields_and_reject_requires_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="status command",
                task_id="task-status",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(gateway_pid=os.getpid())
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("active_task_id:", stdout)
            self.assertIn("task_status:", stdout)
            self.assertIn("user_summary:", stdout)
            self.assertIn("latest_update:", stdout)
            self.assertIn("next_step:", stdout)
            self.assertIn("needs_human_review:", stdout)
            self.assertNotIn("task_workspace_path:", stdout)
            self.assertNotIn("worker_pid:", stdout)
            self.assertNotIn("current_stage:", stdout)
            self.assertNotIn("current_owner:", stdout)
            self.assertNotIn("active_step_id:", stdout)
            self.assertNotIn("current_focus:", stdout)
            self.assertNotIn("progress_path:", stdout)
            self.assertNotIn("pending_advice_count:", stdout)
            self.assertNotIn("latest_review_request_id:", stdout)

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--reject",
            ])
            self.assertEqual(exit_code, 2)
            self.assertIn("requires `--comment`", stderr)

    def test_status_watch_prints_snapshots_until_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="status watch",
                task_id="task-status-watch",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(gateway_pid=os.getpid())
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            def _interrupt_after_first_sleep(_: float) -> None:
                raise KeyboardInterrupt

            with mock.patch("xclaw.cli.time.sleep", side_effect=_interrupt_after_first_sleep):
                exit_code, stdout, stderr = _invoke_main([
                    "status",
                    "--workspace-root", str(workspace_root),
                    "--watch",
                    "--watch-interval", "0.1",
                ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("--- ", stdout)
            self.assertIn("active_task_id:", stdout)
            self.assertIn("task_status:", stdout)

    def test_status_watch_rejects_invalid_combinations_and_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="status watch validation",
                task_id="task-status-watch-validation",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(gateway_pid=os.getpid())
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--watch",
                "--advise", "hello",
            ])
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("`--watch` can only be used", stderr)

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--watch",
                "--watch-interval", "0",
            ])
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("`--watch-interval` must be > 0", stderr)

    def test_status_advise_is_blocked_while_waiting_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="advice blocked during review",
                task_id="task-advise-blocked",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(
                gateway_pid=os.getpid(),
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
            publish_review_request(
                task_store=store,
                artifact_store=artifacts,
                summary="please review",
                proposal_body="proposal",
            )

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--advise", "please shrink scope",
            ])
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("task is waiting for human review", stderr)

    def test_status_approve_and_reject_use_review_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="review command",
                task_id="task-review",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(
                gateway_pid=os.getpid(),
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
            publish_review_request(
                task_store=store,
                artifact_store=artifacts,
                summary="please review",
                proposal_body="proposal",
            )

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--approve",
                "--comment", "looks good",
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("review_decision_id:", stdout)
            self.assertIn("review_decision: approved", stdout)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="reject command",
                task_id="task-reject",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(
                gateway_pid=os.getpid(),
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
            publish_review_request(
                task_store=store,
                artifact_store=artifacts,
                summary="please review",
                proposal_body="proposal",
            )

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--reject",
                "--comment", "tighten scope",
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("review_decision: rejected", stdout)
            context = store.load_task_context()
            self.assertEqual(context.current_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.status, TaskStatus.RUNNING)

    def test_status_advise_is_blocked_in_terminal_states(self) -> None:
        for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TERMINATED):
            with self.subTest(status=status.value), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                repo = root / "repo"
                repo.mkdir(parents=True, exist_ok=False)
                workspace_root = root / "workspace"
                result = initialize_task_workspace(
                    target_repo_path=repo,
                    task_description=f"terminal advice {status.value}",
                    task_id=f"task-terminal-{status.value}",
                    workspace_root=workspace_root,
                )
                store = TaskStore(result.task_workspace_path)
                store.update_runtime_state(
                    gateway_pid=os.getpid(),
                    status=status,
                )
                artifacts = ArtifactStore(result.task_workspace_path)
                ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

                exit_code, stdout, stderr = _invoke_main([
                    "status",
                    "--workspace-root", str(workspace_root),
                    "--advise", "please keep going",
                ])
                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("no active task is running", stderr)

    def test_status_review_actions_fail_outside_waiting_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="review outside gate",
                task_id="task-review-outside-gate",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(gateway_pid=os.getpid(), status=TaskStatus.RUNNING)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--approve",
            ])
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("waiting for approval", stderr)

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--reject",
                "--comment", "not ready",
            ])
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("waiting for approval", stderr)


    def test_status_prefers_product_owner_user_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="status user summary",
                task_id="task-status-user-summary",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(
                gateway_pid=os.getpid(),
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                current_owner="product_owner",
                status=TaskStatus.RUNNING,
            )
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
            publish_progress_update(
                task_store=store,
                artifact_store=artifacts,
                latest_update="PO refreshed the dispatch plan.",
                timeline_title="PO Summary",
                timeline_body="summary",
                current_focus="Implement the active step only.",
                next_step="Developer takes the next coding round.",
                risks="Keep the schema backward compatible.",
                user_summary="PO says the task now focuses on the active step, with backward compatibility as the main risk.",
            )

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(
                "user_summary: PO says the task now focuses on the active step, with backward compatibility as the main risk.",
                stdout,
            )
            self.assertIn("risks: Keep the schema backward compatible.", stdout)

    def test_status_falls_back_to_generated_summary_without_po_user_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="status fallback summary",
                task_id="task-status-fallback-summary",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(
                gateway_pid=os.getpid(),
                stage=Stage.DEVELOPER,
                current_owner="developer",
                status=TaskStatus.RUNNING,
            )
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
            publish_progress_update(
                task_store=store,
                artifact_store=artifacts,
                latest_update="Developer is implementing the dispatched step.",
                timeline_title="Developer Running",
                timeline_body="implementation in progress",
                current_focus="Implement the scoped repository changes.",
                next_step="Tester validates the step after coding finishes.",
                risks="-",
            )

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("user_summary: Current stage: developer.", stdout)
            self.assertIn("needs_human_review: no", stdout)
            self.assertNotIn("risks:", stdout)

    def test_status_shows_pending_advice_only_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="status pending advice",
                task_id="task-status-pending-advice",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(gateway_pid=os.getpid(), status=TaskStatus.RUNNING)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
                "--advise", "please keep the patch small",
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("advice_id:", stdout)
            self.assertIn("pending_advice_count: 1", stdout)

    def test_status_shows_review_request_id_only_during_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="status review request",
                task_id="task-status-review-request",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(
                gateway_pid=os.getpid(),
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
            publish_review_request(
                task_store=store,
                artifact_store=artifacts,
                summary="please review",
                proposal_body="proposal",
            )

            exit_code, stdout, stderr = _invoke_main([
                "status",
                "--workspace-root", str(workspace_root),
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("needs_human_review: yes", stdout)
            self.assertIn("latest_review_request_id: review-request-0001", stdout)


if __name__ == "__main__":
    unittest.main()
