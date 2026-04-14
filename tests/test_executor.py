from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from xclaw import protocol as constants
from xclaw.artifact_store import ArtifactStore
from xclaw.executor import (
    PlanSnapshot,
    RouteDecision,
    StageExecutor,
    _extract_developer_context_artifacts_field,
    _parse_route_decision,
    _require_plan_snapshot,
)
from xclaw.human_io import ensure_supervision_artifacts, publish_review_request
from xclaw.protocol import HumanAdviceDisposition, ReviewKind, Stage, TaskStatus
from xclaw.task_store import TaskStore
from xclaw.workspace import initialize_task_workspace


class ExecutorContractTest(unittest.TestCase):
    def _workspace(self, *, task_id: str):
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
        return result, store, artifacts

    def tearDown(self) -> None:
        tmp_dir = getattr(self, "tmp_dir", None)
        if tmp_dir is not None:
            tmp_dir.cleanup()

    def test_parse_route_decision_requires_review_kind_for_human_gate(self) -> None:
        response = """
- next_stage: human_gate
- task_status: running
- based_on_artifacts: plan
- human_advice_disposition: none
- review_kind_requested: -
""".strip()
        with self.assertRaisesRegex(ValueError, "review_kind_requested must be one of"):
            _parse_route_decision(
                response,
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                unresolved_feedback=False,
                current_artifacts={constants.ARTIFACT_PLAN: "current/plan.md"},
            )

    def test_parse_route_decision_allows_dash_review_kind_outside_human_gate(self) -> None:
        response = """
- next_stage: architect_planning
- task_status: running
- based_on_artifacts: progress
- human_advice_disposition: none
- review_kind_requested: -
""".strip()

        decision = _parse_route_decision(
            response,
            stage=Stage.PRODUCT_OWNER_REFINEMENT,
            unresolved_feedback=False,
            current_artifacts={constants.ARTIFACT_PROGRESS: "current/progress.md"},
        )

        self.assertEqual(decision.next_stage, Stage.ARCHITECT_PLANNING)
        self.assertEqual(decision.task_status, TaskStatus.RUNNING)
        self.assertEqual(decision.based_on_artifacts, (constants.ARTIFACT_PROGRESS,))
        self.assertEqual(decision.human_advice_disposition, HumanAdviceDisposition.NONE)
        self.assertIsNone(decision.review_kind_requested)

    def test_require_plan_snapshot_parses_confirmation_contract(self) -> None:
        snapshot = _require_plan_snapshot(
            response_text=(
                "# Plan\n"
                "- plan_revision: plan-r2\n"
                "- human_confirmation_required: yes\n"
                "- human_confirmation_items: Confirm scope | Confirm dependency risk\n"
                "- active_subtask_id: subtask-002\n"
            ),
            source_label="test response",
        )
        self.assertEqual(snapshot.plan_revision, "plan-r2")
        self.assertTrue(snapshot.human_confirmation_required)
        self.assertEqual(snapshot.human_confirmation_items, ("Confirm scope", "Confirm dependency risk"))
        self.assertEqual(snapshot.active_subtask_id, "subtask-002")

    def test_require_plan_snapshot_ignores_duplicate_fields_in_other_sections(self) -> None:
        snapshot = _require_plan_snapshot(
            response_text=(
                "# Plan\n"
                "- plan_revision: plan-r3\n"
                "- human_confirmation_required: no\n"
                "- human_confirmation_items: -\n"
                "- active_subtask_id: subtask-001\n\n"
                "# Dev Handoff\n"
                "- plan_revision: plan-r3\n"
                "- active_subtask_id: subtask-001\n"
                "- context_artifacts: progress\n"
            ),
            source_label="test response",
        )

        self.assertEqual(snapshot.plan_revision, "plan-r3")
        self.assertFalse(snapshot.human_confirmation_required)
        self.assertEqual(snapshot.human_confirmation_items, ())
        self.assertEqual(snapshot.active_subtask_id, "subtask-001")

    def test_parse_route_decision_prefers_route_decision_section(self) -> None:
        response = """
# Plan
- plan_revision: plan-r4
- human_confirmation_required: no
- human_confirmation_items: -
- active_subtask_id: subtask-004

# Route Decision
- next_stage: developer
- task_status: running
- based_on_artifacts: plan,progress
- human_advice_disposition: none
- review_kind_requested: -
""".strip()

        decision = _parse_route_decision(
            response,
            stage=Stage.PRODUCT_OWNER_DISPATCH,
            unresolved_feedback=False,
            current_artifacts={
                constants.ARTIFACT_PLAN: "current/plan.md",
                constants.ARTIFACT_PROGRESS: "current/progress.md",
            },
        )

        self.assertEqual(decision.next_stage, Stage.DEVELOPER)
        self.assertEqual(decision.task_status, TaskStatus.RUNNING)
        self.assertEqual(
            decision.based_on_artifacts,
            (constants.ARTIFACT_PLAN, constants.ARTIFACT_PROGRESS),
        )
        self.assertEqual(decision.human_advice_disposition, HumanAdviceDisposition.NONE)
        self.assertIsNone(decision.review_kind_requested)

    def test_build_developer_invocation_uses_plan_baseline_and_selected_context(self) -> None:
        result, store, artifacts = self._workspace(task_id="task-developer-context")

        def publish_current(artifact_type: str, body: str) -> None:
            publication = artifacts.publish_artifact(
                artifact_type=artifact_type,
                body=body,
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                producer=constants.ROLE_PRODUCT_OWNER,
                consumer=constants.ROLE_DEVELOPER,
                status="ready",
            )
            store.set_current_artifact(artifact_type=artifact_type, artifact_path=publication.current_path)

        publish_current(
            constants.ARTIFACT_PLAN,
            "# Plan\n\n- plan_revision: plan-r1\n- human_confirmation_required: no\n- human_confirmation_items: -\n- active_subtask_id: subtask-001\n",
        )
        publish_current(
            constants.ARTIFACT_DEV_HANDOFF,
            "# Developer Handoff\n\n- context_artifacts: progress, implementation_result\n",
        )
        publish_current(constants.ARTIFACT_PROGRESS, "# Progress\n")
        publish_current(constants.ARTIFACT_IMPLEMENTATION_RESULT, "# Implementation Result\n")

        executor = StageExecutor(result.task_workspace_path)
        invocation = executor._build_stage_invocation(stage=Stage.DEVELOPER)

        self.assertEqual(
            invocation.input_artifacts,
            (
                constants.ARTIFACT_PLAN,
                constants.ARTIFACT_DEV_HANDOFF,
                constants.ARTIFACT_PROGRESS,
                constants.ARTIFACT_IMPLEMENTATION_RESULT,
            ),
        )

    def test_apply_route_decision_to_plan_review_sets_waiting_approval(self) -> None:
        result, store, artifacts = self._workspace(task_id="task-plan-gate")
        executor = StageExecutor(result.task_workspace_path)
        publication = artifacts.publish_artifact(
            artifact_type=constants.ARTIFACT_PLAN,
            body=(
                "# Plan\n\n"
                "- plan_revision: plan-r5\n"
                "- human_confirmation_required: yes\n"
                "- human_confirmation_items: Confirm scope cut | Confirm risk tradeoff\n"
                "- active_subtask_id: subtask-002\n"
            ),
            stage=Stage.PRODUCT_OWNER_DISPATCH,
            producer=constants.ROLE_PRODUCT_OWNER,
            consumer=constants.ROLE_PRODUCT_OWNER,
            status="ready",
        )
        store.set_current_artifact(artifact_type=constants.ARTIFACT_PLAN, artifact_path=publication.current_path)

        outcome = executor._apply_route_decision(
            stage=Stage.PRODUCT_OWNER_DISPATCH,
            decision=RouteDecision(
                next_stage=Stage.HUMAN_GATE,
                task_status=TaskStatus.RUNNING,
                based_on_artifacts=(constants.ARTIFACT_PLAN,),
                human_advice_disposition=HumanAdviceDisposition.NONE,
                review_kind_requested=ReviewKind.PLAN,
            ),
            published_paths=(publication.current_path,),
            plan_snapshot=PlanSnapshot(
                plan_revision="plan-r5",
                human_confirmation_required=True,
                human_confirmation_items=("Confirm scope cut", "Confirm risk tradeoff"),
                active_subtask_id="subtask-002",
            ),
        )

        self.assertEqual(outcome.task_status, TaskStatus.WAITING_APPROVAL)
        self.assertEqual(store.load_task_context().current_stage, Stage.HUMAN_GATE)
        review_request_body = artifacts.read_current_artifact(constants.ARTIFACT_REVIEW_REQUEST).body
        self.assertIn("- review_kind: plan", review_request_body)
        self.assertIn("- plan_revision: plan-r5", review_request_body)

    def test_dispatch_to_developer_requires_approved_plan_review_when_confirmation_required(self) -> None:
        result, store, artifacts = self._workspace(task_id="task-plan-review-required")
        executor = StageExecutor(result.task_workspace_path)
        publication = artifacts.publish_artifact(
            artifact_type=constants.ARTIFACT_PLAN,
            body=(
                "# Plan\n\n"
                "- plan_revision: plan-r7\n"
                "- human_confirmation_required: yes\n"
                "- human_confirmation_items: Confirm migration risk\n"
                "- active_subtask_id: subtask-003\n"
            ),
            stage=Stage.PRODUCT_OWNER_DISPATCH,
            producer=constants.ROLE_PRODUCT_OWNER,
            consumer=constants.ROLE_PRODUCT_OWNER,
            status="ready",
        )
        store.set_current_artifact(artifact_type=constants.ARTIFACT_PLAN, artifact_path=publication.current_path)

        with self.assertRaisesRegex(ValueError, "requires human confirmation"):
            executor._validate_route_decision_constraints(
                context=store.load_task_context(),
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                decision=RouteDecision(
                    next_stage=Stage.DEVELOPER,
                    task_status=TaskStatus.RUNNING,
                    based_on_artifacts=(constants.ARTIFACT_PLAN,),
                    human_advice_disposition=HumanAdviceDisposition.NONE,
                    review_kind_requested=None,
                ),
                plan_snapshot=PlanSnapshot(
                    plan_revision="plan-r7",
                    human_confirmation_required=True,
                    human_confirmation_items=("Confirm migration risk",),
                    active_subtask_id="subtask-003",
                ),
            )

        store.update_runtime_state(
            stage=Stage.HUMAN_GATE,
            current_owner=constants.ROLE_HUMAN_GATE,
            status=TaskStatus.WAITING_APPROVAL,
        )
        publish_review_request(
            task_store=store,
            artifact_store=artifacts,
            summary="plan review",
            proposal_body="proposal",
            review_kind=ReviewKind.PLAN,
            plan_revision="plan-r7",
            focus_items=("Confirm migration risk",),
        )
        from xclaw.human_io import submit_review_decision  # local import keeps test focused

        submit_review_decision(
            task_store=store,
            artifact_store=artifacts,
            decision="approved",
            comment="approved",
        )
        executor._validate_route_decision_constraints(
            context=store.load_task_context(),
            stage=Stage.PRODUCT_OWNER_DISPATCH,
            decision=RouteDecision(
                next_stage=Stage.DEVELOPER,
                task_status=TaskStatus.RUNNING,
                based_on_artifacts=(constants.ARTIFACT_PLAN,),
                human_advice_disposition=HumanAdviceDisposition.NONE,
                review_kind_requested=None,
            ),
            plan_snapshot=PlanSnapshot(
                plan_revision="plan-r7",
                human_confirmation_required=True,
                human_confirmation_items=("Confirm migration risk",),
                active_subtask_id="subtask-003",
            ),
        )

    def test_closeout_requires_approved_delivery_review(self) -> None:
        result, store, artifacts = self._workspace(task_id="task-closeout-review")
        executor = StageExecutor(result.task_workspace_path)
        publication = artifacts.publish_artifact(
            artifact_type=constants.ARTIFACT_PLAN,
            body=(
                "# Plan\n\n"
                "- plan_revision: plan-r9\n"
                "- human_confirmation_required: no\n"
                "- human_confirmation_items: -\n"
                "- active_subtask_id: -\n"
            ),
            stage=Stage.PRODUCT_OWNER_DISPATCH,
            producer=constants.ROLE_PRODUCT_OWNER,
            consumer=constants.ROLE_PRODUCT_OWNER,
            status="ready",
        )
        store.set_current_artifact(artifact_type=constants.ARTIFACT_PLAN, artifact_path=publication.current_path)

        with self.assertRaisesRegex(ValueError, "closeout requires"):
            executor._validate_route_decision_constraints(
                context=store.load_task_context(),
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                decision=RouteDecision(
                    next_stage=Stage.CLOSEOUT,
                    task_status=TaskStatus.RUNNING,
                    based_on_artifacts=(constants.ARTIFACT_PLAN,),
                    human_advice_disposition=HumanAdviceDisposition.NONE,
                    review_kind_requested=None,
                ),
                plan_snapshot=PlanSnapshot(
                    plan_revision="plan-r9",
                    human_confirmation_required=False,
                    human_confirmation_items=(),
                    active_subtask_id=None,
                ),
            )

    def test_extract_developer_context_artifacts_field_accepts_bare_and_legacy_bullets(self) -> None:
        self.assertEqual(
            _extract_developer_context_artifacts_field(
                "# Developer Handoff\n\n- context_artifacts: progress, implementation_result\n"
            ),
            ("progress, implementation_result", None),
        )
        self.assertEqual(
            _extract_developer_context_artifacts_field(
                "# Developer Handoff\n\n- `context_artifacts: progress, implementation_result`\n"
            ),
            (
                "progress, implementation_result",
                "Parsed legacy backtick-wrapped `context_artifacts` directive from dev_handoff.",
            ),
        )

    def test_extract_developer_context_artifacts_field_ignores_other_sections(self) -> None:
        self.assertEqual(
            _extract_developer_context_artifacts_field(
                "# Plan\n\n- plan_revision: plan-r3\n\n"
                "# Developer Handoff\n\n- context_artifacts: progress, implementation_result\n"
            ),
            ("progress, implementation_result", None),
        )


if __name__ == "__main__":
    unittest.main()
