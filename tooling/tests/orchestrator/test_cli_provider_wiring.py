from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.cli import main


class CliProviderWiringTests(unittest.TestCase):
    def _write_state(self, root: Path, stage: str = "executing") -> None:
        state_root = root / "docs" / ".conductor"
        state_root.mkdir(parents=True)
        payload = {
            "version": 3,
            "schema": {"name": "project-conductor-state", "version": 3},
            "work_items": [
                {
                    "item_id": "CD-A01-07-06",
                    "title": "Wire providers",
                    "tasks": ["Wire providers"],
                    "repos": ["repo-a"],
                    "stage": stage,
                    "source_file": str(root / "docs" / "roadmaps" / "tv7-conductor-roadmap.md"),
                    "source_line": 1,
                    "attempts": {},
                    "review_feedback": {},
                }
            ],
            "iterations": [],
            "agent_runs": [],
            "idempotency": {"keys": {}},
            "planning": {"idea_runs": [], "intent_queue": []},
        }
        (state_root / "state.json").write_text(json.dumps(payload, indent=2) + "\n")
        (state_root / "events.jsonl").write_text("")

    def test_local_review_resolves_default_local_reviewer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-cli-provider-") as td:
            root = Path(td)
            repo = root / "repo-a"
            repo.mkdir()
            self._write_state(root)

            code = main(
                [
                    "--source-dir",
                    str(root),
                    "--state-dir",
                    str(root / "docs" / ".conductor"),
                    "--workspace-dir",
                    str(root),
                    "local-review",
                    "--apply",
                    "--repo",
                    "repo-a",
                    "--command",
                    "true",
                ]
            )

            self.assertEqual(code, 0)
            state = json.loads((root / "docs" / ".conductor" / "state.json").read_text())
            item = state["work_items"][0]
            self.assertEqual(item["stage"], "pr_ready")
            self.assertEqual(item["review_verdict"], "LOCAL_REVIEW_PASSED")

    def test_external_review_blocks_paid_provider_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-cli-provider-") as td:
            root = Path(td)
            self._write_state(root, stage="pr_ready")

            code = main(
                [
                    "--source-dir",
                    str(root),
                    "--state-dir",
                    str(root / "docs" / ".conductor"),
                    "external-review",
                    "--repo",
                    "repo-a",
                ]
            )

            self.assertEqual(code, 2)

    def test_review_compatibility_alias_still_blocks_paid_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-cli-provider-") as td:
            root = Path(td)
            self._write_state(root, stage="pr_ready")

            code = main(
                [
                    "--source-dir",
                    str(root),
                    "--state-dir",
                    str(root / "docs" / ".conductor"),
                    "review",
                    "--repo",
                    "repo-a",
                ]
            )

            self.assertEqual(code, 2)

    def test_execute_rejects_non_ao_executor_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-cli-provider-") as td:
            root = Path(td)
            self._write_state(root, stage="expanded")
            provider_path = root / "providers.toml"
            provider_path.write_text(
                """
[providers.local_exec]
role = "executor"
kind = "local_command"
command = "codex"

[providers.planner]
role = "planner"
kind = "local_command"

[providers.reviewer]
role = "reviewer"
kind = "local_command"

[defaults]
planner = "planner"
executor = "local_exec"
reviewer = "reviewer"
""".strip()
                + "\n"
            )

            code = main(
                [
                    "--source-dir",
                    str(root),
                    "--state-dir",
                    str(root / "docs" / ".conductor"),
                    "execute",
                    "--provider-config",
                    str(provider_path),
                    "--repo",
                    "repo-a",
                ]
            )

            self.assertEqual(code, 2)

    def test_tickets_uses_planner_provider_for_assessment_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-cli-provider-") as td:
            root = Path(td)
            self._write_state(root, stage="seeded")

            with patch("orchestrator.cli.sync_repo_issues") as sync:
                sync.return_value = (0, 0, 0, 1, 1, 1, [])
                code = main(
                    [
                        "--source-dir",
                        str(root),
                        "--state-dir",
                        str(root / "docs" / ".conductor"),
                        "tickets",
                        "--repo",
                        "repo-a",
                    ]
                )

            self.assertEqual(code, 0)
            kwargs = sync.call_args.kwargs
            self.assertEqual(kwargs["assess_agent"], "gemini")
            self.assertEqual(kwargs["agent_model"], "gemini-2.5-pro")

    def test_expand_uses_planner_provider_for_expansion_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-cli-provider-") as td:
            root = Path(td)
            self._write_state(root, stage="seeded")

            with patch("orchestrator.cli.expand_repo_issues") as expand:
                expand.return_value = (0, 0, 0, 0, 1, 1, [])
                code = main(
                    [
                        "--source-dir",
                        str(root),
                        "--state-dir",
                        str(root / "docs" / ".conductor"),
                        "expand",
                        "--repo",
                        "repo-a",
                    ]
                )

            self.assertEqual(code, 0)
            kwargs = expand.call_args.kwargs
            self.assertEqual(kwargs["expand_agent"], "gemini")
            self.assertEqual(kwargs["expand_model"], "gemini-2.5-pro")


if __name__ == "__main__":
    unittest.main()
