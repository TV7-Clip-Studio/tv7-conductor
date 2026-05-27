from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .execution_policy import load_execution_policy
from .lifecycle import WorkStage, transition_item_stage
from .models import now_iso
from .review_policy import load_review_policy
from .state_store import append_event

RE_BOT_AUTHOR = re.compile(
    r"github-actions\[bot\]|chatgpt-codex-connector\[bot\]|chatgpt-codex-connector|deepseek",
    re.IGNORECASE,
)
RE_BOT_BODY = re.compile(r"DeepSeek review|FINAL_VERDICT|AI Summary", re.IGNORECASE)
RE_REJECTED = re.compile(r"FINAL_VERDICT:\s*❌\s*REJECTED|FINAL_VERDICT:\s*REJECTED", re.IGNORECASE)
RE_APPROVED = re.compile(r"FINAL_VERDICT:\s*✅\s*APPROVED|FINAL_VERDICT:\s*APPROVED", re.IGNORECASE)
RE_HARD_ACTION = re.compile(r"request changes|blocking|must fix|before merge", re.IGNORECASE)
RE_RECOMMENDATION = re.compile(
    r"recommendation|recommendations|consider adding|consider|should|could|missing|gap|testing gap|risk",
    re.IGNORECASE,
)
RE_MD_HEADER = re.compile(r"^#{1,6}\s+")
RE_SECTION_LABEL = re.compile(r"^(key changes|risks and considerations|recommendations)\s*:\s*$", re.IGNORECASE)
RE_LEADING_LIST_MARKER = re.compile(r"^\d+\.\s+|^[-*]\s+")
RE_PR_NUMBER = re.compile(r"/pull/(\d+)")


