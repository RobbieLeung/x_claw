from __future__ import annotations

from importlib import resources
import os
from pathlib import Path
import tempfile
import unittest

from xclaw import protocol as constants
from xclaw.agent_adapter import AgentAdapter, AgentAdapterError, RunnerResult
from xclaw.artifact_store import ArtifactStore
from xclaw.executor import StageExecutor
from xclaw.gateway import _resolve_launch_dir_child
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


class AgentAdapterAssetResolutionTest(unittest.TestCase):
    def _initialize_workspace(self, root: Path, *, task_id: str):
        repo = root / "repo"
        repo.mkdir(parents=True, exist_ok=False)
        return initialize_task_workspace(
            target_repo_path=repo,
            task_description="bundled assets fallback",
            task_id=task_id,
            workspace_root=root / "workspace",
        )

    def test_falls_back_to_bundled_agents_and_skills_outside_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result = self._initialize_workspace(root, task_id="task-bundled-assets")
            outside = root / "outside"
            outside.mkdir(parents=True, exist_ok=False)

            with resources.as_file(resources.files("xclaw").joinpath("agents")) as expected_agents, resources.as_file(
                resources.files("xclaw").joinpath("skills")
            ) as expected_skills:
                original_cwd = Path.cwd()
                os.chdir(outside)
                try:
                    adapter = AgentAdapter(result.task_workspace_path)
                finally:
                    os.chdir(original_cwd)

                try:
                    self.assertTrue(expected_agents.is_dir())
                    self.assertTrue(expected_skills.is_dir())
                    self.assertTrue(adapter.agents_dir.samefile(expected_agents))
                    self.assertTrue(adapter.skills_dir.samefile(expected_skills))
                    _, prompt_path = adapter.load_role_prompt(constants.ROLE_PRODUCT_OWNER)
                    self.assertTrue(prompt_path.is_file())
                    self.assertTrue(prompt_path.samefile(expected_agents / "product_owner.md"))
                finally:
                    adapter.close()

    def test_prefers_bundled_assets_over_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result = self._initialize_workspace(root, task_id="task-bundled-priority")
            outside = root / "outside"
            (outside / "agents").mkdir(parents=True, exist_ok=False)
            (outside / "skills").mkdir(parents=True, exist_ok=False)

            with resources.as_file(resources.files("xclaw").joinpath("agents")) as expected_agents, resources.as_file(
                resources.files("xclaw").joinpath("skills")
            ) as expected_skills:
                original_cwd = Path.cwd()
                os.chdir(outside)
                try:
                    adapter = AgentAdapter(result.task_workspace_path)
                finally:
                    os.chdir(original_cwd)

                try:
                    self.assertTrue(adapter.agents_dir.samefile(expected_agents))
                    self.assertTrue(adapter.skills_dir.samefile(expected_skills))
                    self.assertFalse(adapter.agents_dir.samefile(outside / "agents"))
                    self.assertFalse(adapter.skills_dir.samefile(outside / "skills"))
                finally:
                    adapter.close()

    def test_explicit_missing_skills_directory_is_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result = self._initialize_workspace(root, task_id="task-missing-skills")
            missing_skills_dir = root / "missing-skills"

            with self.assertRaisesRegex(AgentAdapterError, "skills directory does not exist"):
                AgentAdapter(result.task_workspace_path, skills_dir=missing_skills_dir)

    def test_gateway_launch_dir_child_ignores_missing_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.assertIsNone(_resolve_launch_dir_child(str(root), "agents"))

            agents_dir = root / "agents"
            agents_dir.mkdir(parents=True, exist_ok=False)
            self.assertEqual(_resolve_launch_dir_child(str(root), "agents"), str(agents_dir.resolve()))

    def test_invoke_developer_prompt_includes_selected_context_artifacts_and_run_log_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result = self._initialize_workspace(root, task_id="task-developer-context-prompt")
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
                "# Developer Handoff\n\n- `context_artifacts: implementation_result, test_report`\n",
            )
            publish_current(constants.ARTIFACT_IMPLEMENTATION_RESULT, "# Implementation Result\n")
            publish_current(constants.ARTIFACT_TEST_REPORT, "# Test Report\n")

            executor = StageExecutor(result.task_workspace_path)
            invocation = executor._build_stage_invocation(stage=Stage.DEVELOPER)
            adapter = AgentAdapter(result.task_workspace_path, runner=_FakeRunner())

            try:
                outcome = adapter.invoke(invocation)
                prompt_text = (Path(result.task_workspace_path) / outcome.prompt_path).read_text(encoding="utf-8")
                run_log_text = (Path(result.task_workspace_path) / outcome.run_log_path).read_text(encoding="utf-8")
            finally:
                adapter.close()

            self.assertIn("## Extra Context", prompt_text)
            self.assertIn("- resolved_context_artifacts: implementation_result, test_report", prompt_text)
            self.assertIn("### implementation_result (current/implementation_result.md)", prompt_text)
            self.assertIn("### test_report (current/test_report.md)", prompt_text)
            self.assertIn(
                "extra_context=context_artifacts_warning=Parsed legacy backtick-wrapped `context_artifacts` directive from dev_handoff.; resolved_context_artifacts=implementation_result, test_report",
                run_log_text,
            )



if __name__ == "__main__":
    unittest.main()
