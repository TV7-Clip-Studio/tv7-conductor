from __future__ import annotations

import unittest

from orchestrator.lifecycle import WorkStage, assert_stage_transition, transition_item_stage


class LifecycleTests(unittest.TestCase):
    def test_valid_transition_path(self) -> None:
        path = [
            (WorkStage.SEEDED.value, WorkStage.EXPANDED.value),
            (WorkStage.EXPANDED.value, WorkStage.EXECUTING.value),
            (WorkStage.EXECUTING.value, WorkStage.LOCAL_REVIEW.value),
            (WorkStage.LOCAL_REVIEW.value, WorkStage.PR_READY.value),
            (WorkStage.PR_READY.value, WorkStage.EXTERNAL_REVIEW.value),
            (WorkStage.EXTERNAL_REVIEW.value, WorkStage.INTEGRATED.value),
            (WorkStage.INTEGRATED.value, WorkStage.DONE.value),
        ]
        for from_stage, to_stage in path:
            assert_stage_transition(from_stage, to_stage)

    def test_legacy_review_stages_remain_compatible(self) -> None:
        path = [
            (WorkStage.EXECUTING.value, WorkStage.REVIEW_FAILED.value),
            (WorkStage.REVIEW_FAILED.value, WorkStage.EXECUTING.value),
            (WorkStage.EXECUTING.value, WorkStage.REVIEW_PASSED.value),
            (WorkStage.REVIEW_PASSED.value, WorkStage.INTEGRATED.value),
        ]
        for from_stage, to_stage in path:
            assert_stage_transition(from_stage, to_stage)

    def test_illegal_transition_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "illegal stage transition"):
            assert_stage_transition(WorkStage.DONE.value, WorkStage.SEEDED.value)

    def test_transition_item_records_history(self) -> None:
        item = {"stage": WorkStage.SEEDED.value}
        transition_item_stage(item, WorkStage.EXPANDED.value, reason="expand")
        self.assertEqual(item["stage"], WorkStage.EXPANDED.value)
        self.assertEqual(len(item.get("stage_history", [])), 1)
        self.assertEqual(item["stage_history"][0]["from"], WorkStage.SEEDED.value)
        self.assertEqual(item["stage_history"][0]["to"], WorkStage.EXPANDED.value)


if __name__ == "__main__":
    unittest.main()
