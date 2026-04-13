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


if __name__ == "__main__":
    unittest.main()
