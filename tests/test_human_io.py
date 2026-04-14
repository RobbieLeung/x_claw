from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from xclaw import protocol as constants
from xclaw.artifact_store import ArtifactStore
from xclaw.human_io import (
    ensure_supervision_artifacts,
    pending_human_advice_count,
    publish_review_request,
    read_current_review_decision,
    read_current_review_request,
    resolve_pending_human_advice,
    submit_human_advice,
    submit_review_decision,
)
from xclaw.protocol import HumanAdviceDisposition, ReviewKind, Stage, TaskStatus
from xclaw.task_store import TaskStore
from xclaw.workspace import initialize_task_workspace


class HumanIoTest(unittest.TestCase):
    def _workspace(self, *, task_id: str) -> tuple[TaskStore, ArtifactStore]:
        self.tmp_dir = tempfile.TemporaryDirectory()
        root = Path(self.tmp_dir.name)
        repo = root / "repo"
        repo.mkdir(parents=True, exist_ok=False)
        result = initialize_task_workspace(
            target_repo_path=repo,
            task_description=task_id,
            task_id=task_id,
            workspace_root=root / "workspace",
        )
        store = TaskStore(result.task_workspace_path)
        artifacts = ArtifactStore(result.task_workspace_path)
        ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
        return store, artifacts

    def tearDown(self) -> None:
        tmp_dir = getattr(self, "tmp_dir", None)
        if tmp_dir is not None:
            tmp_dir.cleanup()

    def test_advice_resolution_marks_all_pending_entries(self) -> None:
        store, artifacts = self._workspace(task_id="task-human-advice")

        submit_human_advice(task_store=store, artifact_store=artifacts, text="first")
        submit_human_advice(task_store=store, artifact_store=artifacts, text="second")

        resolved = resolve_pending_human_advice(
            task_store=store,
            artifact_store=artifacts,
            disposition=HumanAdviceDisposition.PARTIALLY_ACCEPTED,
        )

        self.assertEqual(resolved, 2)
        self.assertEqual(pending_human_advice_count(artifact_store=artifacts), 0)

    def test_plan_review_request_and_decision_preserve_kind_and_revision(self) -> None:
        store, artifacts = self._workspace(task_id="task-plan-review")
        store.update_runtime_state(
            stage=Stage.HUMAN_GATE,
            current_owner=constants.ROLE_HUMAN_GATE,
            status=TaskStatus.WAITING_APPROVAL,
        )

        publish_review_request(
            task_store=store,
            artifact_store=artifacts,
            summary="plan needs confirmation",
            proposal_body="plan body",
            review_kind=ReviewKind.PLAN,
            plan_revision="plan-r3",
            focus_items=("Confirm scope boundary", "Confirm rollout risk"),
        )
        request = read_current_review_request(artifact_store=artifacts)
        assert request is not None
        self.assertEqual(request.review_kind, ReviewKind.PLAN)
        self.assertEqual(request.plan_revision, "plan-r3")
        self.assertEqual(request.focus_items, ("Confirm scope boundary", "Confirm rollout risk"))

        submission = submit_review_decision(
            task_store=store,
            artifact_store=artifacts,
            decision="approved",
            comment="looks fine",
        )
        decision = read_current_review_decision(artifact_store=artifacts)
        assert decision is not None
        self.assertEqual(submission.review_kind, ReviewKind.PLAN.value)
        self.assertEqual(submission.plan_revision, "plan-r3")
        self.assertEqual(decision.review_kind, ReviewKind.PLAN)
        self.assertEqual(decision.plan_revision, "plan-r3")
        self.assertEqual(store.load_task_context().current_stage, Stage.PRODUCT_OWNER_DISPATCH)

    def test_delivery_review_decision_has_no_plan_revision(self) -> None:
        store, artifacts = self._workspace(task_id="task-delivery-review")
        store.update_runtime_state(
            stage=Stage.HUMAN_GATE,
            current_owner=constants.ROLE_HUMAN_GATE,
            status=TaskStatus.WAITING_APPROVAL,
        )

        publish_review_request(
            task_store=store,
            artifact_store=artifacts,
            summary="delivery ready",
            proposal_body="delivery proposal",
            review_kind=ReviewKind.DELIVERY,
        )
        submit_review_decision(
            task_store=store,
            artifact_store=artifacts,
            decision="rejected",
            comment="ship blockers remain",
        )
        decision = read_current_review_decision(artifact_store=artifacts)
        assert decision is not None
        self.assertEqual(decision.review_kind, ReviewKind.DELIVERY)
        self.assertIsNone(decision.plan_revision)


if __name__ == "__main__":
    unittest.main()
