from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from xclaw import protocol as constants
from xclaw.agent_adapter import AgentInvocation, AgentInvocationResult
from xclaw.artifact_store import ArtifactStore
from xclaw.executor import RouteDecision, StageExecutor, _parse_route_decision
from xclaw.models import AgentResult
from xclaw.human_io import (
    ensure_supervision_artifacts,
    publish_progress_update,
    read_latest_review_request_id,
    read_progress_snapshot,
)
from xclaw.protocol import (
    AgentExecutionStatus,
    AgentResultType,
    HumanAdviceDisposition,
    Stage,
    TaskStatus,
)
from xclaw.task_store import TaskStore
from xclaw.workspace import initialize_task_workspace


def _fake_invocation_result(*, role: str, stage: Stage) -> AgentInvocationResult:
    invocation = AgentInvocation(
        role=role,
        objective=f"test {stage.value}",
        stage=stage,
        input_artifacts=(),
    )
    agent_result = AgentResult(
        target_repo_path="/tmp/repo",
        artifact_path="runs/0001/response.md",
        result_id=f"result-{stage.value}",
        role=role,
        result_type=(
            AgentResultType.TEST_RESULT
            if stage == Stage.TESTER
            else AgentResultType.QA_RESULT
        ),
        execution_status=AgentExecutionStatus.SUCCEEDED,
        summary=f"{stage.value} completed",
    )
    return AgentInvocationResult(
        invocation=invocation,
        agent_result=agent_result,
        command=("codex",),
        exit_code=0,
        prompt_path=f"runs/0001_{role}/prompt.md",
        response_path=f"runs/0001_{role}/response.md",
        run_log_path=f"runs/0001_{role}/run.log",
        run_directory=f"runs/0001_{role}",
        input_artifacts=(),
        missing_input_artifacts=(),
    )


class ExecutorContractTest(unittest.TestCase):
    def test_execute_tester_accepts_report_without_explicit_decision_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="executor tester routing",
                task_id="task-executor-tester-routing",
                workspace_root=root / "workspace",
            )
            executor = StageExecutor(result.task_workspace_path)
            store = TaskStore(result.task_workspace_path)

            store.update_runtime_state(
                stage=Stage.TESTER,
                current_owner=constants.ROLE_TESTER,
                status=TaskStatus.RUNNING,
            )
            executor._invoke_role_stage = lambda **_: (
                constants.ROLE_TESTER,
                _fake_invocation_result(role=constants.ROLE_TESTER, stage=Stage.TESTER),
                "# Test Report\n\n- current_step_conclusion: passed with noted gaps\n",
            )

            outcome = executor._execute_tester()

            context = store.load_task_context()
            self.assertEqual(outcome.next_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.current_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.status, TaskStatus.RUNNING)
            self.assertIn(constants.ARTIFACT_TEST_REPORT, context.current_artifacts)
            actions = [event.action for event in store.list_events()]
            self.assertNotIn("tester_decision_invalid", actions)
            self.assertNotIn("tester_failed_route_to_product_owner", actions)

    def test_execute_qa_accepts_result_without_explicit_decision_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="executor qa routing",
                task_id="task-executor-qa-routing",
                workspace_root=root / "workspace",
            )
            executor = StageExecutor(result.task_workspace_path)
            store = TaskStore(result.task_workspace_path)

            store.update_runtime_state(
                stage=Stage.QA,
                current_owner=constants.ROLE_QA,
                status=TaskStatus.RUNNING,
            )
            executor._invoke_role_stage = lambda **_: (
                constants.ROLE_QA,
                _fake_invocation_result(role=constants.ROLE_QA, stage=Stage.QA),
                "# QA Result\n\n- acceptance_conclusion: not ready for human gate yet\n",
            )

            outcome = executor._execute_qa()

            context = store.load_task_context()
            self.assertEqual(outcome.next_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.current_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.status, TaskStatus.RUNNING)
            self.assertIn(constants.ARTIFACT_QA_RESULT, context.current_artifacts)
            self.assertNotIn(constants.ARTIFACT_REPAIR_TICKET, context.current_artifacts)
            actions = [event.action for event in store.list_events()]
            self.assertNotIn("qa_decision_invalid", actions)
            self.assertNotIn("qa_failed_route_to_product_owner", actions)

    def test_repair_loop_increment_follows_po_based_on_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="executor repair loop reasons",
                task_id="task-executor-repair-loop",
                workspace_root=root / "workspace",
            )
            executor = StageExecutor(result.task_workspace_path)
            store = TaskStore(result.task_workspace_path)

            increment = executor._validate_repair_loop_increment(
                context=store.load_task_context(),
                decision=RouteDecision(
                    next_stage=Stage.DEVELOPER,
                    task_status=TaskStatus.RUNNING,
                    based_on_artifacts=(
                        constants.ARTIFACT_TEST_REPORT,
                        constants.ARTIFACT_QA_RESULT,
                    ),
                    human_advice_disposition=HumanAdviceDisposition.NONE,
                ),
            )

            self.assertIsNotNone(increment)
            assert increment is not None
            self.assertEqual(increment.loop_count, 1)
            self.assertEqual(
                increment.reasons,
                (
                    constants.ARTIFACT_TEST_REPORT,
                    constants.ARTIFACT_QA_RESULT,
                ),
            )

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
