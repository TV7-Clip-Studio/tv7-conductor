from __future__ import annotations

import unittest

from orchestrator.repo_defaults import repo_defaults_for


class RepoDefaultsTests(unittest.TestCase):
    def test_two_repos_have_different_validation_templates(self) -> None:
        backend = repo_defaults_for("tv7-fastapi-backend")
        studio = repo_defaults_for("tv7-flutter-studio")
        self.assertNotEqual(backend.validation_commands, studio.validation_commands)
        self.assertIn("pytest", backend.validation_commands)
        self.assertIn("flutter test", studio.validation_commands)

    def test_owner_hints_loaded(self) -> None:
        core = repo_defaults_for("tv7-core")
        self.assertIn("Architect Agent", core.owner_hints)


if __name__ == "__main__":
    unittest.main()
