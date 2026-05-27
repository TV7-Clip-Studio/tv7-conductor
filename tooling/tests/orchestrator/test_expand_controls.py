from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.tickets import expand_repo_issues


class ExpandControlsTests(unittest.TestCase):
    def test_limit_resume_and_dry_run_diff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-expand-controls-") as td:
            core_dir = Path(td)
            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-04-01",
                        "title": "First",
                        "tasks": ["First"],
                        "repos": ["tv7-core"],
                        "source_file": str(core_dir / "docs/roadmaps/core/project-conductor-roadmap.md"),
                        "source_line": 83,
                        "issues": {"tv7-core": {"url": "https://example.test/issue/1"}},
                    },
                    {
                        "item_id": "CO-C01-04-02",
                        "title": "Second",
                        "tasks": ["Second"],
                        "repos": ["tv7-core"],
                        "source_file": str(core_dir / "docs/roadmaps/core/project-conductor-roadmap.md"),
                        "source_line": 88,
                        "issues": {"tv7-core": {"url": "https://example.test/issue/2"}},
                    },
                ]
            }

            def fake_gh_json(args: list[str]):
                issue_ref = args[2]
                if issue_ref.endswith("/1"):
                    return {
                        "number": 1,
                        "url": "https://example.test/issue/1",
                        "title": "[CO-C01-04-01] First",
                        "state": "OPEN",
                        "body": "## Orchestrator Work Item\n\n- Item ID: `CO-C01-04-01`\n",
                    }
                return {
                    "number": 2,
                    "url": "https://example.test/issue/2",
                    "title": "[CO-C01-04-02] Second",
                    "state": "OPEN",
                    "body": "## Orchestrator Work Item\n\n- Item ID: `CO-C01-04-02`\n",
                }

            with (
                patch("orchestrator.tickets.shutil.which", return_value="/usr/bin/gh"),
                patch("orchestrator.tickets._gh_json", side_effect=fake_gh_json),
            ):
                expanded, unchanged, skipped, fallback, item_count, target_count, actions = expand_repo_issues(
                    core_dir=core_dir,
                    state=state,
                    owner="TV7-Clip-Studio",
                    apply_changes=False,
                    strict_expand=False,
                    batch_id="20260228-001",
                    progress=False,
                    repo_filters=["tv7-core"],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                    limit=1,
                    resume_from="CO-C01-04-02",
                    dry_run_diff=True,
                )

            self.assertEqual(item_count, 2)
            self.assertEqual(target_count, 2)
            self.assertEqual(expanded, 1)
            self.assertEqual(unchanged, 0)
            self.assertEqual(skipped, 0)
            self.assertEqual(fallback, 0)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["item_id"], "CO-C01-04-02")
            self.assertIn("diff=", actions[0]["note"])
            diff_path = Path(str(actions[0]["note"]).split("diff=", 1)[1])
            self.assertTrue(diff_path.exists())

    def test_apply_preserves_executing_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-expand-controls-") as td:
            core_dir = Path(td)
            state = {
                "work_items": [
                    {
                        "item_id": "ST-A01-02-01",
                        "title": "Preserve stage while expanding",
                        "tasks": ["Preserve executing stage"],
                        "repos": ["tv7-astro-site"],
                        "stage": "executing",
                        "source_file": str(core_dir / "docs/roadmaps/tv7-astro-site-roadmap.md"),
                        "source_line": 15,
                        "issues": {"tv7-astro-site": {"url": "https://example.test/issue/28"}},
                    }
                ]
            }

            def fake_gh_json(_args: list[str]):
                return {
                    "number": 28,
                    "url": "https://example.test/issue/28",
                    "title": "[ST-A01-02-01] Preserve stage while expanding",
                    "state": "OPEN",
                    "body": "## Orchestrator Work Item\n\n- Item ID: `ST-A01-02-01`\n",
                }

            with (
                patch("orchestrator.tickets.shutil.which", return_value="/usr/bin/gh"),
                patch("orchestrator.tickets._gh_json", side_effect=fake_gh_json),
                patch("orchestrator.tickets._gh_text", return_value=""),
                patch("orchestrator.tickets._set_issue_stage_label", return_value=None),
            ):
                expanded, unchanged, skipped, fallback, item_count, target_count, actions = expand_repo_issues(
                    core_dir=core_dir,
                    state=state,
                    owner="TV7-Clip-Studio",
                    apply_changes=True,
                    strict_expand=False,
                    batch_id="E2E-ASTRO-001",
                    progress=False,
                    repo_filters=["tv7-astro-site"],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                    limit=1,
                    resume_from="",
                    dry_run_diff=False,
                    expand_agent="none",
                )

            self.assertEqual(item_count, 1)
            self.assertEqual(target_count, 1)
            self.assertEqual(expanded, 1)
            self.assertEqual(unchanged, 0)
            self.assertEqual(skipped, 0)
            self.assertEqual(fallback, 0)
            self.assertEqual(actions[0]["action"], "expanded")
            self.assertIn("stage_preserved=executing", actions[0]["note"])
            self.assertEqual(state["work_items"][0]["stage"], "executing")


if __name__ == "__main__":
    unittest.main()
