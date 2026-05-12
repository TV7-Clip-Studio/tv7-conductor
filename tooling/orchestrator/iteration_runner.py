from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import IterationRecord, now_iso
from .state_store import append_event, ensure_layout


def _run_command(
    *,
    core_dir: Path,
    cmd: list[str],
    log_file: Path,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = None
    if env:
        run_env = dict(**env)
    result = subprocess.run(
        cmd,
        cwd=core_dir,
        text=True,
        input=stdin_text,
        capture_output=True,
        env=run_env,
        check=False,
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        f"$ {' '.join(cmd)}\n"
        f"exit={result.returncode}\n\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}\n"
    )
    return result


def _append_agent_run(
    state: dict[str, Any],
    *,
    iteration_id: str,
    repo: str,
    prompt_file: Path,
    status: str,
    attempt: int,
    error: str = "",
) -> None:
    state.setdefault("agent_runs", []).append(
        {
            "iteration_id": iteration_id,
            "repo": repo,
            "prompt_file": str(prompt_file),
            "status": status,
            "attempt": attempt,
            "error": error,
            "ts": now_iso(),
        }
    )


def _write_summary(
    *,
    summary_file: Path,
    iteration: IterationRecord,
    agent_runs: list[dict[str, Any]],
) -> None:
    lines = [
        f"# Orchestrator Iteration {iteration.iteration_id}",
        "",
        f"- Status: `{iteration.status}`",
        f"- Started: `{iteration.started_at}`",
        f"- Ended: `{iteration.ended_at}`",
        f"- Review loops completed: `{iteration.review_loops_completed}`",
        f"- Actionable remaining: `{iteration.actionable_open}`",
        "",
        "## Agent Runs",
        "",
        "| Repo | Attempt | Status | Prompt | Error |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for run in agent_runs:
        lines.append(
            f"| {run.get('repo','')} | {run.get('attempt',0)} | {run.get('status','')} | "
            f"`{run.get('prompt_file','')}` | {run.get('error','')} |"
        )
    if iteration.errors:
        lines.extend(["", "## Errors", ""])
        for err in iteration.errors:
            lines.append(f"- {err}")
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text("\n".join(lines) + "\n")


def run_once(
    *,
    core_dir: Path,
    state: dict[str, Any],
    max_review_loops: int,
    agent_retries: int,
    auto_push: bool,
) -> IterationRecord:
    if shutil.which("codex") is None:
        raise RuntimeError("Missing required command: codex")

    paths = ensure_layout(core_dir)
    iteration_id = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
    run_dir = paths["runs"] / iteration_id
    logs_dir = run_dir / "logs"
    summary_file = run_dir / "summary.md"

    iteration = IterationRecord(
        iteration_id=iteration_id,
        status="RUNNING",
        started_at=now_iso(),
        max_review_loops=max_review_loops,
        agent_retries=agent_retries,
        auto_push=auto_push,
        summary_file=str(summary_file),
    )
    append_event(core_dir, "iteration.start", f"Started iteration {iteration_id}", {"iteration_id": iteration_id})

    # 1) Create/update PRs from current repo changes.
    batch = _run_command(
        core_dir=core_dir,
        cmd=["make", "-C", str(core_dir), "batch-pr"],
        log_file=logs_dir / "batch-pr.log",
    )
    if batch.returncode != 0:
        iteration.status = "FAILED"
        iteration.ended_at = now_iso()
        iteration.errors.append("batch-pr failed; see logs/batch-pr.log")
        append_event(core_dir, "iteration.failed", "batch-pr failed", {"iteration_id": iteration_id})
        return iteration

    actionable_path = core_dir / "docs" / ".tmp" / "deepseek-followup-actionable.json"
    prompt_dir = core_dir / "docs" / ".tmp" / "deepseek-fix-prompts"
    all_agent_runs: list[dict[str, Any]] = []

    for loop in range(1, max_review_loops + 1):
        fix = _run_command(
            core_dir=core_dir,
            cmd=["make", "-C", str(core_dir), "deepseek-fix"],
            log_file=logs_dir / f"deepseek-fix-loop-{loop}.log",
        )
        if fix.returncode != 0:
            iteration.errors.append(f"deepseek-fix failed in loop {loop}; see logs/deepseek-fix-loop-{loop}.log")
            break

        if not actionable_path.exists():
            iteration.errors.append("Missing actionable file after deepseek-fix run.")
            break

        entries = json.loads(actionable_path.read_text())
        if not isinstance(entries, list):
            iteration.errors.append("Invalid actionable JSON payload.")
            break

        iteration.review_loops_completed = loop
        iteration.actionable_open = len(entries)
        append_event(
            core_dir,
            "iteration.review.loop",
            f"Loop {loop}: actionable={len(entries)}",
            {"iteration_id": iteration_id, "loop": loop, "actionable": len(entries)},
        )

        if not entries:
            break

        for entry in entries:
            repo = str(entry.get("repo", "")).strip()
            pr_number = int(entry.get("pr_number", 0) or 0)
            if not repo or pr_number <= 0:
                continue
            prompt_file = prompt_dir / f"{repo}-pr-{pr_number}.md"
            if not prompt_file.exists():
                err = f"Missing prompt file: {prompt_file}"
                iteration.errors.append(err)
                _append_agent_run(
                    state,
                    iteration_id=iteration_id,
                    repo=repo,
                    prompt_file=prompt_file,
                    status="FAILED",
                    attempt=1,
                    error=err,
                )
                continue

            repo_path = core_dir.parent / repo
            prompt_text = prompt_file.read_text()
            success = False
            last_error = ""

            for attempt in range(1, agent_retries + 2):
                cmd = ["codex", "exec", "--full-auto", "-C", str(repo_path), "-"]
                result = _run_command(
                    core_dir=core_dir,
                    cmd=cmd,
                    log_file=logs_dir / f"codex-{repo}-pr-{pr_number}-attempt-{attempt}.log",
                    stdin_text=prompt_text,
                )
                if result.returncode == 0:
                    success = True
                    _append_agent_run(
                        state,
                        iteration_id=iteration_id,
                        repo=repo,
                        prompt_file=prompt_file,
                        status="SUCCESS",
                        attempt=attempt,
                    )
                    append_event(
                        core_dir,
                        "agent.success",
                        f"Agent run succeeded for {repo} PR #{pr_number}",
                        {"iteration_id": iteration_id, "repo": repo, "attempt": attempt},
                    )
                    break
                last_error = f"codex exec failed (attempt {attempt})"
                _append_agent_run(
                    state,
                    iteration_id=iteration_id,
                    repo=repo,
                    prompt_file=prompt_file,
                    status="FAILED",
                    attempt=attempt,
                    error=last_error,
                )

            if not success:
                iteration.errors.append(f"{repo} PR #{pr_number}: {last_error}")

        all_agent_runs = [x for x in state.get("agent_runs", []) if x.get("iteration_id") == iteration_id]
        if iteration.errors:
            break

    # Refresh follow-up snapshot after loop.
    _run_command(
        core_dir=core_dir,
        cmd=["make", "-C", str(core_dir), "deepseek-followup"],
        log_file=logs_dir / "deepseek-followup-final.log",
    )

    if iteration.errors:
        iteration.status = "FAILED"
    elif iteration.actionable_open > 0:
        iteration.status = "PARTIAL"
    else:
        iteration.status = "COMPLETED"
    iteration.ended_at = now_iso()

    _write_summary(summary_file=summary_file, iteration=iteration, agent_runs=all_agent_runs)
    append_event(
        core_dir,
        "iteration.end",
        f"Iteration {iteration_id} ended with status {iteration.status}",
        {"iteration_id": iteration_id, "status": iteration.status, "summary_file": str(summary_file)},
    )
    return iteration

