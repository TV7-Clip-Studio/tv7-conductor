from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from orchestrator.lifecycle import WorkStage
from orchestrator.local_review import run_local_review_items


class LocalReviewTests(unittest.TestCase):
    def _state(self, core_dir: Path) -> dict[str, object]:
        return {
            "work_items": [
                {
                    "item_id": "CD-A01-06-01",
                    "title": "Run local review",
                    "tasks": ["Run local review"],
                    "repos": ["tv7-conductor"],
                    "stage": WorkStage.EXECUTING.value,
                    "source_file": str(core_dir / "docs" / "roadmaps" / "tv7-conductor-roadmap.md"),
                    "source_line": 10,
                    "attempts": {},
                    "review_feedback": {},
                }
            ],
            "agent_runs": [],
            "iterations": [],
        }

    def test_local_review_dry_run_plans_without_mutating_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-local-review-") as td:
            root = Path(td)
            repo = root / "tv7-conductor"
            repo.mkdir()
            state = self._state(root)

            passed, failed, skipped, planned, item_count, target_count, actions = run_local_review_items(
                core_dir=root,
                state=state,
                apply_changes=False,
                workspace_dir=str(root),
                repo_path_override="",
                worktree_path_override="",
                commands=["true"],
                timeout_sec=30,
                repo_filters=["tv7-conductor"],
                roadmap_filters=[],
                item_prefix_filters=[],
            )

            self.assertEqual((passed, failed, skipped, planned), (0, 0, 0, 1))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "would_local_review")
            self.assertEqual(state["work_items"][0]["stage"], WorkStage.EXECUTING.value)  # type: ignore[index]

    def test_local_review_pass_promotes_to_pr_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-local-review-") as td:
            root = Path(td)
            repo = root / "tv7-conductor"
            repo.mkdir()
            state = self._state(root)

            passed, failed, skipped, planned, item_count, target_count, actions = run_local_review_items(
                core_dir=root,
                state=state,
                apply_changes=True,
                workspace_dir=str(root),
                repo_path_override="",
                worktree_path_override="",
                commands=["true"],
                timeout_sec=30,
                repo_filters=["tv7-conductor"],
                roadmap_filters=[],
                item_prefix_filters=[],
            )

            self.assertEqual((passed, failed, skipped, planned), (1, 0, 0, 0))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "local_review_passed")
            item = state["work_items"][0]  # type: ignore[index]
            self.assertEqual(item["stage"], WorkStage.PR_READY.value)
            self.assertEqual(item["review_verdict"], "LOCAL_REVIEW_PASSED")
            self.assertEqual(len(item["stage_history"]), 2)

    def test_local_review_failure_stays_in_local_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-local-review-") as td:
            root = Path(td)
            repo = root / "tv7-conductor"
            repo.mkdir()
            state = self._state(root)

            passed, failed, skipped, planned, item_count, target_count, actions = run_local_review_items(
                core_dir=root,
                state=state,
                apply_changes=True,
                workspace_dir=str(root),
                repo_path_override="",
                worktree_path_override="",
                commands=["false"],
                timeout_sec=30,
                repo_filters=["tv7-conductor"],
                roadmap_filters=[],
                item_prefix_filters=[],
            )

            self.assertEqual((passed, failed, skipped, planned), (0, 1, 0, 0))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "local_review_failed")
            item = state["work_items"][0]  # type: ignore[index]
            self.assertEqual(item["stage"], WorkStage.LOCAL_REVIEW.value)
            self.assertEqual(item["review_verdict"], "LOCAL_REVIEW_FAILED")


if __name__ == "__main__":
    unittest.main()
