from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.review_gate import collect_pr_feedback, review_repo_items
from orchestrator.state_store import paths


class ReviewGateTests(unittest.TestCase):
    def test_collect_pr_feedback_is_deterministic(self) -> None:
        def fake_gh_json(args: list[str]):
            if args[:3] == ["pr", "view", "77"]:
                return {
                    "number": 77,
                    "url": "https://github.com/TV7-Clip-Studio/tv7-core/pull/77",
                    "title": "PR 77",
                    "state": "OPEN",
                    "headRefName": "codex/batch-tv7-core-x",
                    "baseRefName": "main",
                    "reviewDecision": "CHANGES_REQUESTED",
                }
            if args[:2] == ["api", "repos/TV7-Clip-Studio/tv7-core/pulls/77/reviews?per_page=100"]:
                return [
                    {
                        "id": 99,
                        "state": "CHANGES_REQUESTED",
                        "body": "FINAL_VERDICT: REJECTED\nMissing edge case test.",
                        "html_url": "https://example.test/review/99",
                        "user": {"login": "deepseek-bot"},
                    },
                    {
                        "id": 10,
                        "state": "APPROVED",
                        "body": "FINAL_VERDICT: APPROVED",
                        "html_url": "https://example.test/review/10",
                        "user": {"login": "deepseek-bot"},
                    },
                ]
            if args[:2] == ["api", "repos/TV7-Clip-Studio/tv7-core/issues/77/comments?per_page=100"]:
                return []
            if args[:2] == ["api", "repos/TV7-Clip-Studio/tv7-core/pulls/77/comments?per_page=100"]:
                return [
                    {
                        "id": 12,
                        "body": "Recommendation: consider adding regression coverage",
                        "path": "tooling/orchestrator/cli.py",
                        "line": 10,
                        "html_url": "https://example.test/comment/12",
                        "user": {"login": "deepseek-bot"},
                    }
                ]
            raise AssertionError(f"Unexpected gh args: {args}")

        with patch("orchestrator.review_gate._gh_json", side_effect=fake_gh_json):
            first = collect_pr_feedback(
                owner="TV7-Clip-Studio",
                repo="tv7-core",
                pr_number=77,
                include_recommendations=True,
            )
            second = collect_pr_feedback(
                owner="TV7-Clip-Studio",
                repo="tv7-core",
                pr_number=77,
                include_recommendations=True,
            )

        self.assertEqual(first, second)
        fingerprints = [entry["fingerprint"] for entry in first["findings"]]
        self.assertEqual(fingerprints, sorted(fingerprints))
        self.assertTrue(first["action_required"])
        self.assertEqual(first["verdict"], "REJECTED")
        self.assertGreaterEqual(len(first["actionable_findings"]), 1)

    def test_review_stage_transitions_and_escalation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-review-gate-") as td:
            core_dir = Path(td)
            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-07-01",
                        "title": "Approved review path",
                        "tasks": ["Approve path"],
                        "repos": ["tv7-core"],
                        "stage": "pr_ready",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 164,
                        "linked_prs": {"tv7-core": "https://github.com/TV7-Clip-Studio/tv7-core/pull/101"},
                        "attempts": {},
                    },
                    {
                        "item_id": "CO-C01-07-02",
                        "title": "Rejected review path",
                        "tasks": ["Reject path"],
                        "repos": ["tv7-core"],
                        "stage": "pr_ready",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 169,
                        "linked_prs": {"tv7-core": "https://github.com/TV7-Clip-Studio/tv7-core/pull/102"},
                        "attempts": {},
                    },
                    {
                        "item_id": "CO-C01-07-03",
                        "title": "Escalation review path",
                        "tasks": ["Escalate path"],
                        "repos": ["tv7-core"],
                        "stage": "external_review",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 174,
                        "linked_prs": {"tv7-core": "https://github.com/TV7-Clip-Studio/tv7-core/pull/103"},
                        "attempts": {"review:tv7-core": 1},
                    },
                ]
            }

            def fake_gh_json(args: list[str]):
                if args[:2] == ["pr", "view"]:
                    pr = int(args[2])
                    return {
                        "number": pr,
                        "url": f"https://github.com/TV7-Clip-Studio/tv7-core/pull/{pr}",
                        "title": f"PR {pr}",
                        "state": "OPEN",
                        "headRefName": f"codex/batch-tv7-core-{pr}",
                        "baseRefName": "main",
                        "reviewDecision": "CHANGES_REQUESTED" if pr in {102, 103} else "APPROVED",
                    }
                if args[:2] == ["api", "repos/TV7-Clip-Studio/tv7-core/pulls/101/reviews?per_page=100"]:
                    return [
                        {
                            "id": 1,
                            "state": "APPROVED",
                            "body": "FINAL_VERDICT: APPROVED",
                            "html_url": "https://example.test/review/1",
                            "user": {"login": "deepseek-bot"},
                        }
                    ]
                if args[:2] == ["api", "repos/TV7-Clip-Studio/tv7-core/pulls/102/reviews?per_page=100"]:
                    return [
                        {
                            "id": 2,
                            "state": "CHANGES_REQUESTED",
                            "body": "FINAL_VERDICT: REJECTED\nMust fix regression.",
                            "html_url": "https://example.test/review/2",
                            "user": {"login": "deepseek-bot"},
                        }
                    ]
                if args[:2] == ["api", "repos/TV7-Clip-Studio/tv7-core/pulls/103/reviews?per_page=100"]:
                    return [
                        {
                            "id": 3,
                            "state": "CHANGES_REQUESTED",
                            "body": "FINAL_VERDICT: REJECTED\nStill failing.",
                            "html_url": "https://example.test/review/3",
                            "user": {"login": "deepseek-bot"},
                        }
                    ]
                if "/issues/" in args[1] or "/pulls/" in args[1]:
                    return []
                raise AssertionError(f"Unexpected gh args: {args}")

            with (
                patch("orchestrator.review_gate.shutil.which", return_value="/usr/bin/gh"),
                patch("orchestrator.review_gate._gh_json", side_effect=fake_gh_json),
            ):
                passed, failed, escalated, skipped, planned, item_count, target_count, actions = review_repo_items(
                    core_dir=core_dir,
                    state=state,
                    owner="TV7-Clip-Studio",
                    apply_changes=True,
                    max_loops_override=1,
                    review_policy_path="",
                    execution_policy_path="",
                    include_recommendations_override=None,
                    progress=False,
                    repo_filters=[],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                    limit=0,
                    resume_from="",
                )

            self.assertEqual((passed, failed, escalated, skipped, planned), (1, 1, 1, 0, 0))
            self.assertEqual((item_count, target_count), (3, 3))
            self.assertEqual(len(actions), 3)

            by_id = {item["item_id"]: item for item in state["work_items"]}
            self.assertEqual(by_id["CO-C01-07-01"]["stage"], "integrated")
            self.assertEqual(by_id["CO-C01-07-01"]["review_verdict"], "APPROVED")
            self.assertEqual(by_id["CO-C01-07-01"]["attempts"]["review:tv7-core"], 0)

            self.assertEqual(by_id["CO-C01-07-02"]["stage"], "external_review")
            self.assertEqual(by_id["CO-C01-07-02"]["review_verdict"], "REJECTED")
            self.assertEqual(by_id["CO-C01-07-02"]["attempts"]["review:tv7-core"], 1)
            self.assertGreaterEqual(by_id["CO-C01-07-02"]["review_feedback"]["actionable_count"], 1)

            self.assertEqual(by_id["CO-C01-07-03"]["stage"], "needs_human")
            self.assertEqual(by_id["CO-C01-07-03"]["review_verdict"], "ESCALATED")
            self.assertEqual(by_id["CO-C01-07-03"]["attempts"]["review:tv7-core"], 2)
            events = paths(core_dir)["events"].read_text()
            self.assertIn("external_review.finding", events)
            self.assertIn("external_review.item", events)


if __name__ == "__main__":
    unittest.main()
