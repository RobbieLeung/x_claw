from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from x_claw.artifact_store import ArtifactStore
from x_claw.human_io import (
    ensure_supervision_artifacts,
    pending_human_advice_count,
    publish_progress_update,
    publish_review_request,
    read_human_advice_entries,
    read_latest_review_request_id,
    read_progress_snapshot,
    resolve_pending_human_advice,
    submit_human_advice,
    submit_review_decision,
)
from x_claw.protocol import HumanAdviceDisposition, Stage, TaskStatus
from x_claw.task_store import TaskStore
from x_claw.workspace import initialize_task_workspace


class HumanIoTest(unittest.TestCase):
    def test_advice_and_review_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="demo",
                task_id="task-human-ops",
                workspace_root=root / "workspace",
            )
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            advice_id = submit_human_advice(
                task_store=store,
                artifact_store=artifacts,
                text="please keep the rollout small",
            )
            self.assertEqual(advice_id, "advice-0001")
            self.assertEqual(pending_human_advice_count(artifact_store=artifacts), 1)

            store.update_runtime_state(
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )
            review_request_id = publish_review_request(
                task_store=store,
                artifact_store=artifacts,
                summary="please review the current proposal",
                proposal_body="proposal body",
            )
            self.assertEqual(read_latest_review_request_id(artifact_store=artifacts), review_request_id)

            submission = submit_review_decision(
                task_store=store,
                artifact_store=artifacts,
                decision="rejected",
                comment="needs a tighter scope",
            )
            context = store.load_task_context()
            self.assertEqual(submission.decision, "rejected")
            self.assertEqual(context.current_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.status, TaskStatus.RUNNING)
            progress = read_progress_snapshot(artifact_store=artifacts)
            self.assertEqual(progress.needs_human_review, False)
            self.assertIn("Human review recorded", progress.latest_update)

    def test_resolve_pending_human_advice_marks_all_pending_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="resolve advice",
                task_id="task-resolve-advice",
                workspace_root=root / "workspace",
            )
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            submit_human_advice(task_store=store, artifact_store=artifacts, text="first")
            submit_human_advice(task_store=store, artifact_store=artifacts, text="second")

            resolved = resolve_pending_human_advice(
                task_store=store,
                artifact_store=artifacts,
                disposition=HumanAdviceDisposition.PARTIALLY_ACCEPTED,
            )
            self.assertEqual(resolved, 2)
            self.assertEqual(pending_human_advice_count(artifact_store=artifacts), 0)
            statuses = [entry.status for entry in read_human_advice_entries(artifact_store=artifacts)]
            self.assertEqual(statuses, ["partially_accepted", "partially_accepted"])

    def test_review_request_and_decision_ids_increment_across_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="multi round review",
                task_id="task-multi-review",
                workspace_root=root / "workspace",
            )
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            store.update_runtime_state(
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )
            request_1 = publish_review_request(
                task_store=store,
                artifact_store=artifacts,
                summary="round one",
                proposal_body="proposal one",
            )
            decision_1 = submit_review_decision(
                task_store=store,
                artifact_store=artifacts,
                decision="rejected",
                comment="needs more focus",
            )

            store.update_runtime_state(
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )
            request_2 = publish_review_request(
                task_store=store,
                artifact_store=artifacts,
                summary="round two",
                proposal_body="proposal two",
            )
            decision_2 = submit_review_decision(
                task_store=store,
                artifact_store=artifacts,
                decision="approved",
                comment="good now",
            )

            self.assertEqual(request_1, "review-request-0001")
            self.assertEqual(decision_1.review_decision_id, "review-decision-0001")
            self.assertEqual(request_2, "review-request-0002")
            self.assertEqual(decision_2.review_decision_id, "review-decision-0002")
            self.assertEqual(read_latest_review_request_id(artifact_store=artifacts), request_2)
            self.assertEqual(store.load_task_context().current_stage, Stage.PRODUCT_OWNER_DISPATCH)

    def test_submit_human_advice_rejects_terminal_states(self) -> None:
        for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TERMINATED):
            with self.subTest(status=status.value), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                repo = root / "repo"
                repo.mkdir(parents=True, exist_ok=False)
                result = initialize_task_workspace(
                    target_repo_path=repo,
                    task_description=f"terminal advice {status.value}",
                    task_id=f"task-human-io-{status.value}",
                    workspace_root=root / "workspace",
                )
                store = TaskStore(result.task_workspace_path)
                artifacts = ArtifactStore(result.task_workspace_path)
                ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
                store.update_runtime_state(status=status)

                with self.assertRaisesRegex(RuntimeError, "advice is not accepted"):
                    submit_human_advice(
                        task_store=store,
                        artifact_store=artifacts,
                        text="please adjust scope",
                    )


    def test_publish_progress_update_preserves_user_summary_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="progress summary preservation",
                task_id="task-progress-summary-preservation",
                workspace_root=root / "workspace",
            )
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            publish_progress_update(
                task_store=store,
                artifact_store=artifacts,
                latest_update="PO updated the summary.",
                timeline_title="PO Summary",
                timeline_body="first summary",
                current_focus="Focus on the scoped change.",
                next_step="Developer implements the next step.",
                risks="Schema compatibility.",
                user_summary="PO summary for users.",
            )
            publish_progress_update(
                task_store=store,
                artifact_store=artifacts,
                latest_update="Gateway is still running.",
                timeline_title="Gateway Update",
                timeline_body="orchestrator update",
                current_focus="Focus on the scoped change.",
                next_step="Developer continues coding.",
                risks="Schema compatibility.",
            )

            progress = read_progress_snapshot(artifact_store=artifacts)
            self.assertEqual(progress.latest_update, "Gateway is still running.")
            self.assertEqual(progress.user_summary, "PO summary for users.")


if __name__ == "__main__":
    unittest.main()
