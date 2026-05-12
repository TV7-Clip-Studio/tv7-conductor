from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from .lifecycle import WorkStage, transition_item_stage
from .models import now_iso
from .state_store import append_event, paths
from .worktree_preview import git_changed_files, git_status_short, slugify


def _split_values(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            token = part.strip()
            if token:
                out.append(token)
    return out


def _resolve_repo_path(repo: str, repo_path: str, workspace_dir: str) -> Path:
    raw = str(repo_path or "").strip() or repo
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path(workspace_dir).expanduser().resolve() / path).resolve()


def _run_commands(
    *,
    run_dir: Path,
    repo: str,
    item_id: str,
    cwd: Path,
    commands: list[str],
    timeout_sec: int,
) -> tuple[bool, list[dict[str, Any]]]:
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    passed = True
    for index, command in enumerate(commands, start=1):
        log_path = log_dir / f"{slugify(repo)}-{slugify(item_id)}-{index}.log"
        try:
            result = subprocess.run(
                command,
                cwd=str(cwd),
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
    return passed, results


def _write_report(
    *,
    core_dir: Path,
    mode: str,
    repo: str,
    item_id: str,
    title: str,
    review_path: Path,
    passed: bool,
    commands: list[str],
    results: list[dict[str, Any]],
    action: str,
) -> Path:
    report_dir = paths(core_dir)["root"] / "reports" / "local-review"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
    report_path = report_dir / f"local-review-{slugify(repo)}-{slugify(item_id)}-{stamp}.json"
    payload = {
        "mode": mode,
        "repo": repo,
        "item_id": item_id,
        "title": title,
        "review_path": str(review_path),
        "passed": passed,
        "validation_result": "passed" if passed else "failed",
        "commands": commands,
        "results": results,
        "changed_files": git_changed_files(review_path),
        "git_status_short": git_status_short(review_path),
        "action": action,
        "generated_at": now_iso(),
    }
    report_path.write_text(json.dumps(payload, indent=2) + "\n")
    return report_path


def run_local_review_items(
    *,
    core_dir: Path,
    state: dict[str, Any],
    apply_changes: bool,
    workspace_dir: str,
    repo_path_override: str,
    worktree_path_override: str,
    commands: list[str],
    timeout_sec: int,
    repo_filters: list[str] | None = None,
    roadmap_filters: list[str] | None = None,
    item_prefix_filters: list[str] | None = None,
    limit: int = 0,
    resume_from: str = "",
    progress: bool = False,
) -> tuple[int, int, int, int, int, int, list[dict[str, Any]]]:
    repo_filter_set = set(_split_values(repo_filters))
    roadmap_filter_values = _split_values(roadmap_filters)
    item_prefixes = [x.upper() for x in _split_values(item_prefix_filters)]
    roadmap_filter_names = {Path(x).name for x in roadmap_filter_values}
    roadmap_filter_paths = {str(Path(x).resolve()) for x in roadmap_filter_values if x}
    review_commands = [str(x).strip() for x in commands if str(x).strip()]

    def _progress(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    def _matches_item(item: dict[str, Any]) -> bool:
        stage = str(item.get("stage", WorkStage.SEEDED.value)).strip().lower()
        if stage not in {WorkStage.EXECUTING.value, WorkStage.LOCAL_REVIEW.value}:
            return False
        item_id = str(item.get("item_id", "")).upper()
        if item_prefixes and not any(item_id.startswith(prefix) for prefix in item_prefixes):
            return False
        if roadmap_filter_values:
            source_file = str(item.get("source_file", ""))
            if not source_file:
                return False
            source_name = Path(source_file).name
            source_abs = str(Path(source_file).resolve())
            if source_name not in roadmap_filter_names and source_abs not in roadmap_filter_paths:
                return False
        return True

    def _target_repos(item: dict[str, Any]) -> list[str]:
        repos = [str(repo).strip() for repo in item.get("repos", []) if str(repo).strip()]
        if not repo_filter_set:
            return sorted(repos)
        return sorted([repo for repo in repos if repo in repo_filter_set])

    filtered_items = [
        item for item in state.get("work_items", []) if isinstance(item, dict) and _matches_item(item)
    ]
    targets: list[tuple[dict[str, Any], str]] = []
    for item in filtered_items:
        for repo in _target_repos(item):
            targets.append((item, repo))
    targets.sort(key=lambda pair: (str(pair[0].get("item_id", "")), pair[1]))

    resume_token = str(resume_from).strip().upper()
    started = not bool(resume_token)
    max_targets = max(0, int(limit))
    processed = 0
    passed = 0
    failed = 0
    skipped = 0
    planned = 0
    actions: list[dict[str, Any]] = []
    item_outcomes: dict[str, dict[str, Any]] = {}

    for item, repo in targets:
        item_id = str(item.get("item_id", "")).strip()
        if not started:
            probe = item_id.upper()
            if probe == resume_token or probe.startswith(resume_token):
                started = True
            else:
                continue
        if max_targets > 0 and processed >= max_targets:
            break
        processed += 1
        _progress(f"[local-review] target {processed}/{len(targets)} repo={repo} item={item_id}")

        repo_path = _resolve_repo_path(repo, repo_path_override, workspace_dir)
        review_path = Path(worktree_path_override).expanduser().resolve() if worktree_path_override else repo_path
        if not review_path.is_dir():
            skipped += 1
            actions.append(
                {
                    "action": "skip_missing_review_path",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": "",
                    "note": f"review path not found: {review_path}",
                }
            )
            continue

        if not review_commands:
            skipped += 1
            actions.append(
                {
                    "action": "skip_no_local_review_commands",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": "",
                    "note": "provide --command to run local review",
                }
            )
            continue

        if not apply_changes:
            planned += 1
            report_path = _write_report(
                core_dir=core_dir,
                mode="dry-run",
                repo=repo,
                item_id=item_id,
                title=str(item.get("title", "")),
                review_path=review_path,
                passed=False,
                commands=review_commands,
                results=[],
                action="would_local_review",
            )
            actions.append(
                {
                    "action": "would_local_review",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": str(report_path),
                    "note": f"commands={len(review_commands)} review_path={review_path}",
                }
            )
            continue

        run_dir = paths(core_dir)["runs"] / f"local-review-{slugify(repo)}-{slugify(item_id)}-{processed}"
        ok, results = _run_commands(
            run_dir=run_dir,
            repo=repo,
            item_id=item_id,
            cwd=review_path,
            commands=review_commands,
            timeout_sec=int(timeout_sec),
        )
        action = "local_review_passed" if ok else "local_review_failed"
        report_path = _write_report(
            core_dir=core_dir,
            mode="apply",
            repo=repo,
            item_id=item_id,
            title=str(item.get("title", "")),
            review_path=review_path,
            passed=ok,
            commands=review_commands,
            results=results,
            action=action,
        )
        actions.append(
            {
                "action": action,
                "item_id": item_id,
                "repo": repo,
                "title": item.get("title", ""),
                "url": str(report_path),
                "note": f"commands={len(review_commands)} review_path={review_path}",
            }
        )
        if ok:
            passed += 1
        else:
            failed += 1

        entry = item_outcomes.setdefault(
            item_id,
            {"item": item, "reports": [], "any_failed": False, "reviewed": 0},
        )
        entry["reports"].append(
            {
                "repo": repo,
                "passed": ok,
                "report_path": str(report_path),
                "results": results,
            }
        )
        entry["reviewed"] = int(entry["reviewed"]) + 1
        if not ok:
            entry["any_failed"] = True
        append_event(
            core_dir,
            "local_review.target",
            f"Local review evaluated {repo} for item {item_id}",
            {
                "item_id": item_id,
                "repo": repo,
                "passed": ok,
                "report_path": str(report_path),
            },
        )

    if apply_changes:
        for item_id, payload in item_outcomes.items():
            item = payload["item"]
            reports = sorted(payload.get("reports", []), key=lambda x: str(x.get("repo", "")))
            review_feedback = item.setdefault("review_feedback", {})
            if not isinstance(review_feedback, dict):
                review_feedback = {}
                item["review_feedback"] = review_feedback
            review_feedback["local_review"] = {
                "generated_at": now_iso(),
                "targets": reports,
                "actionable_count": 1 if bool(payload.get("any_failed")) else 0,
            }
            item["updated_at"] = now_iso()
            if str(item.get("stage", "")).strip().lower() == WorkStage.EXECUTING.value:
                transition_item_stage(item, WorkStage.LOCAL_REVIEW.value, reason="local review started")
            if bool(payload.get("any_failed")):
                item["review_verdict"] = "LOCAL_REVIEW_FAILED"
                transition_item_stage(item, WorkStage.LOCAL_REVIEW.value, reason="local review failed")
            elif int(payload.get("reviewed", 0)) > 0:
                item["review_verdict"] = "LOCAL_REVIEW_PASSED"
                transition_item_stage(item, WorkStage.PR_READY.value, reason="local review passed")
            append_event(
                core_dir,
                "local_review.item",
                f"Local review updated item {item_id}",
                {
                    "item_id": item_id,
                    "stage": str(item.get("stage", "")),
                    "review_verdict": str(item.get("review_verdict", "")),
                    "target_count": len(reports),
                },
            )

    return passed, failed, skipped, planned, len(filtered_items), len(targets), actions
