from __future__ import annotations

from importlib import resources
import os
from pathlib import Path
import tempfile
import unittest

from xclaw import protocol as constants
from xclaw.agent_adapter import AgentAdapter, AgentAdapterError
from xclaw.gateway import _resolve_launch_dir_child
from xclaw.workspace import initialize_task_workspace


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


if __name__ == "__main__":
    unittest.main()
