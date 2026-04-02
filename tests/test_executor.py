from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from x_claw import protocol as constants
from x_claw.artifact_store import ArtifactStore
from x_claw.executor import RouteDecision, StageExecutor, _parse_route_decision
from x_claw.human_io import (
    ensure_supervision_artifacts,
    publish_progress_update,
    read_latest_review_request_id,
    read_progress_snapshot,
)
from x_claw.protocol import HumanAdviceDisposition, Stage, TaskStatus
from x_claw.task_store import TaskStore
from x_claw.workspace import initialize_task_workspace


class ExecutorContractTest(unittest.TestCase):
    def test_parse_route_decision_requires_none_without_pending_advice(self) -> None:
        response = """
- next_stage: project_manager_research
- task_status: running
- based_on_artifacts: requirement_spec
- human_advice_disposition: accepted
""".strip()
        with self.assertRaisesRegex(ValueError, "must be `none` when no unresolved human advice exists"):
            _parse_route_decision(
                response,
                stage=Stage.PRODUCT_OWNER_REFINEMENT,
                unresolved_feedback=False,
                current_artifacts={"requirement_spec": "/tmp/requirement_spec.md"},
            )

    def test_parse_route_decision_requires_disposition_with_pending_advice(self) -> None:
        response = """
- next_stage: product_owner_dispatch
- task_status: running
- based_on_artifacts: execution_plan
- human_advice_disposition: none
""".strip()
        with self.assertRaisesRegex(ValueError, "must not be `none` while unresolved human advice exists"):
            _parse_route_decision(
                response,
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                unresolved_feedback=True,
                current_artifacts={"execution_plan": "/tmp/execution_plan.md"},
            )

    def test_apply_route_decision_to_human_gate_sets_waiting_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="executor review gate",
                task_id="task-executor-review",
                workspace_root=root / "workspace",
            )
            executor = StageExecutor(result.task_workspace_path)
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            outcome = executor._apply_route_decision(
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                decision=RouteDecision(
                    next_stage=Stage.HUMAN_GATE,
                    task_status=TaskStatus.RUNNING,
                    based_on_artifacts=(),
                    human_advice_disposition=HumanAdviceDisposition.NONE,
                ),
                published_paths=(),
            )

            context = store.load_task_context()
            self.assertEqual(outcome.task_status, TaskStatus.WAITING_APPROVAL)
            self.assertEqual(context.current_stage, Stage.HUMAN_GATE)
            self.assertEqual(context.status, TaskStatus.WAITING_APPROVAL)
            self.assertEqual(read_latest_review_request_id(artifact_store=artifacts), "review-request-0001")

    def test_build_developer_invocation_uses_po_selected_context_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="executor developer context",
                task_id="task-executor-developer-context",
                workspace_root=root / "workspace",
            )
            executor = StageExecutor(result.task_workspace_path)
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            def publish_current(artifact_type: str, body: str) -> None:
                publication = artifacts.publish_artifact(
                    artifact_type=artifact_type,
                    body=body,
                    stage=Stage.PRODUCT_OWNER_DISPATCH,
                    producer=constants.ROLE_PRODUCT_OWNER,
                    consumer=constants.ROLE_DEVELOPER,
                    status="ready",
                )
                store.set_current_artifact(
                    artifact_type=artifact_type,
                    artifact_path=publication.current_path,
                )

            publish_current(constants.ARTIFACT_REQUIREMENT_SPEC, "# Requirement Spec\n")
            publish_current(
                constants.ARTIFACT_EXECUTION_PLAN,
                "# Execution Plan\n\n- active_step_id: step-002\n",
            )
            publish_current(
                constants.ARTIFACT_DEV_HANDOFF,
                "# Developer Handoff\n\n- context_artifacts: progress, implementation_result\n",
            )
            publish_current(
                constants.ARTIFACT_IMPLEMENTATION_RESULT,
                "# Implementation Result\n\nCompleted step-001.\n",
            )
            publish_current(
                constants.ARTIFACT_TEST_REPORT,
                "# Test Report\n\n- decision: passed\n",
            )

            publish_progress_update(
                task_store=store,
                artifact_store=artifacts,
                latest_update="step-001 finished; preparing step-002",
                timeline_title="Dispatch Prep",
                timeline_body="PO is preparing the next coding round.",
                current_focus="Implement the step-002 repository changes only.",
                next_step="Developer implements step-002.",
                risks="Avoid scope creep into step-003.",
            )
            store.update_runtime_state(
                stage=Stage.DEVELOPER,
                current_owner=constants.ROLE_DEVELOPER,
                status=TaskStatus.RUNNING,
            )

            invocation = executor._build_stage_invocation(stage=Stage.DEVELOPER)

            self.assertEqual(invocation.role, constants.ROLE_DEVELOPER)
            self.assertIn(constants.ARTIFACT_PROGRESS, invocation.input_artifacts)
            self.assertIn(constants.ARTIFACT_IMPLEMENTATION_RESULT, invocation.input_artifacts)
            self.assertNotIn(constants.ARTIFACT_TEST_REPORT, invocation.input_artifacts)
            self.assertEqual(invocation.extra_context, {})

    def test_build_developer_invocation_omits_optional_context_without_po_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="executor developer no extra context",
                task_id="task-executor-developer-no-extra-context",
                workspace_root=root / "workspace",
            )
            executor = StageExecutor(result.task_workspace_path)
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            def publish_current(artifact_type: str, body: str) -> None:
                publication = artifacts.publish_artifact(
                    artifact_type=artifact_type,
                    body=body,
                    stage=Stage.PRODUCT_OWNER_DISPATCH,
                    producer=constants.ROLE_PRODUCT_OWNER,
                    consumer=constants.ROLE_DEVELOPER,
                    status="ready",
                )
                store.set_current_artifact(
                    artifact_type=artifact_type,
                    artifact_path=publication.current_path,
                )

            publish_current(constants.ARTIFACT_REQUIREMENT_SPEC, "# Requirement Spec\n")
            publish_current(constants.ARTIFACT_EXECUTION_PLAN, "# Execution Plan\n")
            publish_current(
                constants.ARTIFACT_DEV_HANDOFF,
                "# Developer Handoff\n\n- context_artifacts: -\n",
            )
            publish_current(constants.ARTIFACT_PROGRESS, "# Progress\n")
            publish_current(constants.ARTIFACT_TEST_REPORT, "# Test Report\n\n- decision: passed\n")

            invocation = executor._build_stage_invocation(stage=Stage.DEVELOPER)

            self.assertEqual(
                invocation.input_artifacts,
                (
                    constants.ARTIFACT_REQUIREMENT_SPEC,
                    constants.ARTIFACT_EXECUTION_PLAN,
                    constants.ARTIFACT_DEV_HANDOFF,
                ),
            )


    def test_sync_product_owner_progress_uses_po_summary_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="executor progress sync",
                task_id="task-executor-progress-sync",
                workspace_root=root / "workspace",
            )
            executor = StageExecutor(result.task_workspace_path)
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)

            progress_path = executor._sync_product_owner_progress(
                stage=Stage.PRODUCT_OWNER_DISPATCH,
                decision=RouteDecision(
                    next_stage=Stage.DEVELOPER,
                    task_status=TaskStatus.RUNNING,
                    based_on_artifacts=(),
                    human_advice_disposition=HumanAdviceDisposition.NONE,
                ),
                response_text=(
                    "- latest_update: PO refreshed the plan\n"
                    "- current_focus: Implement the current step only.\n"
                    "- next_step: Developer starts coding.\n"
                    "- risks: Keep current progress consumers compatible.\n"
                    "- needs_human_review: no\n"
                    "- user_summary: The PO narrowed the work to the current step and highlighted compatibility as the main risk.\n"
                ),
            )

            self.assertEqual(progress_path, "current/progress.md")
            progress = read_progress_snapshot(artifact_store=artifacts)
            self.assertEqual(progress.latest_update, "PO refreshed the plan")
            self.assertEqual(progress.current_focus, "Implement the current step only.")
            self.assertEqual(progress.next_step, "Developer starts coding.")
            self.assertEqual(progress.risks, "Keep current progress consumers compatible.")
            self.assertFalse(progress.needs_human_review)
            self.assertEqual(
                progress.user_summary,
                "The PO narrowed the work to the current step and highlighted compatibility as the main risk.",
            )


if __name__ == "__main__":
    unittest.main()
