from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from xclaw.gateway import GatewayRunConfig, TaskGateway
from xclaw.human_io import ensure_supervision_artifacts, read_progress_snapshot
from xclaw.protocol import Stage, TaskStatus
from xclaw.executor import StageOutcome
from xclaw.task_store import TaskStore
from xclaw.artifact_store import ArtifactStore
from xclaw.workspace import initialize_task_workspace


class GatewayTest(unittest.TestCase):
    def test_record_execution_result_marks_human_review_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=False)
            result = initialize_task_workspace(
                target_repo_path=repo,
                task_description="gateway waiting approval",
                task_id="task-gateway-waiting-approval",
                workspace_root=root / "workspace",
            )
            gateway = TaskGateway(GatewayRunConfig(task_workspace_path=result.task_workspace_path))
            store = TaskStore(result.task_workspace_path)
            artifacts = ArtifactStore(result.task_workspace_path)
            ensure_supervision_artifacts(task_store=store, artifact_store=artifacts)
            store.update_runtime_state(
                stage=Stage.HUMAN_GATE,
                current_owner="human_gate",
                status=TaskStatus.WAITING_APPROVAL,
            )

            gateway._record_execution_result(
                outcome=StageOutcome(
                    task_id=result.task_id,
                    stage_executed=Stage.PRODUCT_OWNER_DISPATCH,
                    next_stage=Stage.HUMAN_GATE,
                    task_status=TaskStatus.WAITING_APPROVAL,
                    current_owner="human_gate",
                    published_artifacts=(),
                    message="waiting for human review",
                )
            )

            progress = read_progress_snapshot(artifact_store=artifacts)
            self.assertTrue(progress.needs_human_review)
            self.assertEqual(progress.latest_update, "waiting for human review")
            self.assertEqual(progress.current_focus, "Waiting for human review on the current proposal.")


if __name__ == "__main__":
    unittest.main()
