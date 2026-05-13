from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.worktree_preview import build_worktree_plan, launch_preview, prepare_worktree, run_validation


class WorktreePreviewTests(unittest.TestCase):
    def test_builds_isolated_worktree_plan_with_preview_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-worktree-plan-") as td:
            root = Path(td)
            plan = build_worktree_plan(
                repo="example-api",
                repo_path=root / "example-api",
                item_id="EX-A01-01-01",
                worktree_root=root / "worktrees",
                branch="",
                base_ref="main",
                preview_port=5001,
                preview_command="PORT=${CONDUCTOR_PREVIEW_PORT} make dev",
                apply=False,
            )

            self.assertEqual(plan.branch, "conductor/example-api/EX-A01-01-01")
            self.assertEqual(
                plan.worktree_path,
                (root / "worktrees" / "example-api" / "EX-A01-01-01").resolve(),
            )
            self.assertEqual(plan.preview_url, "http://localhost:5001")
            self.assertFalse(plan.apply)

    def test_dry_run_prepare_writes_report_without_creating_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-worktree-plan-") as td:
            root = Path(td)
            core_dir = root / "core"
            core_dir.mkdir()
            plan = build_worktree_plan(
                repo="example-api",
                repo_path=root / "example-api",
                item_id="EX-A01-01-01",
                worktree_root=root / "worktrees",
                branch="",
                base_ref="main",
                preview_port=5001,
                preview_command="make dev",
                apply=False,
            )

            created, error, report_path = prepare_worktree(core_dir, plan)

            self.assertFalse(created)
            self.assertEqual(error, "")
            self.assertFalse(plan.worktree_path.exists())
            payload = json.loads(report_path.read_text())
            self.assertFalse(payload["created"])
            self.assertEqual(payload["plan"]["preview_url"], "http://localhost:5001")
            self.assertIn("cleanup_command", payload)

    def test_prepare_report_includes_changed_files_for_existing_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-worktree-plan-") as td:
            root = Path(td)
            core_dir = root / "core"
            worktree = root / "worktree"
            core_dir.mkdir()
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            (worktree / "changed.txt").write_text("changed\n")
            plan = build_worktree_plan(
                repo="example-api",
                repo_path=worktree,
                item_id="EX-A01-01-01",
                worktree_root=root,
                branch="",
                base_ref="HEAD",
                preview_port=5001,
                preview_command="make dev",
                apply=False,
            )
            object.__setattr__(plan, "worktree_path", worktree)

            _, _, report_path = prepare_worktree(core_dir, plan)

            payload = json.loads(report_path.read_text())
            self.assertEqual(payload["changed_files"], ["changed.txt"])
            self.assertTrue(payload["git_status_short"])

    def test_launch_preview_writes_pid_and_log_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-preview-") as td:
            port = 8765
            root = Path(td)
            core_dir = root / "core"
            worktree = root / "worktree"
            core_dir.mkdir()
            worktree.mkdir()
            (worktree / "index.html").write_text("ok\n")
            plan = build_worktree_plan(
                repo="example-site",
                repo_path=worktree,
                item_id="EX-A01-01-01",
                worktree_root=root,
                branch="",
                base_ref="HEAD",
                preview_port=port,
                preview_command=f"{sys.executable} -m http.server $CONDUCTOR_PREVIEW_PORT --bind 127.0.0.1",
                apply=False,
            )
            object.__setattr__(plan, "worktree_path", worktree)

            fake_process = SimpleNamespace(pid=12345, poll=lambda: None)
            with (
                patch("orchestrator.worktree_preview.subprocess.Popen", return_value=fake_process),
                patch("orchestrator.worktree_preview._health_check", return_value=True),
            ):
                pid, healthy, report_path = launch_preview(
                    core_dir,
                    plan,
                    health_url=f"http://127.0.0.1:{port}/",
                    wait_sec=0.1,
                )

            payload = json.loads(report_path.read_text())
            self.assertEqual(pid, 12345)
            self.assertTrue(healthy)
            self.assertEqual(payload["pid"], pid)
            self.assertTrue(Path(payload["log_path"]).exists())

    def test_run_validation_records_passed_command_and_logs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-validation-") as td:
            root = Path(td)
            core_dir = root / "core"
            worktree = root / "worktree"
            core_dir.mkdir()
            worktree.mkdir()
            (worktree / "check.txt").write_text("ok\n")
            plan = build_worktree_plan(
                repo="example-api",
                repo_path=worktree,
                item_id="EX-A01-01-01",
                worktree_root=root,
                branch="",
                base_ref="HEAD",
                preview_port=0,
                preview_command="",
                apply=False,
            )
            object.__setattr__(plan, "worktree_path", worktree)

            passed, report_path = run_validation(
                core_dir,
                plan,
                commands=[f"{sys.executable} -c \"from pathlib import Path; assert Path('check.txt').exists()\""],
                timeout_sec=5,
            )

            payload = json.loads(report_path.read_text())
            self.assertTrue(passed)
            self.assertEqual(payload["validation_result"], "passed")
            self.assertEqual(payload["results"][0]["returncode"], 0)
            self.assertTrue(Path(payload["results"][0]["log_path"]).exists())

    def test_run_validation_stops_on_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-validation-") as td:
            root = Path(td)
            core_dir = root / "core"
            worktree = root / "worktree"
            core_dir.mkdir()
            worktree.mkdir()
            plan = build_worktree_plan(
                repo="example-api",
                repo_path=worktree,
                item_id="EX-A01-01-01",
                worktree_root=root,
                branch="",
                base_ref="HEAD",
                preview_port=0,
                preview_command="",
                apply=False,
            )
            object.__setattr__(plan, "worktree_path", worktree)

            passed, report_path = run_validation(
                core_dir,
                plan,
                commands=[f"{sys.executable} -c \"raise SystemExit(7)\"", f"{sys.executable} -c \"print('skip')\""],
                timeout_sec=5,
            )

            payload = json.loads(report_path.read_text())
            self.assertFalse(passed)
            self.assertEqual(payload["validation_result"], "failed")
            self.assertEqual(len(payload["results"]), 1)
            self.assertEqual(payload["results"][0]["returncode"], 7)


if __name__ == "__main__":
    unittest.main()
