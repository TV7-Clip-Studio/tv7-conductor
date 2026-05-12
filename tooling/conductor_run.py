#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=str(cwd), check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Project Conductor flow (plan + tickets).")
    conductor_dir_default = Path(__file__).resolve().parents[1]
    workspace_default = conductor_dir_default.parent
    core_default = workspace_default / "tv7-core"
    parser.add_argument("--core-dir", default=str(core_default), help="Path to core/governance repo")
    parser.add_argument("--workspace-dir", default=str(workspace_default), help="Path to workspace root")
    parser.add_argument("--conductor-dir", default=str(conductor_dir_default), help="Path to conductor repo")
    parser.add_argument(
        "--state-dir",
        default=str(conductor_dir_default / "docs" / ".conductor"),
        help="Path to conductor state root",
    )
    parser.add_argument("--roadmap-file", default="", help="Optional roadmap file path for single-roadmap plan mode")
    parser.add_argument("--repo", action="append", default=[], help="Optional repo filter(s) for ticket sync")
    parser.add_argument("--item-prefix", action="append", default=[], help="Optional item id prefix filter(s)")
    parser.add_argument("--agent-profile", default="local_auto_medium", help="Agent profile for ticket sync")
    parser.add_argument("--batch-id", default="", help="Optional batch id override")
    parser.add_argument("--dry-run", action="store_true", help="Run tickets in dry-run mode")
    parser.add_argument(
        "--assess",
        action="store_true",
        default=False,
        help="Enable assessment during conductor-run ticket sync (disabled by default).",
    )
    args = parser.parse_args(argv)

    core_dir = Path(args.core_dir).resolve()
    workspace_dir = Path(args.workspace_dir).resolve()
    conductor_dir = Path(args.conductor_dir).resolve()
    state_dir = Path(args.state_dir).resolve()
    conductor_cli = [
        sys.executable,
        str(conductor_dir / "tooling" / "conductor.py"),
        "--core-dir",
        str(core_dir),
        "--workspace-dir",
        str(workspace_dir),
        "--conductor-dir",
        str(conductor_dir),
        "--state-dir",
        str(state_dir),
    ]

    env = dict(os.environ)
    env.setdefault("TV7_STATE_ROOT_NAME", ".conductor")
    env["TV7_CONDUCTOR_STATE_DIR"] = str(state_dir)
    env["TV7_CONDUCTOR_DIR"] = str(conductor_dir)
    env["TV7_WORKSPACE_DIR"] = str(workspace_dir)

    plan_cmd = [*conductor_cli, "plan"]
    if args.roadmap_file:
        plan_cmd.extend(["--single-roadmap", "--roadmap", args.roadmap_file])
    _run(plan_cmd, cwd=core_dir)

    tickets_cmd = [*conductor_cli, "tickets", "--agent-profile", args.agent_profile]
    if not args.dry_run:
        tickets_cmd.append("--apply")
    if args.batch_id:
        tickets_cmd.extend(["--batch-id", args.batch_id])
    for repo in args.repo:
        tickets_cmd.extend(["--repo", repo])
    for prefix in args.item_prefix:
        tickets_cmd.extend(["--item-prefix", prefix])
    if not args.assess:
        tickets_cmd.append("--no-assess")

    result = subprocess.run(tickets_cmd, cwd=str(core_dir), env=env, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
