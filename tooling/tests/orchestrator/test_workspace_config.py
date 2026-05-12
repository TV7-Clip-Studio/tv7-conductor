from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from orchestrator.workspace_config import load_workspace_config


class WorkspaceConfigTests(unittest.TestCase):
    def test_loads_repo_profiles_with_preview_ports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-conductor-config-") as td:
            root = Path(td)
            config = root / "workspace.toml"
            config.write_text(
                """
[workspace]
root = "workspace"
state_dir = "state/.conductor"

[repos.example-api]
path = "example-api"
roadmap_sources = ["ROADMAP.md"]
labels = ["conductor", "repo:example-api"]
validation_commands = ["make check"]
automation_level = "execute"

[repos.example-api.preview]
command = "PORT=${CONDUCTOR_PREVIEW_PORT} make dev"
default_port = 5000
preview_port = 5001
health_url = "http://localhost:${CONDUCTOR_PREVIEW_PORT}/health"
""".strip()
                + "\n"
            )

            loaded = load_workspace_config(config)

            self.assertEqual(loaded.workspace_dir, (root / "workspace").resolve())
            self.assertEqual(loaded.state_dir, (root / "state" / ".conductor").resolve())
            repo = loaded.repos["example-api"]
            self.assertEqual(repo.path, (root / "workspace" / "example-api").resolve())
            self.assertEqual(repo.validation_commands, ("make check",))
            self.assertEqual(repo.preview.default_port, 5000)
            self.assertEqual(repo.preview.preview_port, 5001)
            self.assertEqual(repo.automation_level, "execute")

    def test_rejects_unknown_automation_level(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-conductor-config-") as td:
            config = Path(td) / "workspace.toml"
            config.write_text(
                """
[repos.example]
automation_level = "autopilot"
""".strip()
                + "\n"
            )

            with self.assertRaisesRegex(ValueError, "unsupported automation_level"):
                load_workspace_config(config)


if __name__ == "__main__":
    unittest.main()
