from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import json
import re
import subprocess
import time
import urllib.request

from .models import now_iso
from .state_store import paths


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip()).strip("-")
    return slug or "task"


@dataclass(frozen=True)
class WorktreePlan:
    repo: str
    repo_path: Path
    item_id: str
    branch: str
    worktree_path: Path
    base_ref: str
    preview_port: int
    preview_command: str
    preview_url: str
    apply: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "repo_path": str(self.repo_path),
            "item_id": self.item_id,
            "branch": self.branch,
            "worktree_path": str(self.worktree_path),
            "base_ref": self.base_ref,
            "preview_port": self.preview_port,
            "preview_command": self.preview_command,
            "preview_url": self.preview_url,
            "apply": self.apply,
        }


def git_changed_files(worktree_path: Path) -> list[str]:
    if not worktree_path.is_dir():
        return []
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--short"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    changed: list[str] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        path = text[3:].strip() if len(text) > 3 else text
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            changed.append(path)
    return changed


def git_status_short(worktree_path: Path) -> list[str]:
    if not worktree_path.is_dir():
        return []
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--short"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def build_worktree_plan(
    *,
    repo: str,
    repo_path: Path,
    item_id: str,
    worktree_root: Path,
    branch: str,
    base_ref: str,
    preview_port: int,
    preview_command: str,
    apply: bool,
) -> WorktreePlan:
    repo_slug = slugify(repo)
    item_slug = slugify(item_id)
    resolved_branch = branch.strip() or f"conductor/{repo_slug}/{item_slug}"
    worktree_path = worktree_root.expanduser().resolve() / repo_slug / item_slug
    port = int(preview_port or 0)
    return WorktreePlan(
        repo=repo,
        repo_path=repo_path.expanduser().resolve(),
        item_id=item_id,
        branch=resolved_branch,
        worktree_path=worktree_path,
        base_ref=base_ref.strip() or "HEAD",
        preview_port=port,
        preview_command=preview_command.strip(),
        preview_url=f"http://localhost:{port}" if port else "",
        apply=bool(apply),
    )


def write_worktree_report(core_dir: Path, plan: WorktreePlan, *, created: bool, error: str = "") -> Path:
    report_dir = paths(core_dir)["root"] / "reports" / "worktrees"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
    report_path = report_dir / f"worktree-{slugify(plan.repo)}-{slugify(plan.item_id)}-{stamp}.json"
    payload = {
        "created": created,
        "error": error,
        "validation_result": "not_run",
        "cleanup_command": f"git -C {plan.repo_path} worktree remove {plan.worktree_path}",
        "changed_files": git_changed_files(plan.worktree_path),
        "git_status_short": git_status_short(plan.worktree_path),
        "plan": plan.to_dict(),
    }
    report_path.write_text(json.dumps(payload, indent=2) + "\n")
    return report_path


def prepare_worktree(core_dir: Path, plan: WorktreePlan) -> tuple[bool, str, Path]:
    created = False
    error = ""
    if plan.apply:
        plan.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "git",
            "-C",
            str(plan.repo_path),
            "worktree",
            "add",
            "-b",
            plan.branch,
            str(plan.worktree_path),
            plan.base_ref,
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip() or f"git worktree add failed ({result.returncode})"
        else:
            created = True
    report_path = write_worktree_report(core_dir, plan, created=created, error=error)
    return created, error, report_path


def _preview_env(plan: WorktreePlan) -> dict[str, str]:
    env = dict(os.environ)
    if plan.preview_port:
        env["CONDUCTOR_PREVIEW_PORT"] = str(plan.preview_port)
        env.setdefault("PORT", str(plan.preview_port))
    return env


def _health_check(url: str, timeout_sec: float) -> bool:
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            return 200 <= int(response.status) < 500
    except Exception:
        return False


def launch_preview(
    core_dir: Path,
    plan: WorktreePlan,
    *,
    health_url: str = "",
    wait_sec: float = 10.0,
) -> tuple[int, bool, Path]:
    if not plan.preview_command:
        raise ValueError("preview command is required")
    if not plan.worktree_path.is_dir():
        raise FileNotFoundError(f"worktree not found: {plan.worktree_path}")

    report_dir = paths(core_dir)["root"] / "reports" / "previews"
    log_dir = report_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
    slug = f"{slugify(plan.repo)}-{slugify(plan.item_id)}-{stamp}"
    log_path = log_dir / f"{slug}.log"
    out = log_path.open("w")
    process = subprocess.Popen(
        plan.preview_command,
        cwd=str(plan.worktree_path),
        env=_preview_env(plan),
        shell=True,
        stdout=out,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out.close()

    resolved_health_url = health_url or plan.preview_url
    deadline = time.monotonic() + max(0.0, wait_sec)
    healthy = False
    while time.monotonic() <= deadline:
        if process.poll() is not None:
            break
        if _health_check(resolved_health_url, timeout_sec=1.0):
            healthy = True
            break
        time.sleep(0.25)

    report_path = report_dir / f"preview-{slug}.json"
    payload = {
        "pid": process.pid,
        "running": process.poll() is None,
        "healthy": healthy,
        "validation_result": "health_passed" if healthy else "health_not_confirmed",
        "health_url": resolved_health_url,
        "preview_url": plan.preview_url,
        "log_path": str(log_path),
        "stop_command": f"kill {process.pid}",
        "plan": plan.to_dict(),
    }
    report_path.write_text(json.dumps(payload, indent=2) + "\n")
    return process.pid, healthy, report_path


def run_validation(
    core_dir: Path,
    plan: WorktreePlan,
    *,
    commands: list[str],
    timeout_sec: int = 600,
) -> tuple[bool, Path]:
    if not plan.worktree_path.is_dir():
        raise FileNotFoundError(f"worktree not found: {plan.worktree_path}")
    if not commands:
        raise ValueError("at least one validation command is required")

    report_dir = paths(core_dir)["root"] / "reports" / "validations"
    log_dir = report_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
    slug = f"{slugify(plan.repo)}-{slugify(plan.item_id)}-{stamp}"

    results: list[dict[str, object]] = []
    passed = True
    for index, command in enumerate(commands, start=1):
        log_path = log_dir / f"{slug}-{index}.log"
        try:
            result = subprocess.run(
                command,
                cwd=str(plan.worktree_path),
                env=_preview_env(plan),
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=max(1, int(timeout_sec)),
                check=False,
            )
            output = result.stdout or ""
            returncode = int(result.returncode)
        except subprocess.TimeoutExpired as exc:
            output = str(exc.stdout or "") + f"\nTIMEOUT after {timeout_sec}s\n"
            returncode = 124
        log_path.write_text(output)
        ok = returncode == 0
        passed = passed and ok
        results.append(
            {
                "command": command,
                "returncode": returncode,
                "passed": ok,
                "log_path": str(log_path),
            }
        )
        if not ok:
            break

    report_path = report_dir / f"validation-{slug}.json"
    payload = {
        "passed": passed,
        "validation_result": "passed" if passed else "failed",
        "changed_files": git_changed_files(plan.worktree_path),
        "git_status_short": git_status_short(plan.worktree_path),
        "results": results,
        "plan": plan.to_dict(),
    }
    report_path.write_text(json.dumps(payload, indent=2) + "\n")
    return passed, report_path
