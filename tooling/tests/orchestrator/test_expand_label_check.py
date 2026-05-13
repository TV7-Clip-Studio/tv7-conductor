from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.tickets import check_stage_label_consistency


class ExpandLabelCheckTests(unittest.TestCase):
    def _state(self, issue_url: str) -> dict[str, object]:
        return {
            "work_items": [
                {
                    "item_id": "CO-C01-04-04",
                    "title": "Label consistency",
                    "repos": ["tv7-core"],
                    "stage": "expanded",
                    "issues": {
                        "tv7-core": {
                            "url": issue_url,
                        }
                    },
                }
            ]
        }

    def test_detects_drift_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-expand-label-") as td:
            state = self._state("https://example.test/issue/77")

            def fake_gh_json(args: list[str]):
                return {
                    "url": "https://example.test/issue/77",
                    "title": "[CO-C01-04-04] Label consistency",
                    "labels": [{"name": "stage:seed"}],
                }

            with (
                patch("orchestrator.tickets.shutil.which", return_value="/usr/bin/gh"),
                patch("orchestrator.tickets._gh_json", side_effect=fake_gh_json),
            ):
                checked, drift, fixed, actions = check_stage_label_consistency(
                    core_dir=Path(td),
                    state=state,
                    owner="TV7-Clip-Studio",
                    apply_changes=False,
                    progress=False,
                )

            self.assertEqual(checked, 1)
            self.assertEqual(drift, 1)
            self.assertEqual(fixed, 0)
            self.assertEqual(actions[0]["action"], "label_drift")

    def test_apply_fixes_stage_label(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-expand-label-") as td:
            state = self._state("https://example.test/issue/77")

            def fake_gh_json(args: list[str]):
                return {
                    "url": "https://example.test/issue/77",
                    "title": "[CO-C01-04-04] Label consistency",
                    "labels": [{"name": "stage:seed"}],
                }

            with (
                patch("orchestrator.tickets.shutil.which", return_value="/usr/bin/gh"),
                patch("orchestrator.tickets._gh_json", side_effect=fake_gh_json),
                patch("orchestrator.tickets.subprocess.run") as run_patch,
            ):
                checked, drift, fixed, actions = check_stage_label_consistency(
                    core_dir=Path(td),
                    state=state,
                    owner="TV7-Clip-Studio",
                    apply_changes=True,
                    progress=False,
                )

            self.assertEqual(checked, 1)
            self.assertEqual(drift, 1)
            self.assertEqual(fixed, 1)
            self.assertEqual(actions[0]["action"], "label_fixed")
            self.assertTrue(run_patch.called)


if __name__ == "__main__":
    unittest.main()
