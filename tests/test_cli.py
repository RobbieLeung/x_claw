from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xclaw import cli
from xclaw import protocol as constants
from xclaw.artifact_store import ArtifactStore
from xclaw.cli import main
from xclaw.human_io import ensure_supervision_artifacts, publish_review_request
from xclaw.protocol import ReviewKind, Stage, TaskStatus
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
    def _workspace(self, *, task_id: str) -> tuple[Path, TaskStore, ArtifactStore]:
        self.tmp_dir = tempfile.TemporaryDirectory()
        root = Path(self.tmp_dir.name)
        repo = root / "repo"
        repo.mkdir(parents=True, exist_ok=False)
        workspace_root = root / "workspace"
        result = initialize_task_workspace(
            target_repo_path=repo,
            task_description=task_id,
            task_id=task_id,
            workspace_root=workspace_root,
        )
        store = TaskStore(result.task_workspace_path)
        store.update_runtime_state(gateway_pid=os.getpid())
        artifacts = ArtifactStore(result.task_workspace_path)
        ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
        return workspace_root, store, artifacts

    def tearDown(self) -> None:
        tmp_dir = getattr(self, "tmp_dir", None)
        if tmp_dir is not None:
            tmp_dir.cleanup()

    def test_status_reports_supervision_fields(self) -> None:
        workspace_root, _, _ = self._workspace(task_id="task-status")
        exit_code, stdout, stderr = _invoke_main(["status", "--workspace-root", str(workspace_root)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("active_task_id:", stdout)
        self.assertIn("task_status:", stdout)
        self.assertIn("user_summary:", stdout)
        self.assertIn("needs_human_review:", stdout)
        self.assertNotIn("review_kind:", stdout)

    def test_status_shows_plan_review_kind_and_confirmation_summary(self) -> None:
        workspace_root, store, artifacts = self._workspace(task_id="task-plan-review-status")
        store.update_runtime_state(
            stage=Stage.HUMAN_GATE,
            current_owner=constants.ROLE_HUMAN_GATE,
            status=TaskStatus.WAITING_APPROVAL,
        )
        publish_review_request(
            task_store=store,
            artifact_store=artifacts,
            summary="please confirm the plan",
            proposal_body="plan proposal",
            review_kind=ReviewKind.PLAN,
            plan_revision="plan-r2",
            focus_items=("Confirm scope cut", "Confirm integration risk"),
        )

        exit_code, stdout, stderr = _invoke_main(["status", "--workspace-root", str(workspace_root)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("needs_human_review: yes", stdout)
        self.assertIn("review_kind: plan", stdout)
        self.assertIn("plan_confirmation_summary: Confirm scope cut | Confirm integration risk", stdout)
        self.assertIn("latest_review_request_id: review-request-0001", stdout)

    def test_status_approve_and_reject_include_review_kind(self) -> None:
        workspace_root, store, artifacts = self._workspace(task_id="task-approve-review-kind")
        store.update_runtime_state(
            stage=Stage.HUMAN_GATE,
            current_owner=constants.ROLE_HUMAN_GATE,
            status=TaskStatus.WAITING_APPROVAL,
        )
        publish_review_request(
            task_store=store,
            artifact_store=artifacts,
            summary="delivery review",
            proposal_body="delivery proposal",
            review_kind=ReviewKind.DELIVERY,
        )
        exit_code, stdout, stderr = _invoke_main(
            ["status", "--workspace-root", str(workspace_root), "--approve", "--comment", "ok"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("review_decision: approved", stdout)
        self.assertIn("review_kind: delivery", stdout)

    def test_status_advise_is_blocked_while_waiting_approval(self) -> None:
        workspace_root, store, artifacts = self._workspace(task_id="task-advise-blocked")
        store.update_runtime_state(
            stage=Stage.HUMAN_GATE,
            current_owner=constants.ROLE_HUMAN_GATE,
            status=TaskStatus.WAITING_APPROVAL,
        )
        publish_review_request(
            task_store=store,
            artifact_store=artifacts,
            summary="please review",
            proposal_body="proposal",
            review_kind=ReviewKind.DELIVERY,
        )
        exit_code, stdout, stderr = _invoke_main(
            ["status", "--workspace-root", str(workspace_root), "--advise", "please shrink scope"]
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("task is waiting for human review", stderr)


    def test_resume_restarts_latest_task_at_product_owner_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            older = initialize_task_workspace(
                target_repo_path=repo,
                task_description="older",
                task_id="task-20240101-000000-older",
                workspace_root=workspace_root,
            )
            latest = initialize_task_workspace(
                target_repo_path=repo,
                task_description="latest",
                task_id="task-20240102-000000-latest",
                workspace_root=workspace_root,
            )
            older_store = TaskStore(older.task_workspace_path)
            older_store.update_runtime_state(
                stage=Stage.TESTER,
                current_owner=constants.ROLE_TESTER,
                status=TaskStatus.TERMINATED,
                gateway_pid=None,
            )
            latest_store = TaskStore(latest.task_workspace_path)
            latest_store.update_runtime_state(
                stage=Stage.DEVELOPER,
                current_owner=constants.ROLE_DEVELOPER,
                status=TaskStatus.FAILED,
                gateway_pid=None,
            )

            fake_worker = type("Worker", (), {"pid": 654})()
            with mock.patch("xclaw.cli._spawn_gateway_worker", return_value=fake_worker) as spawn_worker:
                exit_code, stdout, stderr = _invoke_main(["resume", "--workspace-root", str(workspace_root)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("task_id: task-20240102-000000-latest", stdout)
            self.assertIn("resume_stage: product_owner_dispatch", stdout)
            spawn_worker.assert_called_once()
            resumed_store = TaskStore(latest.task_workspace_path)
            resumed_context = resumed_store.load_task_context()
            self.assertEqual(resumed_context.status, TaskStatus.RUNNING)
            self.assertEqual(resumed_context.current_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(resumed_context.current_owner, constants.ROLE_PRODUCT_OWNER)

    def test_resume_rejects_when_active_task_exists(self) -> None:
        workspace_root, _, _ = self._workspace(task_id="task-resume-blocked")
        exit_code, stdout, stderr = _invoke_main(["resume", "--workspace-root", str(workspace_root)])
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("active task already exists", stderr)

    def test_resume_rejects_completed_latest_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="done",
                task_id="task-resume-completed",
                workspace_root=workspace_root,
            )
            store = TaskStore(result.task_workspace_path)
            store.update_runtime_state(
                stage=Stage.CLOSEOUT,
                current_owner=constants.ROLE_ORCHESTRATOR,
                status=TaskStatus.COMPLETED,
                gateway_pid=None,
            )

            exit_code, stdout, stderr = _invoke_main(["resume", "--workspace-root", str(workspace_root)])
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("latest task is already completed", stderr)

    def test_start_prints_progress_path(self) -> None:
        parser = cli.build_parser()
        self.assertNotIn("--interaction", parser.format_help())
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            fake_worker = type("Worker", (), {"pid": 321})()
            with mock.patch("xclaw.cli._spawn_gateway_worker", return_value=fake_worker):
                exit_code, stdout, stderr = _invoke_main(
                    [
                        "start",
                        "--repo",
                        str(repo),
                        "--task",
                        "demo task",
                        "--workspace-root",
                        str(root / "workspace"),
                        "--task-id",
                        "task-cli-start",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("progress_path:", stdout)

    def test_start_accepts_bootstrap_plan_without_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            plan = root / "plan.md"
            repo.mkdir(parents=True, exist_ok=False)
            plan.write_text("# Imported Plan\n\nDetails\n", encoding="utf-8")
            fake_worker = type("Worker", (), {"pid": 321})()

            with mock.patch("xclaw.cli._spawn_gateway_worker", return_value=fake_worker):
                exit_code, stdout, stderr = _invoke_main(
                    [
                        "start",
                        "--repo",
                        str(repo),
                        "--plan",
                        str(plan),
                        "--workspace-root",
                        str(root / "workspace"),
                        "--task-id",
                        "task-cli-start-plan",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("progress_path:", stdout)

            store = TaskStore(root / "workspace" / "task-cli-start-plan")
            context = store.load_task_context()
            self.assertEqual(context.current_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.bootstrap_plan_source_path, str(plan.resolve()))
            self.assertIn("task=Imported Plan", "\n".join(context.recovery_notes))

    def test_start_uses_explicit_task_over_bootstrap_plan_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            plan = root / "plan.md"
            repo.mkdir(parents=True, exist_ok=False)
            plan.write_text("# Imported Plan\n", encoding="utf-8")
            fake_worker = type("Worker", (), {"pid": 321})()

            with mock.patch("xclaw.cli._spawn_gateway_worker", return_value=fake_worker):
                exit_code, _, stderr = _invoke_main(
                    [
                        "start",
                        "--repo",
                        str(repo),
                        "--plan",
                        str(plan),
                        "--task",
                        "explicit task name",
                        "--workspace-root",
                        str(root / "workspace"),
                        "--task-id",
                        "task-cli-start-explicit",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            store = TaskStore(root / "workspace" / "task-cli-start-explicit")
            self.assertIn("task=explicit task name", "\n".join(store.load_task_context().recovery_notes))

    def test_start_requires_task_or_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)

            exit_code, stdout, stderr = _invoke_main(
                [
                    "start",
                    "--repo",
                    str(repo),
                    "--workspace-root",
                    str(root / "workspace"),
                ]
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("requires `--task` or `--plan`", stderr)

    def test_start_rejects_invalid_bootstrap_plan_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            empty_plan = root / "empty.md"
            plan_dir = root / "plan-dir"
            repo.mkdir(parents=True, exist_ok=False)
            empty_plan.write_text("\n", encoding="utf-8")
            plan_dir.mkdir(parents=True, exist_ok=False)

            for plan_arg, expected in (
                (root / "missing.md", "bootstrap plan does not exist"),
                (plan_dir, "bootstrap plan must be a file"),
                (empty_plan, "bootstrap plan must be non-empty"),
            ):
                with self.subTest(plan_arg=plan_arg):
                    exit_code, stdout, stderr = _invoke_main(
                        [
                            "start",
                            "--repo",
                            str(repo),
                            "--plan",
                            str(plan_arg),
                            "--workspace-root",
                            str(root / "workspace"),
                        ]
                    )
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(stdout, "")
                    self.assertIn(expected, stderr)


if __name__ == "__main__":
    unittest.main()
