from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orchestrator import cli


class FixtureE2EScenarioTests(unittest.TestCase):
    def test_fixture_project_reaches_pr_ready_without_github_or_paid_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conductor-e2e-fixture-") as td:
            root = Path(td)
            project = root / "example-project"
            state_dir = root / ".conductor"
            worktree_root = root / "worktrees"
            workspace_config = root / "workspace.toml"
            roadmap = project / "ROADMAP.md"
            project.mkdir()
            (project / "app.txt").write_text("ready\n")
            roadmap.write_text(
                "\n".join(
                    [
                        "# Example Project Roadmap",
                        "Roadmap Code: EX",
                        "Batch Code: A01",
                        "",
                        "## Phase 1",
                        "- [ ] [EX-A01-01-01] Make example project ready",
                    ]
                )
                + "\n"
            )
            workspace_config.write_text(
                f"""
[workspace]
root = "{root}"
state_dir = "{state_dir}"

[repos.example-project]
path = "example-project"
roadmap_sources = ["example-project/ROADMAP.md"]
validation_commands = ["{sys.executable} -c \\"from pathlib import Path; assert Path('app.txt').read_text() == 'ready\\\\n'\\""]
automation_level = "review"
""".strip()
                + "\n"
            )

            subprocess.run(["git", "init"], cwd=project, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=project, check=True)
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(["git", "commit", "-m", "Initial fixture project"], cwd=project, check=True, stdout=subprocess.DEVNULL)

            with redirect_stdout(StringIO()):
                rc = cli.main(
                    [
                        "--core-dir",
                        str(project),
                        "--workspace-dir",
                        str(root),
                        "--conductor-dir",
                        str(root / "conductor"),
                        "--state-dir",
                        str(state_dir),
                        "--workspace-config",
                        str(workspace_config),
                        "plan",
                        "--single-roadmap",
                        "--roadmap",
                        str(roadmap),
                        "--default-repos",
                        "example-project",
                    ]
                )
            self.assertEqual(rc, 0)

            state_path = state_dir / "state.json"
            state = json.loads(state_path.read_text())
            self.assertEqual(state["work_items"][0]["item_id"], "EX-A01-01-01")
            self.assertEqual(state["work_items"][0]["repos"], ["example-project"])

            with redirect_stdout(StringIO()):
                rc = cli.main(
                    [
                        "--core-dir",
                        str(project),
                        "--workspace-dir",
                        str(root),
                        "--conductor-dir",
                        str(root / "conductor"),
                        "--state-dir",
                        str(state_dir),
                        "--workspace-config",
                        str(workspace_config),
                        "prepare-worktree",
                        "--repo",
                        "example-project",
                        "--repo-path",
                        str(project),
                        "--item-id",
                        "EX-A01-01-01",
                        "--worktree-root",
                        str(worktree_root),
                        "--apply",
                    ]
                )
            self.assertEqual(rc, 0)
            worktree_path = worktree_root / "example-project" / "EX-A01-01-01"
            self.assertTrue(worktree_path.is_dir())

            state = json.loads(state_path.read_text())
            state["work_items"][0]["stage"] = "executing"
            state_path.write_text(json.dumps(state, indent=2) + "\n")

            review_command = (
                f"{sys.executable} -c "
                "\"from pathlib import Path; assert Path('app.txt').read_text() == 'ready\\n'\""
            )
            with redirect_stdout(StringIO()):
                rc = cli.main(
                    [
                        "--core-dir",
                        str(project),
                        "--workspace-dir",
                        str(root),
                        "--conductor-dir",
                        str(root / "conductor"),
                        "--state-dir",
                        str(state_dir),
                        "--workspace-config",
                        str(workspace_config),
                        "local-review",
                        "--apply",
                        "--repo",
                        "example-project",
                        "--item-prefix",
                        "EX-A01-01-01",
                        "--repo-path",
                        str(project),
                        "--worktree-path",
                        str(worktree_path),
                        "--command",
                        review_command,
                    ]
                )
            self.assertEqual(rc, 0)

            state = json.loads(state_path.read_text())
            item = state["work_items"][0]
            self.assertEqual(item["stage"], "pr_ready")
            self.assertEqual(item["review_verdict"], "LOCAL_REVIEW_PASSED")
            self.assertEqual(item["review_feedback"]["local_review"]["actionable_count"], 0)
            reports = sorted((state_dir / "reports" / "local-review").glob("*.json"))
            self.assertTrue(reports)


if __name__ == "__main__":
    unittest.main()
