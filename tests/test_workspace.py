from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from xclaw.markdown import read_markdown_file
from xclaw.task_store import TaskStore
from xclaw.protocol import Stage, TaskStatus
from xclaw.workspace import find_latest_task_workspace, initialize_task_workspace


class WorkspaceTest(unittest.TestCase):
    def test_task_template_drops_interaction_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="workspace init",
                task_id="task-workspace",
                workspace_root=root / "workspace",
            )
            document = read_markdown_file(Path(result.task_file_path))
            self.assertNotIn("interaction_mode", document.body)
            self.assertIn("- gateway_pid:", document.body)


    def test_find_latest_task_workspace_prefers_recent_task_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            workspace_root = root / "workspace"
            older = initialize_task_workspace(
                target_repo_path=repo,
                task_description="older",
                task_id="task-20240101-000000-old",
                workspace_root=workspace_root,
            )
            latest = initialize_task_workspace(
                target_repo_path=repo,
                task_description="latest",
                task_id="task-20240102-000000-new",
                workspace_root=workspace_root,
            )
            TaskStore(older.task_workspace_path).update_runtime_state(
                stage=Stage.DEVELOPER,
                current_owner="developer",
                status=TaskStatus.FAILED,
                gateway_pid=None,
            )
            TaskStore(latest.task_workspace_path).update_runtime_state(
                stage=Stage.TESTER,
                current_owner="tester",
                status=TaskStatus.TERMINATED,
                gateway_pid=None,
            )

            discovered = find_latest_task_workspace(workspace_root)
            self.assertIsNotNone(discovered)
            assert discovered is not None
            self.assertEqual(discovered.task_id, "task-20240102-000000-new")
            self.assertEqual(discovered.task_workspace_path, latest.task_workspace_path)

    def test_initialize_task_workspace_supports_bootstrap_plan_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            plan = root / "plan.md"
            repo.mkdir(parents=True, exist_ok=False)
            plan.write_text("# Imported Plan\n", encoding="utf-8")

            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="bootstrap from plan",
                task_id="task-bootstrap-plan",
                workspace_root=root / "workspace",
                initial_stage=Stage.PRODUCT_OWNER_DISPATCH,
                bootstrap_plan_source_path=plan,
            )

            context = TaskStore(result.task_workspace_path).load_task_context()
            self.assertEqual(context.current_stage, Stage.PRODUCT_OWNER_DISPATCH)
            self.assertEqual(context.current_owner, "product_owner")
            self.assertEqual(context.bootstrap_plan_source_path, str(plan.resolve()))

            document = read_markdown_file(Path(result.task_file_path))
            self.assertIn("| bootstrap_plan_source_path |", document.body)
            self.assertIn(str(plan.resolve()), document.body)

    def test_task_store_load_task_context_accepts_missing_bootstrap_plan_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="legacy workspace",
                task_id="task-legacy-workspace",
                workspace_root=root / "workspace",
            )

            task_path = Path(result.task_file_path)
            original = task_path.read_text(encoding="utf-8")
            task_path.write_text(
                original.replace("| bootstrap_plan_source_path | - |\n", ""),
                encoding="utf-8",
            )

            context = TaskStore(result.task_workspace_path).load_task_context()
            self.assertIsNone(context.bootstrap_plan_source_path)


if __name__ == "__main__":
    unittest.main()
