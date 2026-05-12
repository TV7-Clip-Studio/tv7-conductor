from __future__ import annotations

import unittest

from orchestrator.repo_defaults import repo_defaults_for
from orchestrator.tickets import _issue_body, _issue_labels


class SeedIssueBodyTests(unittest.TestCase):
    def _sample_item(self, item_id: str) -> dict[str, object]:
        return {
            "item_id": item_id,
            "title": "Sample task",
            "tasks": [
                "Sample task",
                "Implement parser",
                "**NOTE**: this should not appear in seed task bullets",
            ],
            "source_file": "docs/roadmaps/core/project-conductor-roadmap.md",
            "source_line": 70,
        }

    def test_seed_body_snapshot_for_all_repos(self) -> None:
        repos = [
            "tv7-core",
            "tv7-fastapi-backend",
            "tv7-flutter-studio",
            "tv7-astro-site",
            "tv7-axum-engine",
        ]
        for idx, repo in enumerate(repos, start=1):
            item = self._sample_item(f"CO-C01-03-0{idx}")
            defaults = repo_defaults_for(repo)
            body = _issue_body(
                item,
                repo=repo,
                repo_defaults=defaults,
                batch_id="20260228-001",
            )
            self.assertIn("## Orchestrator Work Item", body)
            self.assertIn("- Item ID: `", body)
            self.assertNotIn("- Stage:", body)
            self.assertNotIn("- Assessment:", body)
            self.assertIn("## Tasks", body)
            self.assertIn("## Validation Commands", body)
            for cmd in defaults.validation_commands:
                self.assertIn(f"`{cmd}`", body)
            self.assertNotIn("## Acceptance", body)
            self.assertNotIn("## Agent Assessment", body)
            self.assertNotIn("## Risks", body)
            self.assertNotIn("## Notes", body)
            self.assertNotIn("**NOTE**", body)

    def test_seed_labels_include_required_set(self) -> None:
        defaults = repo_defaults_for("tv7-core")
        labels = _issue_labels(
            repo="tv7-core",
            batch_id="C01",
            stage="seed",
            extra_labels=list(defaults.extra_labels),
        )
        required = {
            "conductor",
            "type:task",
            "source:roadmap",
            "repo:tv7-core",
            "stage:seed",
            "batch:c01",
        }
        self.assertTrue(required.issubset(set(labels)))


if __name__ == "__main__":
    unittest.main()
