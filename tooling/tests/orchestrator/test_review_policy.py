from __future__ import annotations

import unittest

from orchestrator.review_policy import load_review_policy


class ReviewPolicyTests(unittest.TestCase):
    def test_max_loop_resolution(self) -> None:
        policy = load_review_policy()
        self.assertEqual(policy.max_loops_for(repo="tv7-axum-engine", task_class="code"), 3)
        self.assertEqual(policy.max_loops_for(repo="tv7-core", task_class="docs"), 1)
        self.assertEqual(policy.max_loops_for(repo="tv7-core", task_class="code"), 2)


if __name__ == "__main__":
    unittest.main()
