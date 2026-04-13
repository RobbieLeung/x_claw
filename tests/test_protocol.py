from __future__ import annotations

import unittest

from xclaw import protocol


class ProtocolTest(unittest.TestCase):
    def test_exports_plan_review_constants(self) -> None:
        self.assertEqual(protocol.TaskStatus.WAITING_APPROVAL.value, protocol.TASK_STATUS_WAITING_APPROVAL)
        self.assertEqual(protocol.ReviewDecision.APPROVED.value, protocol.REVIEW_DECISION_APPROVED)
        self.assertEqual(protocol.ReviewKind.PLAN.value, protocol.REVIEW_KIND_PLAN)
        self.assertEqual(protocol.ReviewKind.DELIVERY.value, protocol.REVIEW_KIND_DELIVERY)

    def test_role_stage_and_artifact_sets_match_architect_plan_flow(self) -> None:
        self.assertIn(protocol.ROLE_ARCHITECT, protocol.ROLE_NAMES)
        self.assertNotIn("project_manager", protocol.ROLE_NAMES)
        self.assertNotIn("qa", protocol.ROLE_NAMES)

        self.assertIn(protocol.ARTIFACT_PLAN, protocol.ARTIFACT_TYPES)
        self.assertNotIn("requirement_spec", protocol.ARTIFACT_TYPES)
        self.assertNotIn("execution_plan", protocol.ARTIFACT_TYPES)
        self.assertNotIn("research_brief", protocol.ARTIFACT_TYPES)
        self.assertNotIn("qa_result", protocol.ARTIFACT_TYPES)

        self.assertEqual(protocol.Stage.ARCHITECT_PLANNING.value, "architect_planning")
        self.assertNotIn("project_manager_research", [stage.value for stage in protocol.Stage])
        self.assertNotIn("qa", [stage.value for stage in protocol.Stage])


if __name__ == "__main__":
    unittest.main()
