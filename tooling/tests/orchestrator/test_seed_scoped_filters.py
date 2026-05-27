from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.tickets import sync_repo_issues


class SeedScopedFilterTests(unittest.TestCase):
    def test_apply_mode_never_creates_off_scope_issues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-seed-scope-") as td:
            core_dir = Path(td)
            core_roadmap = core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"
            backend_roadmap = core_dir / "docs" / "roadmaps" / "tv7-fastapi-backend-roadmap.md"

            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-03-01",
                        "title": "Scoped target",
                        "tasks": ["Scoped target"],
                        "repos": ["tv7-core", "tv7-astro-site"],
                        "source_file": str(core_roadmap),
                        "source_line": 69,
                        "issues": {},
                    },
                    {
                        "item_id": "PY-A01-02-01",
                        "title": "Out of scope",
                        "tasks": ["Out of scope"],
                        "repos": ["tv7-fastapi-backend"],
                        "source_file": str(backend_roadmap),
                        "source_line": 10,
                        "issues": {},
                    },
                ]
            }

            created_repos: list[str] = []

            def fake_gh_json(args: list[str]):
                if args[:2] == ["issue", "list"]:
                    return []
                if args[:2] == ["issue", "view"]:
                    return {
                        "number": 101,
                        "url": "https://example.test/issue/101",
                        "state": "OPEN",
                        "title": "[CO-C01-03-01] Scoped target",
                    }
                return []

            def fake_gh_text(args: list[str]) -> str:
                if args[:2] == ["issue", "create"]:
                    repo = args[args.index("--repo") + 1]
                    created_repos.append(repo)
                    return "https://example.test/issue/101"
                return ""

            with (
                patch("orchestrator.tickets.shutil.which", return_value="/usr/bin/gh"),
                patch("orchestrator.tickets._gh_json", side_effect=fake_gh_json),
                patch("orchestrator.tickets._gh_text", side_effect=fake_gh_text),
                patch("orchestrator.tickets._apply_issue_labels", return_value=None),
            ):
                created, linked, skipped, planned, item_count, repo_target_count, actions = sync_repo_issues(
                    core_dir=core_dir,
                    state=state,
                    owner="TV7-Clip-Studio",
                    apply_changes=True,
                    assess_before_create=False,
                    assess_agent="none",
                    strict_assess=False,
                    agent_profile="codex_fast_low",
                    agent_model="gpt-5-codex",
                    agent_reasoning="low",
                    agent_personality="pragmatic",
                    assess_timeout_sec=60,
                    assess_log_max_chars=1000,
                    batch_id="20260228-001",
                    progress=False,
                    repo_filters=["tv7-core"],
                    roadmap_filters=[str(core_roadmap)],
                    item_prefix_filters=["CO-C01-03-01"],
                )

            self.assertEqual(created, 1)
            self.assertEqual(linked, 0)
            self.assertEqual(planned, 0)
            self.assertEqual(item_count, 1)
            self.assertEqual(repo_target_count, 1)
            self.assertEqual(created_repos, ["TV7-Clip-Studio/tv7-core"])

            action_repos = {str(a.get("repo", "")) for a in actions if a.get("action") == "created"}
            self.assertEqual(action_repos, {"tv7-core"})

    def test_dry_run_does_not_mutate_labels_for_linked_issues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-seed-scope-") as td:
            core_dir = Path(td)
            roadmap = core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"
            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-03-01",
                        "title": "Existing issue",
                        "tasks": ["Existing issue"],
                        "repos": ["tv7-core"],
                        "source_file": str(roadmap),
                        "source_line": 69,
                        "issues": {},
                    }
                ]
            }

            def fake_gh_json(args: list[str]):
                if args[:2] == ["issue", "list"]:
                    return [
                        {
                            "number": 501,
                            "url": "https://example.test/issue/501",
                            "title": "[CO-C01-03-01] Existing issue",
                            "state": "OPEN",
                        }
                    ]
                return []

            with (
                patch("orchestrator.tickets.shutil.which", return_value="/usr/bin/gh"),
                patch("orchestrator.tickets._gh_json", side_effect=fake_gh_json),
                patch("orchestrator.tickets._apply_issue_labels", return_value=None) as label_patch,
            ):
                created, linked, skipped, planned, item_count, repo_target_count, _actions = sync_repo_issues(
                    core_dir=core_dir,
                    state=state,
                    owner="TV7-Clip-Studio",
                    apply_changes=False,
                    assess_before_create=False,
                    assess_agent="none",
                    strict_assess=False,
                    agent_profile="codex_fast_low",
                    agent_model="gpt-5-codex",
                    agent_reasoning="low",
                    agent_personality="pragmatic",
                    assess_timeout_sec=60,
                    assess_log_max_chars=1000,
                    batch_id="20260228-001",
                    progress=False,
                    repo_filters=[],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                )

            self.assertEqual(created, 0)
            self.assertEqual(linked, 1)
            self.assertEqual(planned, 0)
            self.assertEqual(item_count, 1)
            self.assertEqual(repo_target_count, 1)
            label_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
