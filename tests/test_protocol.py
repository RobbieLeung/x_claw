from __future__ import annotations

import unittest

from xclaw import protocol


class ProtocolTest(unittest.TestCase):
    def test_exports_new_status_and_review_values(self) -> None:
        self.assertEqual(protocol.TaskStatus.RUNNING.value, protocol.TASK_STATUS_RUNNING)
        self.assertEqual(protocol.TaskStatus.WAITING_APPROVAL.value, protocol.TASK_STATUS_WAITING_APPROVAL)
        self.assertNotIn("waiting_human_feedback", protocol.TASK_STATUS_NAMES)
        self.assertEqual(protocol.ReviewDecision.APPROVED.value, protocol.REVIEW_DECISION_APPROVED)
        self.assertEqual(protocol.REVIEW_DECISION_NAMES, ("approved", "rejected"))

    def test_artifact_sets_match_simplified_supervision_model(self) -> None:
        self.assertIn(protocol.ARTIFACT_PROGRESS, protocol.ARTIFACT_TYPES)
        self.assertIn(protocol.ARTIFACT_HUMAN_ADVICE_LOG, protocol.ARTIFACT_TYPES)
        self.assertIn(protocol.ARTIFACT_REVIEW_REQUEST, protocol.ARTIFACT_TYPES)
        self.assertIn(protocol.ARTIFACT_REVIEW_DECISION, protocol.ARTIFACT_TYPES)
        self.assertNotIn("conversation", protocol.ARTIFACT_TYPES)
        self.assertNotIn("human_input", protocol.ARTIFACT_TYPES)
        self.assertNotIn("human_feedback", protocol.ARTIFACT_TYPES)
        self.assertNotIn("approval", protocol.ARTIFACT_TYPES)


if __name__ == "__main__":
    unittest.main()
