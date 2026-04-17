from __future__ import annotations

from importlib import resources
import os
from pathlib import Path
import tempfile
import unittest

from xclaw import protocol as constants
from xclaw.agent_adapter import AgentAdapter, AgentInvocation, RunnerResult
from xclaw.artifact_store import ArtifactStore
from xclaw.executor import StageExecutor
from xclaw.human_io import ensure_supervision_artifacts
from xclaw.protocol import Stage
from xclaw.task_store import TaskStore
from xclaw.workspace import initialize_task_workspace


class _FakeRunner:
    def run(self, command, *, cwd, stdin_text=None, timeout_seconds=None):
        return RunnerResult(
            command=tuple(str(item) for item in command),
            exit_code=0,
            stdout="# ok\n",
            stderr="",
        )


class AgentAdapterTest(unittest.TestCase):
    def _workspace(self, *, task_id: str, **kwargs):
        self.tmp_dir = tempfile.TemporaryDirectory()
        root = Path(self.tmp_dir.name)
        repo = root / "repo"
        repo.mkdir(parents=True, exist_ok=False)
        return initialize_task_workspace(
            target_repo_path=repo,
            task_description=task_id,
            task_id=task_id,
            workspace_root=root / "workspace",
            **kwargs,
        )

    def tearDown(self) -> None:
        tmp_dir = getattr(self, "tmp_dir", None)
        if tmp_dir is not None:
            tmp_dir.cleanup()

    def test_validate_prompt_assets_accepts_architect_plan_role_set(self) -> None:
        result = self._workspace(task_id="task-validate-prompts")
        adapter = AgentAdapter(result.task_workspace_path)
        try:
            resolved = adapter.validate_prompt_assets()
        finally:
            adapter.close()
        self.assertEqual(
            set(resolved),
            {
                constants.ROLE_PRODUCT_OWNER,
                constants.ROLE_ARCHITECT,
                constants.ROLE_DEVELOPER,
                constants.ROLE_TESTER,
            },
        )

    def test_invoke_developer_prompt_includes_plan_and_selected_context_artifacts(self) -> None:
        result = self._workspace(task_id="task-developer-prompt")
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
            store.set_current_artifact(artifact_type=artifact_type, artifact_path=publication.current_path)

        publish_current(
            constants.ARTIFACT_PLAN,
            "# Plan\n\n- plan_revision: plan-r1\n- human_confirmation_required: no\n- human_confirmation_items: -\n- active_subtask_id: subtask-001\n",
        )
        publish_current(
            constants.ARTIFACT_DEV_HANDOFF,
            "# Developer Handoff\n\n- context_artifacts: implementation_result, test_report\n",
        )
        publish_current(constants.ARTIFACT_IMPLEMENTATION_RESULT, "# Implementation Result\n")
        publish_current(constants.ARTIFACT_TEST_REPORT, "# Test Report\n")

        executor = StageExecutor(result.task_workspace_path)
        invocation = executor._build_stage_invocation(stage=Stage.DEVELOPER)
        adapter = AgentAdapter(result.task_workspace_path, runner=_FakeRunner())
        try:
            outcome = adapter.invoke(invocation)
            prompt_text = (Path(result.task_workspace_path) / outcome.prompt_path).read_text(encoding="utf-8")
        finally:
            adapter.close()

        self.assertIn("### plan (current/plan.md)", prompt_text)
        self.assertIn("### implementation_result (current/implementation_result.md)", prompt_text)
        self.assertIn("### test_report (current/test_report.md)", prompt_text)

    def test_bundled_agents_include_architect_prompt(self) -> None:
        result = self._workspace(task_id="task-bundled-architect")
        adapter = AgentAdapter(result.task_workspace_path)
        try:
            with resources.as_file(resources.files("xclaw").joinpath("agents")) as expected_agents:
                self.assertTrue((expected_agents / "architect.md").is_file())
                _, prompt_path = adapter.load_role_prompt(constants.ROLE_ARCHITECT)
                self.assertTrue(prompt_path.samefile(expected_agents / "architect.md"))
        finally:
            adapter.close()

    def test_product_owner_prompt_includes_external_bootstrap_plan(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        root = Path(self.tmp_dir.name)
        repo = root / "repo"
        plan = root / "bootstrap-plan.md"
        repo.mkdir(parents=True, exist_ok=False)
        plan.write_text("# Imported Plan\n\nBootstrap details\n", encoding="utf-8")
        result = initialize_task_workspace(
            target_repo_path=repo,
            task_description="bootstrap task",
            task_id="task-po-bootstrap-plan",
            workspace_root=root / "workspace",
            initial_stage=Stage.PRODUCT_OWNER_DISPATCH,
            bootstrap_plan_source_path=plan,
        )

        executor = StageExecutor(result.task_workspace_path)
        invocation = executor._build_stage_invocation(stage=Stage.PRODUCT_OWNER_DISPATCH)
        adapter = AgentAdapter(result.task_workspace_path, runner=_FakeRunner())
        try:
            outcome = adapter.invoke(invocation)
            prompt_text = (Path(result.task_workspace_path) / outcome.prompt_path).read_text(encoding="utf-8")
        finally:
            adapter.close()

        self.assertIn("Read only from the attached input documents below.", prompt_text)
        self.assertIn(f"### {plan.resolve()} ({plan.resolve()})", prompt_text)
        self.assertIn("Bootstrap details", prompt_text)
        self.assertIn("bootstrap_plan_source:", prompt_text)

    def test_product_owner_invocation_fails_when_external_bootstrap_plan_missing(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        root = Path(self.tmp_dir.name)
        repo = root / "repo"
        missing_plan = root / "missing-plan.md"
        repo.mkdir(parents=True, exist_ok=False)
        result = initialize_task_workspace(
            target_repo_path=repo,
            task_description="bootstrap task",
            task_id="task-po-missing-bootstrap-plan",
            workspace_root=root / "workspace",
            initial_stage=Stage.PRODUCT_OWNER_DISPATCH,
            bootstrap_plan_source_path=missing_plan,
        )

        executor = StageExecutor(result.task_workspace_path)
        invocation = executor._build_stage_invocation(stage=Stage.PRODUCT_OWNER_DISPATCH)
        adapter = AgentAdapter(result.task_workspace_path, runner=_FakeRunner())
        try:
            outcome = adapter.invoke(invocation)
        finally:
            adapter.close()

        self.assertEqual(outcome.exit_code, 2)
        self.assertIn(str(missing_plan.resolve()), outcome.missing_input_artifacts)


if __name__ == "__main__":
    unittest.main()
