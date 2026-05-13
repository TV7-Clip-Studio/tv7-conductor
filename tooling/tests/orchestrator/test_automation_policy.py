from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.automation_policy import automation_violations, level_allows, load_workspace_config_optional
from orchestrator.cli import main


class AutomationPolicyTests(unittest.TestCase):
    def test_level_order_allows_only_same_or_higher_levels(self) -> None:
        self.assertTrue(level_allows("review", "execute"))
        self.assertTrue(level_allows("review", "review"))
        self.assertFalse(level_allows("plan", "apply"))
        self.assertFalse(level_allows("execute", "review"))

    def test_detects_repo_automation_violation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-automation-policy-") as td:
            root = Path(td)
            config = root / "workspace.toml"
            config.write_text(
                """
[workspace]
root = "."

[repos.repo-a]
automation_level = "plan"

[repos.repo-b]
automation_level = "review"
""".strip()
                + "\n"
            )
            workspace = load_workspace_config_optional(config)
            violations = automation_violations(
                workspace_config=workspace,
                repos=["repo-a", "repo-b"],
                required_level="execute",
                command="execute",
            )

            self.assertEqual([v.repo for v in violations], ["repo-a"])

    def test_tickets_apply_is_blocked_below_apply_level(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-automation-policy-") as td:
            root = Path(td)
            state_root = root / "docs" / ".conductor"
            state_root.mkdir(parents=True)
            (state_root / "state.json").write_text('{"work_items":[]}\n')
            (state_root / "events.jsonl").write_text("")
            config = root / "workspace.toml"
            config.write_text(
                """
[workspace]
root = "."

[repos.repo-a]
automation_level = "plan"
""".strip()
                + "\n"
            )

            with patch("orchestrator.cli.sync_repo_issues") as sync:
                code = main(
                    [
                        "--source-dir",
                        str(root),
                        "--state-dir",
                        str(state_root),
                        "--workspace-config",
                        str(config),
                        "tickets",
                        "--apply",
                        "--repo",
                        "repo-a",
                    ]
                )

            self.assertEqual(code, 2)
            sync.assert_not_called()

    def test_tickets_apply_runs_at_apply_level(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-automation-policy-") as td:
            root = Path(td)
            state_root = root / "docs" / ".conductor"
            state_root.mkdir(parents=True)
            (state_root / "state.json").write_text('{"work_items":[]}\n')
            (state_root / "events.jsonl").write_text("")
            config = root / "workspace.toml"
            config.write_text(
                """
[workspace]
root = "."

[repos.repo-a]
automation_level = "apply"
""".strip()
                + "\n"
            )

            with patch("orchestrator.cli.sync_repo_issues") as sync:
                sync.return_value = (0, 0, 0, 0, 0, 0, [])
                code = main(
                    [
                        "--source-dir",
                        str(root),
                        "--state-dir",
                        str(state_root),
                        "--workspace-config",
                        str(config),
                        "tickets",
                        "--apply",
                        "--repo",
                        "repo-a",
                    ]
                )

            self.assertEqual(code, 0)
            sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
