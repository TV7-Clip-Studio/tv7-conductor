from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.execution_bridge import execute_repo_handoffs
from orchestrator.idempotency import make_idempotency_key


class ExecuteHandoffTests(unittest.TestCase):
    def test_force_includes_items_already_in_executing_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-execute-") as td:
            core_dir = Path(td) / "tv7-core"
            core_dir.mkdir(parents=True, exist_ok=True)

            state = {
                "work_items": [
                    {
                        "item_id": "ST-A01-02-01",
                        "title": "Retry stuck execution",
                        "tasks": ["Retry execute handoff"],
                        "repos": ["tv7-astro-site"],
                        "stage": "executing",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "tv7-astro-site-roadmap.md"),
                        "source_line": 15,
                        "issues": {"tv7-astro-site": {"url": "https://github.com/TV7-Clip-Studio/tv7-astro-site/issues/28"}},
                    }
                ]
            }

            spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
                core_dir=core_dir,
                state=state,
                apply_changes=False,
                ao_command="ao",
                policy_path="",
                batch_id="E2E-ASTRO-001",
                progress=False,
                repo_filters=["tv7-astro-site"],
                roadmap_filters=[],
                item_prefix_filters=[],
                limit=1,
                resume_from="",
                force=True,
            )

            self.assertEqual((spawned, failed, skipped, planned), (0, 0, 0, 1))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "would_spawn")
            self.assertIn("cmd=ao spawn tv7-astro-site 28", actions[0]["note"])

    def test_dry_run_uses_github_issue_number_from_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-execute-") as td:
            core_dir = Path(td) / "tv7-core"
            core_dir.mkdir(parents=True, exist_ok=True)

            state = {
                "work_items": [
                    {
                        "item_id": "ST-A01-02-01",
                        "title": "Bridge execution handoff",
                        "tasks": ["Spawn execution for repo targets"],
                        "repos": ["tv7-astro-site"],
                        "stage": "expanded",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "tv7-astro-site-roadmap.md"),
                        "source_line": 15,
                        "issues": {
                            "tv7-astro-site": {
                                "url": "https://github.com/TV7-Clip-Studio/tv7-astro-site/issues/28"
                            }
                        },
                    }
                ]
            }

            spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
                core_dir=core_dir,
                state=state,
                apply_changes=False,
                ao_command="ao",
                policy_path="",
                batch_id="E2E-ASTRO-001",
                progress=False,
                repo_filters=["tv7-astro-site"],
                roadmap_filters=[],
                item_prefix_filters=[],
                limit=1,
                resume_from="",
                force=False,
            )

            self.assertEqual((spawned, failed, skipped, planned), (0, 0, 0, 1))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "would_spawn")
            self.assertIn("cmd=ao spawn tv7-astro-site 28", actions[0]["note"])

    def test_dry_run_plans_ao_spawn_with_sandbox_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-execute-") as td:
            core_dir = Path(td) / "tv7-core"
            core_dir.mkdir(parents=True, exist_ok=True)

            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-06-01",
                        "title": "Bridge execution handoff",
                        "tasks": ["Spawn execution for repo targets"],
                        "repos": ["tv7-core"],
                        "stage": "expanded",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 143,
                        "issues": {"tv7-core": {"url": "https://example.test/issue/1", "number": 1}},
                    }
                ]
            }

            spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
                core_dir=core_dir,
                state=state,
                apply_changes=False,
                ao_command="ao",
                policy_path="",
                batch_id="20260228-001",
                progress=False,
                repo_filters=[],
                roadmap_filters=[],
                item_prefix_filters=[],
                limit=0,
                resume_from="",
                force=False,
            )

            self.assertEqual((spawned, failed, skipped, planned), (0, 0, 0, 1))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "would_spawn")
            self.assertIn("cmd=ao spawn tv7-core 1", actions[0]["note"])
            self.assertIn("sandbox=", actions[0]["note"])

    def test_apply_records_artifacts_and_updates_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-execute-") as td:
            root = Path(td)
            core_dir = root / "tv7-core"
            repo_dir = root / "tv7-core"
            core_dir.mkdir(parents=True, exist_ok=True)
            (core_dir / "docs").mkdir(exist_ok=True)
            (core_dir / ".git").mkdir(exist_ok=True)

            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-06-02",
                        "title": "Execute bridge apply run",
                        "tasks": ["Spawn and capture artifacts"],
                        "repos": ["tv7-core"],
                        "stage": "expanded",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 143,
                        "issues": {"tv7-core": {"url": "https://example.test/issue/2", "number": 2}},
                        "linked_prs": {},
                        "attempts": {},
                        "errors": [],
                    }
                ],
                "agent_runs": [],
            }

            def fake_run(cmd, **kwargs):
                if cmd and cmd[0] == "ao":
                    if cmd[1:] == ["status", "--json"]:
                        return subprocess.CompletedProcess(
                            args=cmd,
                            returncode=0,
                            stdout='[{"name":"tas-2","project":"tv7-core","status":"working","activity":"active"}]\n',
                            stderr="",
                        )
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout="spawned https://github.com/TV7-Clip-Studio/tv7-core/pull/77\nSESSION=tas-2\n",
                        stderr="",
                    )
                if cmd[:3] == ["git", "-C", str(repo_dir)] and "--abbrev-ref" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="codex/bridge\n", stderr="")
                if cmd[:3] == ["git", "-C", str(repo_dir)] and cmd[-1] == "HEAD":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="abc123def456\n", stderr="")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            with (
                patch("orchestrator.execution_bridge.shutil.which", return_value="/usr/bin/ao"),
                patch("orchestrator.execution_bridge.subprocess.run", side_effect=fake_run),
            ):
                spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
                    core_dir=core_dir,
                    state=state,
                    apply_changes=True,
                    ao_command="ao",
                    policy_path="",
                    batch_id="20260228-001",
                    progress=False,
                    repo_filters=[],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                    limit=0,
                    resume_from="",
                    force=False,
                )

            self.assertEqual((spawned, failed, skipped, planned), (1, 0, 0, 0))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "spawned")
            self.assertEqual(state["work_items"][0]["stage"], "executing")
            self.assertEqual(
                state["work_items"][0]["linked_prs"]["tv7-core"],
                "https://github.com/TV7-Clip-Studio/tv7-core/pull/77",
            )

            run_entry = state["agent_runs"][0]
            self.assertEqual(run_entry["run_type"], "execute_handoff")
            self.assertEqual(run_entry["exit_status"], 0)
            self.assertEqual(run_entry["branch"], "codex/bridge")
            self.assertEqual(run_entry["commit"], "abc123def456")
            self.assertTrue(str(run_entry["log_file"]).endswith(".log"))
            self.assertTrue(Path(run_entry["log_file"]).is_file())
            self.assertTrue(Path(run_entry["payload_file"]).is_file())
            self.assertEqual(run_entry["command"], "ao spawn tv7-core 2")
            self.assertEqual(run_entry["session_id"], "tas-2")
            self.assertEqual(run_entry["spawn_health"], "working/active")

            key = make_idempotency_key(
                item_id="CO-C01-06-02",
                repo="tv7-core",
                batch_id="20260228-001",
                stage="executing",
            )
            self.assertIn(key, state["idempotency"]["keys"])

    def test_apply_marks_failed_when_session_exits_immediately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-execute-") as td:
            root = Path(td)
            core_dir = root / "tv7-core"
            repo_dir = root / "tv7-core"
            core_dir.mkdir(parents=True, exist_ok=True)
            (core_dir / "docs").mkdir(exist_ok=True)
            (core_dir / ".git").mkdir(exist_ok=True)

            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-06-03",
                        "title": "Spawn exits immediately",
                        "tasks": ["Detect early session exit"],
                        "repos": ["tv7-core"],
                        "stage": "expanded",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 170,
                        "issues": {"tv7-core": {"url": "https://example.test/issue/3", "number": 3}},
                        "linked_prs": {},
                        "attempts": {},
                        "errors": [],
                    }
                ],
                "agent_runs": [],
            }

            def fake_run(cmd, **kwargs):
                if cmd and cmd[0] == "ao":
                    if cmd[1:] == ["status", "--json"]:
                        return subprocess.CompletedProcess(
                            args=cmd,
                            returncode=0,
                            stdout='[{"name":"tas-3","project":"tv7-core","status":"killed","activity":"exited"}]\n',
                            stderr="",
                        )
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout="SESSION=tas-3\n",
                        stderr="",
                    )
                if cmd[:3] == ["git", "-C", str(repo_dir)] and "--abbrev-ref" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="feat/issue-3\n", stderr="")
                if cmd[:3] == ["git", "-C", str(repo_dir)] and cmd[-1] == "HEAD":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="deadbeef1234\n", stderr="")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            with (
                patch("orchestrator.execution_bridge.shutil.which", return_value="/usr/bin/ao"),
                patch("orchestrator.execution_bridge.subprocess.run", side_effect=fake_run),
            ):
                spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
                    core_dir=core_dir,
                    state=state,
                    apply_changes=True,
                    ao_command="ao",
                    policy_path="",
                    batch_id="20260228-001",
                    progress=False,
                    repo_filters=[],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                    limit=0,
                    resume_from="",
                    force=False,
                )

            self.assertEqual((spawned, failed, skipped, planned), (0, 1, 0, 0))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "spawn_failed")
            self.assertIn("session=tas-3", actions[0]["note"])
            self.assertEqual(state["work_items"][0]["stage"], "expanded")
            self.assertIn("health=killed/exited", state["work_items"][0]["last_error"])

    def test_apply_auto_unblocks_codex_workspace_prompt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-execute-") as td:
            root = Path(td)
            core_dir = root / "tv7-core"
            repo_dir = root / "tv7-core"
            core_dir.mkdir(parents=True, exist_ok=True)
            (core_dir / "docs").mkdir(exist_ok=True)
            (core_dir / ".git").mkdir(exist_ok=True)

            ao_state_dir = root / "ao-state"
            session_meta = ao_state_dir / "abc-tv7-core" / "sessions" / "tas-4"
            session_meta.parent.mkdir(parents=True, exist_ok=True)
            session_meta.write_text(
                "\n".join(
                    [
                        "worktree=/tmp/worktree",
                        "branch=feat/issue-4",
                        "status=spawning",
                        "tmuxName=abc-tas-4",
                        "issue=4",
                        "project=tv7-core",
                    ]
                )
                + "\n"
            )

            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-06-07",
                        "title": "Recover codex trust prompt sessions",
                        "tasks": ["Auto-unblock trust prompt once"],
                        "repos": ["tv7-core"],
                        "stage": "expanded",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 176,
                        "issues": {"tv7-core": {"url": "https://example.test/issue/4", "number": 4}},
                        "linked_prs": {},
                        "attempts": {},
                        "errors": [],
                    }
                ],
                "agent_runs": [],
            }

            status_calls = {"count": 0}

            def fake_run(cmd, **kwargs):
                if cmd and cmd[0] == "ao":
                    if cmd[1:3] == ["spawn", "tv7-core"]:
                        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="SESSION=tas-4\n", stderr="")
                    if cmd[1:] == ["status", "--json"]:
                        status_calls["count"] += 1
                        if status_calls["count"] == 1:
                            return subprocess.CompletedProcess(
                                args=cmd,
                                returncode=0,
                                stdout='[{"name":"tas-4","project":"tv7-core","status":"spawning","activity":null}]\n',
                                stderr="",
                            )
                        return subprocess.CompletedProcess(
                            args=cmd,
                            returncode=0,
                            stdout='[{"name":"tas-4","project":"tv7-core","status":"working","activity":"active"}]\n',
                            stderr="",
                        )
                    if cmd[1:3] == ["send", "--no-wait"]:
                        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="sent\n", stderr="")
                if cmd and cmd[0] == "tmux":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout=(
                            "You are running Codex in /tmp/worktree\n"
                            "1. Yes, allow Codex to work in this folder without asking for approval\n"
                            "Press enter to continue\n"
                        ),
                        stderr="",
                    )
                if cmd[:3] == ["git", "-C", str(repo_dir)] and "--abbrev-ref" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="feat/issue-4\n", stderr="")
                if cmd[:3] == ["git", "-C", str(repo_dir)] and cmd[-1] == "HEAD":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="facefeed1234\n", stderr="")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            with (
                patch("orchestrator.execution_bridge.shutil.which", return_value="/usr/bin/ao"),
                patch("orchestrator.execution_bridge.subprocess.run", side_effect=fake_run),
                patch.dict(
                    os.environ,
                    {
                        "TV7_AO_STATE_DIR": str(ao_state_dir),
                        "TV7_EXEC_SPAWN_HEALTH_POLLS": "1",
                        "TV7_EXEC_TRUST_APPROVE_SETTLE_SEC": "0",
                    },
                    clear=False,
                ),
            ):
                spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
                    core_dir=core_dir,
                    state=state,
                    apply_changes=True,
                    ao_command="ao",
                    policy_path="",
                    batch_id="20260304-001",
                    progress=False,
                    repo_filters=[],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                    limit=0,
                    resume_from="",
                    force=False,
                )

            self.assertEqual((spawned, failed, skipped, planned), (1, 0, 0, 0))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "spawned")
            self.assertIn("bootstrap=codex_workspace_trust_approved_tmux", actions[0]["note"])
            run_entry = state["agent_runs"][0]
            self.assertEqual(run_entry["spawn_bootstrap"], "codex_workspace_trust_approved_tmux")
            self.assertEqual(run_entry["spawn_health"], "working/active")

    def test_apply_uses_blind_bootstrap_when_prompt_text_not_detected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-execute-") as td:
            root = Path(td)
            core_dir = root / "tv7-core"
            repo_dir = root / "tv7-core"
            core_dir.mkdir(parents=True, exist_ok=True)
            (core_dir / "docs").mkdir(exist_ok=True)
            (core_dir / ".git").mkdir(exist_ok=True)

            ao_state_dir = root / "ao-state"
            session_meta = ao_state_dir / "abc-tv7-core" / "sessions" / "tas-5"
            session_meta.parent.mkdir(parents=True, exist_ok=True)
            session_meta.write_text(
                "\n".join(
                    [
                        "worktree=/tmp/worktree",
                        "branch=feat/issue-5",
                        "status=spawning",
                        "tmuxName=abc-tas-5",
                        "issue=5",
                        "project=tv7-core",
                    ]
                )
                + "\n"
            )

            state = {
                "work_items": [
                    {
                        "item_id": "CO-C01-06-07",
                        "title": "Recover codex trust prompt sessions",
                        "tasks": ["Fallback trust unblock"],
                        "repos": ["tv7-core"],
                        "stage": "expanded",
                        "source_file": str(core_dir / "docs" / "roadmaps" / "core" / "project-conductor-roadmap.md"),
                        "source_line": 176,
                        "issues": {"tv7-core": {"url": "https://example.test/issue/5", "number": 5}},
                        "linked_prs": {},
                        "attempts": {},
                        "errors": [],
                    }
                ],
                "agent_runs": [],
            }

            status_calls = {"count": 0}

            def fake_run(cmd, **kwargs):
                if cmd and cmd[0] == "ao":
                    if cmd[1:3] == ["spawn", "tv7-core"]:
                        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="SESSION=tas-5\n", stderr="")
                    if cmd[1:] == ["status", "--json"]:
                        status_calls["count"] += 1
                        if status_calls["count"] == 1:
                            return subprocess.CompletedProcess(
                                args=cmd,
                                returncode=0,
                                stdout='[{"name":"tas-5","project":"tv7-core","status":"spawning","activity":"exited"}]\n',
                                stderr="",
                            )
                        return subprocess.CompletedProcess(
                            args=cmd,
                            returncode=0,
                            stdout='[{"name":"tas-5","project":"tv7-core","status":"working","activity":"active"}]\n',
                            stderr="",
                        )
                    if cmd[1:3] == ["send", "--no-wait"]:
                        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="sent\n", stderr="")
                if cmd and cmd[0] == "tmux":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="some unrelated pane text\n", stderr="")
                if cmd[:3] == ["git", "-C", str(repo_dir)] and "--abbrev-ref" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="feat/issue-5\n", stderr="")
                if cmd[:3] == ["git", "-C", str(repo_dir)] and cmd[-1] == "HEAD":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="beadfeed1234\n", stderr="")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            with (
                patch("orchestrator.execution_bridge.shutil.which", return_value="/usr/bin/ao"),
                patch("orchestrator.execution_bridge.subprocess.run", side_effect=fake_run),
                patch.dict(
                    os.environ,
                    {
                        "TV7_AO_STATE_DIR": str(ao_state_dir),
                        "TV7_EXEC_SPAWN_HEALTH_POLLS": "1",
                        "TV7_EXEC_TRUST_APPROVE_SETTLE_SEC": "0",
                    },
                    clear=False,
                ),
            ):
                spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
                    core_dir=core_dir,
                    state=state,
                    apply_changes=True,
                    ao_command="ao",
                    policy_path="",
                    batch_id="20260304-001",
                    progress=False,
                    repo_filters=[],
                    roadmap_filters=[],
                    item_prefix_filters=[],
                    limit=0,
                    resume_from="",
                    force=False,
                )

            self.assertEqual((spawned, failed, skipped, planned), (1, 0, 0, 0))
            self.assertEqual((item_count, target_count), (1, 1))
            self.assertEqual(actions[0]["action"], "spawned")
            self.assertIn("bootstrap=blind_tmux_keys_sent", actions[0]["note"])
            run_entry = state["agent_runs"][0]
            self.assertEqual(run_entry["spawn_bootstrap"], "blind_tmux_keys_sent")
            self.assertEqual(run_entry["spawn_health"], "working/active")


if __name__ == "__main__":
    unittest.main()