def _gh_json(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown gh error"
        raise RuntimeError(f"gh {' '.join(args)} failed: {stderr}")
    import json

    return json.loads(result.stdout)


def _split_values(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            token = part.strip()
            if token:
                out.append(token)
    return out


def _parse_pr_number(raw: str) -> int:
    value = str(raw or "").strip()
    if value.isdigit():
        return int(value)
    match = RE_PR_NUMBER.search(value)
    if not match:
        return 0
    return int(match.group(1))


def _verdict(body: str) -> str:
    if RE_REJECTED.search(body):
        return "REJECTED"
    if RE_APPROVED.search(body):
        return "APPROVED"
    return "UNKNOWN"


def _action_required(state: str, verdict: str, body: str, include_recommendations: bool) -> bool:
    if state == "CHANGES_REQUESTED":
        return True
    if verdict == "REJECTED":
        return True
    if RE_HARD_ACTION.search(body):
        return True
    if include_recommendations and RE_RECOMMENDATION.search(body):
        return True
    return False


def _summarize(body: str) -> str:
    if not body:
        return ""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return ""
    for raw in lines:
        line = raw
        lower = line.lower()
        if lower == "deepseek review":
            continue
        if RE_MD_HEADER.match(line) and "deepseek review" in lower:
            continue
        if RE_SECTION_LABEL.match(line):
            continue
        line = RE_LEADING_LIST_MARKER.sub("", line)
        line = line.replace("**", "").replace("`", "")
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line[:220]
    return re.sub(r"\s+", " ", lines[0]).strip()[:220]


def _fingerprint(parts: list[str]) -> str:
    raw = "|".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def collect_pr_feedback(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    include_recommendations: bool,
) -> dict[str, Any]:
    repo_slug = f"{owner}/{repo}"
    pr_view = _gh_json(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo_slug,
            "--json",
            "number,url,title,state,headRefName,baseRefName,reviewDecision",
        ]
    )
    reviews = _gh_json([f"api", f"repos/{owner}/{repo}/pulls/{pr_number}/reviews?per_page=100"])
    comments = _gh_json([f"api", f"repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=100"])
    review_comments = _gh_json([f"api", f"repos/{owner}/{repo}/pulls/{pr_number}/comments?per_page=100"])

    findings: list[dict[str, Any]] = []

    def _append(items: list[dict[str, Any]], source_type: str, has_state: bool, has_path: bool) -> None:
        for item in items:
            author = ((item.get("user") or {}).get("login") or "").strip()
            body = (item.get("body") or "").strip()
            if not (RE_BOT_AUTHOR.search(author) or RE_BOT_BODY.search(body)):
                continue
            state = (item.get("state") or "").strip().upper() if has_state else ""
            verdict = _verdict(body)
            path = str(item.get("path", "")).strip() if has_path else ""
            line = int(item.get("line", 0) or 0) if has_path else 0
            summary = _summarize(body)
            required = _action_required(state, verdict, body, include_recommendations)
            url = str(item.get("html_url", "")).strip()
            fingerprint = _fingerprint(
                [
                    source_type,
                    str(item.get("id", "")),
                    state,
                    verdict,
                    path,
                    str(line),
                    summary,
                ]
            )
            findings.append(
                {
                    "fingerprint": fingerprint,
                    "source_type": source_type,
                    "id": str(item.get("id", "")),
                    "author": author,
                    "state": state,
                    "verdict": verdict,
                    "action_required": required,
                    "summary": summary,
                    "path": path,
                    "line": line,
                    "url": url,
                }
            )

    _append(reviews if isinstance(reviews, list) else [], "review", has_state=True, has_path=False)
    _append(comments if isinstance(comments, list) else [], "comment", has_state=False, has_path=False)
    _append(review_comments if isinstance(review_comments, list) else [], "review_comment", has_state=False, has_path=True)

    findings.sort(key=lambda x: str(x.get("fingerprint", "")))
    actionable = [f for f in findings if bool(f.get("action_required"))]
    actionable.sort(key=lambda x: str(x.get("fingerprint", "")))

    overall_verdict = "UNKNOWN"
    if any(str(x.get("verdict", "")) == "REJECTED" for x in findings):
        overall_verdict = "REJECTED"
    elif findings and all(str(x.get("verdict", "")) == "APPROVED" for x in findings if str(x.get("verdict", "")) != "UNKNOWN"):
        if not actionable:
            overall_verdict = "APPROVED"

    return {
        "repo": repo,
        "pr_number": int(pr_view.get("number", pr_number)),
        "pr_url": str(pr_view.get("url", "")),
        "pr_title": str(pr_view.get("title", "")),
        "pr_state": str(pr_view.get("state", "")),
        "head_branch": str(pr_view.get("headRefName", "")),
        "base_branch": str(pr_view.get("baseRefName", "")),
        "review_decision": str(pr_view.get("reviewDecision", "")),
        "verdict": overall_verdict,
        "action_required": bool(actionable),
        "findings": findings,
        "actionable_findings": actionable,
        "generated_at": now_iso(),
    }


def review_repo_items(
    *,
    core_dir: Path,
    state: dict[str, Any],
    owner: str,
    apply_changes: bool,
    max_loops_override: int,
    review_policy_path: str,
    execution_policy_path: str,
    include_recommendations_override: bool | None,
    progress: bool,
    repo_filters: list[str] | None = None,
    roadmap_filters: list[str] | None = None,
    item_prefix_filters: list[str] | None = None,
    limit: int = 0,
    resume_from: str = "",
) -> tuple[int, int, int, int, int, int, int, list[dict[str, Any]]]:
    if shutil.which("gh") is None:
        raise RuntimeError("Missing required command: gh")

    review_policy = load_review_policy(review_policy_path or None)
    execution_policy = load_execution_policy(execution_policy_path or None)
    include_recommendations = (
        bool(include_recommendations_override)
        if include_recommendations_override is not None
        else bool(review_policy.include_recommendations)
    )

    repo_filter_set = set(_split_values(repo_filters))
    roadmap_filter_values = _split_values(roadmap_filters)
    item_prefixes = [x.upper() for x in _split_values(item_prefix_filters)]
    roadmap_filter_names = {Path(x).name for x in roadmap_filter_values}
    roadmap_filter_paths = {str(Path(x).resolve()) for x in roadmap_filter_values if x}

    def _progress(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    def _matches_item(item: dict[str, Any]) -> bool:
        stage = str(item.get("stage", WorkStage.SEEDED.value)).strip().lower()
        if stage not in {
            WorkStage.EXECUTING.value,
            WorkStage.PR_READY.value,
            WorkStage.EXTERNAL_REVIEW.value,
            WorkStage.REVIEW_FAILED.value,
        }:
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
        linked_prs = item.get("linked_prs", {})
        if not isinstance(linked_prs, dict):
            return []
        repos = [repo for repo in linked_prs.keys() if str(repo).strip()]
        if not repo_filter_set:
            return sorted(repos)
        return sorted([repo for repo in repos if repo in repo_filter_set])

    filtered_items: list[dict[str, Any]] = []
    for it in state.get("work_items", []):
        if isinstance(it, dict) and _matches_item(it):
            filtered_items.append(it)

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
    escalated = 0
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
        _progress(f"[external-review] target {processed}/{len(targets)} repo={repo} item={item_id}")

        linked_prs = item.get("linked_prs", {})
        pr_ref = str((linked_prs or {}).get(repo, "")).strip() if isinstance(linked_prs, dict) else ""
        pr_number = _parse_pr_number(pr_ref)
        if pr_number <= 0:
            skipped += 1
            actions.append(
                {
                    "action": "skip_missing_pr",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": "",
                    "note": "missing linked PR for review gate",
                }
            )
            continue

        try:
            feedback = collect_pr_feedback(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                include_recommendations=include_recommendations,
            )
        except RuntimeError as exc:
            feedback = {
                "repo": repo,
                "pr_number": pr_number,
                "pr_url": pr_ref,
                "verdict": "UNKNOWN",
                "action_required": True,
                "findings": [],
                "actionable_findings": [],
                "generated_at": now_iso(),
                "error": str(exc),
            }

        task_class = execution_policy.classify_work_type(item)
        max_loops = review_policy.max_loops_for(
            repo=repo,
            task_class=task_class,
            fallback=max_loops_override if max_loops_override > 0 else None,
        )
        if max_loops_override > 0:
            max_loops = int(max_loops_override)

        attempts = item.setdefault("attempts", {})
        if not isinstance(attempts, dict):
            attempts = {}
            item["attempts"] = attempts
        attempt_key = f"review:{repo}"
        current_attempt = int(attempts.get(attempt_key, 0) or 0)

        required = bool(feedback.get("action_required"))
        next_attempt = current_attempt + 1 if required else 0
        decision = "pass"
        if required:
            decision = "escalate" if next_attempt > max_loops else "retry"

        if not apply_changes:
            planned += 1
            action_name = {
                "pass": "would_review_passed",
                "retry": "would_review_failed",
                "escalate": "would_review_escalated",
            }[decision]
            actions.append(
                {
                    "action": action_name,
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": str(feedback.get("pr_url", pr_ref)),
                    "note": f"attempt={next_attempt if required else 0}/{max_loops}; class={task_class}",
                }
            )
        else:
            if required:
                attempts[attempt_key] = next_attempt
            else:
                attempts[attempt_key] = 0
            if decision == "pass":
                passed += 1
            elif decision == "retry":
                failed += 1
            else:
                escalated += 1

            action_name = {
                "pass": "review_passed",
                "retry": "review_failed",
                "escalate": "review_escalated",
            }[decision]
            actions.append(
                {
                    "action": action_name,
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": str(feedback.get("pr_url", pr_ref)),
                    "note": f"attempt={attempts.get(attempt_key,0)}/{max_loops}; class={task_class}",
                }
            )
            append_event(
                core_dir,
                "external_review.finding",
                f"External review evaluated {repo} for item {item_id}",
                {
                    "item_id": item_id,
                    "repo": repo,
                    "decision": decision,
                    "attempt": int(attempts.get(attempt_key, 0)),
                    "max_loops": max_loops,
                    "action_required": required,
                    "pr_url": str(feedback.get("pr_url", pr_ref)),
                },
            )

        item_entry = item_outcomes.setdefault(
            item_id,
            {"item": item, "feedback": [], "any_retry": False, "any_escalate": False, "reviewed": 0},
        )
        item_entry["feedback"].append(feedback)
        item_entry["reviewed"] = int(item_entry["reviewed"]) + 1
        if decision == "retry":
            item_entry["any_retry"] = True
        if decision == "escalate":
            item_entry["any_escalate"] = True

    if apply_changes:
        for item_id, payload in item_outcomes.items():
            item = payload["item"]
            feedback_entries = list(payload.get("feedback", []))
            feedback_entries.sort(
                key=lambda x: (
                    str(x.get("repo", "")),
                    int(x.get("pr_number", 0) or 0),
                )
            )
            retry_payload: list[dict[str, Any]] = []
            for entry in feedback_entries:
                for finding in entry.get("actionable_findings", []):
                    if not isinstance(finding, dict):
                        continue
                    retry_payload.append(
                        {
                            "repo": str(entry.get("repo", "")),
                            "pr_number": int(entry.get("pr_number", 0) or 0),
                            "pr_url": str(entry.get("pr_url", "")),
                            "fingerprint": str(finding.get("fingerprint", "")),
                            "source_type": str(finding.get("source_type", "")),
                            "summary": str(finding.get("summary", "")),
                            "path": str(finding.get("path", "")),
                            "line": int(finding.get("line", 0) or 0),
                            "url": str(finding.get("url", "")),
                        }
                    )
            retry_payload.sort(
                key=lambda x: (
                    str(x.get("repo", "")),
                    int(x.get("pr_number", 0)),
                    str(x.get("fingerprint", "")),
                )
            )

            item["review_feedback"] = {
                "generated_at": now_iso(),
                "targets": feedback_entries,
                "retry_payload": retry_payload,
                "actionable_count": len(retry_payload),
            }
            item["updated_at"] = now_iso()

            if bool(payload.get("any_escalate")):
                item["review_verdict"] = "ESCALATED"
                transition_item_stage(item, WorkStage.NEEDS_HUMAN.value, reason="review loop max exceeded")
            elif bool(payload.get("any_retry")):
                item["review_verdict"] = "REJECTED"
                if str(item.get("stage", "")).strip().lower() == WorkStage.EXECUTING.value:
                    transition_item_stage(
                        item,
                        WorkStage.EXTERNAL_REVIEW.value,
                        reason="legacy external review started",
                    )
                elif str(item.get("stage", "")).strip().lower() == WorkStage.PR_READY.value:
                    transition_item_stage(
                        item,
                        WorkStage.EXTERNAL_REVIEW.value,
                        reason="external review started",
                    )
                else:
                    transition_item_stage(
                        item,
                        WorkStage.EXTERNAL_REVIEW.value,
                        reason="external review actionable findings",
                    )
            elif int(payload.get("reviewed", 0)) > 0:
                item["review_verdict"] = "APPROVED"
                if str(item.get("stage", "")).strip().lower() == WorkStage.EXECUTING.value:
                    transition_item_stage(
                        item,
                        WorkStage.EXTERNAL_REVIEW.value,
                        reason="legacy external review started",
                    )
                elif str(item.get("stage", "")).strip().lower() == WorkStage.PR_READY.value:
                    transition_item_stage(
                        item,
                        WorkStage.EXTERNAL_REVIEW.value,
                        reason="external review started",
                    )
                transition_item_stage(item, WorkStage.INTEGRATED.value, reason="external review approved")

            append_event(
                core_dir,
                "external_review.item",
                f"External review updated item {item_id}",
                {
                    "item_id": item_id,
                    "stage": str(item.get("stage", "")),
                    "review_verdict": str(item.get("review_verdict", "")),
                    "actionable_count": int(item["review_feedback"].get("actionable_count", 0)),
                },
            )

    return passed, failed, escalated, skipped, planned, len(filtered_items), len(targets), actions
