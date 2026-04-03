from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from xclaw.markdown import read_markdown_file
from xclaw.workspace import initialize_task_workspace


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


if __name__ == "__main__":
    unittest.main()
