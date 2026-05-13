from __future__ import annotations

import unittest

from orchestrator.execution_policy import load_execution_policy


class ExecutionPolicyTests(unittest.TestCase):
    def test_docs_work_uses_docs_mode(self) -> None:
        policy = load_execution_policy()
        item = {
            "title": "Update runbook docs for execute handoff",
            "tasks": ["Document command flags and examples"],
            "source_file": "docs/repos/core/project-conductor-runbook.md",
        }
        mode = policy.sandbox_mode_for(item=item, repo="tv7-core")
        self.assertEqual(mode, "none")
        self.assertEqual(policy.classify_work_type(item), "docs")

    def test_repo_override_can_force_container_mode(self) -> None:
        policy = load_execution_policy()
        item = {
            "title": "Refactor engine execution service",
            "tasks": ["Update Rust API and tests"],
            "source_file": "docs/roadmaps/tv7-axum-engine-roadmap.md",
        }
        mode = policy.sandbox_mode_for(item=item, repo="tv7-axum-engine")
        self.assertEqual(mode, "container")
        self.assertGreaterEqual(policy.timeout_for_mode(mode), 30)


if __name__ == "__main__":
    unittest.main()
