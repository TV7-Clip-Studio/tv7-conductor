from __future__ import annotations

import argparse
from io import StringIO
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import cli


class ResolveConflictsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patcher_batch_id = patch("orchestrator.cli._default_batch_id", return_value="dummy-batch")
        self.patcher_batch_id.start()

    def tearDown(self) -> None:
        self.patcher_batch_id.stop()

    @patch("subprocess.run")
    def test_resolve_conflicts_no_unmerged_files(self, mock_run: MagicMock) -> None:
        # Simulate directory check passing
        with patch("pathlib.Path.is_dir", return_value=True):
            # git diff output: empty (no conflicts)
            mock_diff = MagicMock()
            mock_diff.returncode = 0
            mock_diff.stdout = ""
            mock_run.return_value = mock_diff

            rc = cli.main(["resolve-conflicts", "--repo", "/dummy/repo"])

            self.assertEqual(rc, 0)
            # Verify git diff command was executed
            mock_run.assert_called_once_with(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd="/dummy/repo",
                capture_output=True,
                text=True,
                check=False,
            )

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_resolve_conflicts_success(self, mock_popen: MagicMock, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.read_text", return_value="resolved file without markers"), \
             patch("builtins.open", unittest.mock.mock_open()):
            mock_diff = MagicMock(returncode=0, stdout="src/main.py\nsrc/utils.py\n")
            mock_add1 = MagicMock(returncode=0)
            mock_add2 = MagicMock(returncode=0)
            mock_verify = MagicMock(returncode=0, stdout="")
            mock_run.side_effect = [mock_diff, mock_add1, mock_add2, mock_verify]

            mock_process = MagicMock()
            mock_process.returncode = 0
            mock_process.stdout = ["edit: src/main.py\n", "succeeded in 0ms\n"]
            mock_popen.return_value = mock_process

            rc = cli.main(["resolve-conflicts", "--repo", "/dummy/repo"])
            self.assertEqual(rc, 0)
            self.assertEqual(mock_run.call_count, 4)

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_resolve_conflicts_codex_failed(self, mock_popen: MagicMock, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open()):
            mock_diff = MagicMock(returncode=0, stdout="src/main.py\n")
            mock_run.side_effect = [mock_diff]

            mock_process = MagicMock()
            mock_process.returncode = 42
            mock_process.stdout = ["failed in 0ms\n"]
            mock_popen.return_value = mock_process

            rc = cli.main(["resolve-conflicts", "--repo", "/dummy/repo"])
            self.assertEqual(rc, 42)

    @patch("subprocess.run")
    def test_suggest_branch(self, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("sys.stdout", new=StringIO()) as mock_stdout:
            # 1. git status shows changes
            mock_status = MagicMock()
            mock_status.returncode = 0
            mock_status.stdout = " M src/main.py\n"

            # 2. gemini call succeeds
            mock_gemini = MagicMock()
            mock_gemini.returncode = 0
            mock_gemini.stdout = "Loaded cached credentials.\nfeature-auth-ui\nfix-rebase-bug\nrefactor-db\n"

            mock_run.side_effect = [mock_status, mock_gemini]

            rc = cli.main(["suggest-branch", "--repo", "/dummy/repo", "--context", "ticket details"])

            self.assertEqual(rc, 0)
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(mock_stdout.getvalue().strip(), "feature-auth-ui\nfix-rebase-bug\nrefactor-db")

            # Check gemini call args
            gemini_call = mock_run.call_args_list[1]
            self.assertEqual(gemini_call[0][0], ["gemini", "-m", "gemini-2.5-flash", "-p", "-"])
            self.assertIn("ticket details", gemini_call[1]["input"])
            self.assertIn("src/main.py", gemini_call[1]["input"])

    @patch("subprocess.run")
    def test_suggest_commit(self, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("sys.stdout", new=StringIO()) as mock_stdout:
            # 1. git diff shows changes
            mock_diff = MagicMock()
            mock_diff.returncode = 0
            mock_diff.stdout = "some diff output\n"

            # 2. gemini call succeeds
            mock_gemini = MagicMock()
            mock_gemini.returncode = 0
            mock_gemini.stdout = "Loaded cached credentials.\nadd main login layout\n"

            mock_run.side_effect = [mock_diff, mock_gemini]

            rc = cli.main(["suggest-commit", "--repo", "/dummy/repo"])

            self.assertEqual(rc, 0)
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(mock_stdout.getvalue().strip(), "add main login layout")

            # Check gemini call args
            gemini_call = mock_run.call_args_list[1]
            self.assertEqual(gemini_call[0][0], ["gemini", "-m", "gemini-2.5-flash", "-p", "-"])
            self.assertIn("some diff output", gemini_call[1]["input"])

    @patch("subprocess.run")
    def test_describe_changes(self, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("sys.stdout", new=StringIO()) as mock_stdout:
            # 1. git diff shows changes
            mock_diff = MagicMock()
            mock_diff.returncode = 0
            mock_diff.stdout = "some diff output\n"

            # 2. gemini call succeeds
            mock_gemini = MagicMock()
            mock_gemini.returncode = 0
            mock_gemini.stdout = "Loaded cached credentials.\n- Add main dashboard layout\n- Implement logout route\n"

            mock_run.side_effect = [mock_diff, mock_gemini]

            rc = cli.main(["describe-changes", "--repo", "/dummy/repo"])

            self.assertEqual(rc, 0)
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(mock_stdout.getvalue().strip(), "- Add main dashboard layout\n- Implement logout route")

            # Check gemini call args
            gemini_call = mock_run.call_args_list[1]
            self.assertEqual(gemini_call[0][0], ["gemini", "-m", "gemini-2.5-flash", "-p", "-"])
            self.assertIn("some diff output", gemini_call[1]["input"])
            self.assertIn("Generate a brief, clear bulleted summary", gemini_call[1]["input"])

    @patch("subprocess.run")
    def test_create_pr_success(self, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True):
            mock_branch = MagicMock(returncode=0, stdout="feat-test")
            mock_log = MagicMock(returncode=0, stdout="commit 1\ncommit 2")
            mock_gemini = MagicMock(returncode=0, stdout="Loaded cached credentials.\nTITLE: feat-test: Add new feature\nBODY:\n- Implement core functionality")
            mock_pr_create = MagicMock(returncode=0, stdout="https://github.com/TV7-Clip-Studio/tv7-core/pull/42")

            mock_run.side_effect = [mock_branch, mock_log, mock_gemini, mock_pr_create]

            rc = cli.main(["create-pr", "--repo", "/dummy/repo"])
            self.assertEqual(rc, 0)

            create_call = mock_run.call_args_list[3][0][0]
            self.assertEqual(create_call, ["gh", "pr", "create", "--title", "feat-test: Add new feature", "--body", "- Implement core functionality"])

    @patch("subprocess.run")
    @patch("builtins.input", return_value="3")
    def test_resolve_pr_review_exit_choice(self, mock_input: MagicMock, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True):
            mock_pr_view = MagicMock(returncode=0, stdout='{"number": 42, "url": "https://github.com/TV7-Clip-Studio/tv7-core/pull/42"}')
            mock_branch = MagicMock(returncode=0, stdout='feat-test')
            mock_poll = MagicMock(returncode=0, stdout='{"reviews": [], "comments": [{"body": "DeepSeek review FINAL_VERDICT: APPROVED (95%)", "createdAt": "2026-06-08T00:52:49Z"}]}')
            mock_gemini = MagicMock(
                returncode=0,
                stdout="""Loaded cached credentials.
### Summary of Findings
- Fix this

### Files to Modify
- src/main.py: fix syntax error

### Additional Technical Context
- Ensure proper imports
"""
            )
            mock_analysis = MagicMock(returncode=0)

            mock_run.side_effect = [
                mock_pr_view,
                mock_branch,
                mock_poll,
                mock_gemini,
                mock_analysis
            ]

            rc = cli.main(["resolve-pr-review", "--repo", "/dummy/repo"])
            self.assertEqual(rc, 0)

    @patch("subprocess.run")
    @patch("builtins.input", return_value="2")
    def test_resolve_pr_review_merge_choice(self, mock_input: MagicMock, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True):
            mock_pr_view = MagicMock(returncode=0, stdout='{"number": 42, "url": "https://github.com/TV7-Clip-Studio/tv7-core/pull/42"}')
            mock_branch = MagicMock(returncode=0, stdout='feat-test')
            mock_poll = MagicMock(returncode=0, stdout='{"reviews": [], "comments": [{"body": "DeepSeek review FINAL_VERDICT: APPROVED (95%)", "createdAt": "2026-06-08T00:52:49Z"}]}')
            mock_gemini = MagicMock(
                returncode=0,
                stdout="""Loaded cached credentials.
### Summary of Findings
- Fix this

### Files to Modify
- src/main.py: fix syntax error

### Additional Technical Context
- Ensure proper imports
"""
            )
            mock_analysis = MagicMock(returncode=0)
            mock_mergeable = MagicMock(returncode=0, stdout='{"mergeable": "MERGEABLE", "state": "OPEN"}')
            mock_merge = MagicMock(returncode=0)
            mock_checkout = MagicMock(returncode=0)
            mock_pull = MagicMock(returncode=0)
            mock_head_ref = MagicMock(returncode=0, stdout='{"headRefName": "feat-test"}')
            mock_del_branch = MagicMock(returncode=0)

            mock_run.side_effect = [
                mock_pr_view,
                mock_branch,
                mock_poll,
                mock_mergeable,
                mock_merge,
                mock_checkout,
                mock_pull,
                mock_head_ref,
                mock_del_branch
            ]

            rc = cli.main(["resolve-pr-review", "--repo", "/dummy/repo"])
            self.assertEqual(rc, 0)

    @patch("subprocess.run")
    @patch("builtins.input", return_value="2")
    def test_resolve_pr_review_blocks_unmergeable_pr(self, mock_input: MagicMock, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True):
            mock_pr_view = MagicMock(returncode=0, stdout='{"number": 42, "url": "https://github.com/TV7-Clip-Studio/tv7-core/pull/42"}')
            mock_branch = MagicMock(returncode=0, stdout='feat-test')
            mock_poll = MagicMock(returncode=0, stdout='{"reviews": [], "comments": [{"body": "DeepSeek review FINAL_VERDICT: APPROVED (95%)", "createdAt": "2026-06-08T00:52:49Z"}]}')
            mock_mergeable = MagicMock(returncode=0, stdout='{"mergeable": "CONFLICTING", "state": "OPEN"}')

            mock_run.side_effect = [
                mock_pr_view,
                mock_branch,
                mock_poll,
                mock_mergeable,
            ]

            rc = cli.main(["resolve-pr-review", "--repo", "/dummy/repo"])

            self.assertEqual(rc, 1)
            merge_calls = [call for call in mock_run.call_args_list if call[0][0] == ["gh", "pr", "merge", "--merge", "--delete-branch"]]
            self.assertEqual(merge_calls, [])

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("builtins.input")
    def test_resolve_pr_review_uses_codex_default_model_when_env_unset(self, mock_input: MagicMock, mock_popen: MagicMock, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open()), \
             patch.dict("os.environ", {}, clear=True):
            mock_pr_view = MagicMock(returncode=0, stdout='{"number": 42, "url": "https://github.com/TV7-Clip-Studio/tv7-core/pull/42"}')
            mock_branch = MagicMock(returncode=0, stdout='feat-test')
            mock_poll = MagicMock(returncode=0, stdout='{"reviews": [], "comments": [{"body": "DeepSeek review FINAL_VERDICT: APPROVED (95%)", "createdAt": "2026-06-08T00:52:49Z"}]}')
            mock_gemini = MagicMock(
                returncode=0,
                stdout="""Loaded cached credentials.
### Summary of Findings
- Fix this

### Files to Modify
- src/main.py: fix syntax error

### Additional Technical Context
- Ensure proper imports
"""
            )
            mock_analysis = MagicMock(returncode=0)

            mock_input.side_effect = ["1", "2", "2"]

            mock_process = MagicMock()
            mock_process.returncode = 0
            mock_process.stdout = ["succeeded in 0ms\n"]
            mock_popen.return_value = mock_process

            mock_status = MagicMock(returncode=0, stdout='')

            mock_run.side_effect = [
                mock_pr_view,
                mock_branch,
                mock_poll,
                mock_gemini,
                mock_analysis,
                mock_status
            ]

            rc = cli.main(["resolve-pr-review", "--repo", "/dummy/repo"])
            self.assertEqual(rc, 0)
            for popen_call in mock_popen.call_args_list:
                command = popen_call[0][0]
                self.assertNotIn("-m", command)
                self.assertNotIn("gpt-5.5", command)
                self.assertNotIn("gpt-5-codex", command)

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("builtins.input")
    def test_resolve_pr_review_uses_env_codex_model(self, mock_input: MagicMock, mock_popen: MagicMock, mock_run: MagicMock) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open()), \
             patch.dict("os.environ", {"TV7_CODEX_MODEL": "gpt-test-model"}, clear=True):
            mock_pr_view = MagicMock(returncode=0, stdout='{"number": 42, "url": "https://github.com/TV7-Clip-Studio/tv7-core/pull/42"}')
            mock_branch = MagicMock(returncode=0, stdout='feat-test')
            mock_poll = MagicMock(returncode=0, stdout='{"reviews": [], "comments": [{"body": "DeepSeek review FINAL_VERDICT: APPROVED (95%)", "createdAt": "2026-06-08T00:52:49Z"}]}')
            mock_gemini = MagicMock(
                returncode=0,
                stdout="""Loaded cached credentials.
### Summary of Findings
- Fix this

### Files to Modify
- src/main.py: fix syntax error

### Additional Technical Context
- Ensure proper imports
"""
            )

            mock_input.side_effect = ["1", "2", "2"]

            mock_process = MagicMock()
            mock_process.returncode = 0
            mock_process.stdout = ["succeeded in 0ms\n"]
            mock_popen.return_value = mock_process

            mock_status = MagicMock(returncode=0, stdout='')

            mock_run.side_effect = [
                mock_pr_view,
                mock_branch,
                mock_poll,
                mock_gemini,
                mock_status,
            ]

            rc = cli.main(["resolve-pr-review", "--repo", "/dummy/repo"])
            self.assertEqual(rc, 0)
            for popen_call in mock_popen.call_args_list:
                command = popen_call[0][0]
                self.assertIn("-m", command)
                self.assertIn("gpt-test-model", command)
                self.assertNotIn("gpt-5-codex", command)

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("builtins.input")
    def test_resolve_pr_review_stages_files_and_commit_fails(
        self,
        mock_input: MagicMock,
        mock_popen: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        with patch("pathlib.Path.is_dir", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open()):
            mock_pr_view = MagicMock(returncode=0, stdout='{"number": 42, "url": "https://github.com/TV7-Clip-Studio/tv7-core/pull/42"}')
            mock_branch = MagicMock(returncode=0, stdout='feat-test')
            mock_poll = MagicMock(returncode=0, stdout='{"reviews": [], "comments": [{"body": "DeepSeek review FINAL_VERDICT: REJECTED (55%)\\n### Findings\\n- Fix cli.py", "createdAt": "2026-06-08T00:52:49Z"}]}')
            mock_gemini = MagicMock(
                returncode=0,
                stdout="""Loaded cached credentials.
### Summary of Findings
- Fix this

### Files to Modify
- tooling/orchestrator/cli.py: fix review flow

### Additional Technical Context
- File intentionally contains conflict marker detector strings.
"""
            )

            mock_input.side_effect = ["1", "2"]

            plan_process = MagicMock()
            plan_process.returncode = 0
            plan_process.stdout = ["codex\n", "- Inspect cli.py\n"]
            resolve_process = MagicMock()
            resolve_process.returncode = 0
            resolve_process.stdout = ["codex\n", "Implemented review flow fix.\n"]
            mock_popen.side_effect = [plan_process, resolve_process]

            mock_status = MagicMock(returncode=0, stdout=" M tooling/orchestrator/cli.py\n")
            mock_unmerged = MagicMock(returncode=0, stdout="")
            mock_add = MagicMock(returncode=0)
            mock_verify = MagicMock(returncode=0, stdout="")
            mock_commit = MagicMock(returncode=1)

            mock_run.side_effect = [
                mock_pr_view,
                mock_branch,
                mock_poll,
                mock_gemini,
                mock_status,
                mock_unmerged,
                mock_add,
                mock_verify,
                mock_commit,
            ]

            rc = cli.main(["resolve-pr-review", "--repo", "/dummy/repo"])

            self.assertEqual(rc, 1)
            add_calls = [call for call in mock_run.call_args_list if call[0][0] == ["git", "add", "--", "tooling/orchestrator/cli.py"]]
            self.assertEqual(len(add_calls), 1)


if __name__ == "__main__":
    unittest.main()
