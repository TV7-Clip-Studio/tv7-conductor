from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .lifecycle import WorkStage, normalize_stage


def now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class RepoIssue:
    repo: str
    number: int | None = None
    url: str = ""
    state: str = ""
    pr_url: str = ""
    batch_id: str = ""
    assess_status: str = ""
    assess_log: str = ""
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "pr_url": self.pr_url,
            "batch_id": self.batch_id,
            "assess_status": self.assess_status,
            "assess_log": self.assess_log,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class WorkItem:
    item_id: str
    title: str
    tasks: list[str]
    repos: list[str]
    status: str = "READY"
    stage: str = WorkStage.SEEDED.value
    attempts: dict[str, int] = field(default_factory=dict)
    stage_history: list[dict[str, str]] = field(default_factory=list)
    linked_prs: dict[str, str] = field(default_factory=dict)
    review_verdict: str = ""
    review_feedback: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    source_file: str = ""
    source_line: int = 0
    issues: dict[str, RepoIssue] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    last_iteration_id: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "tasks": self.tasks,
            "repos": self.repos,
            "status": self.status,
            "stage": normalize_stage(self.stage),
            "attempts": self.attempts,
            "stage_history": self.stage_history,
            "linked_prs": self.linked_prs,
            "review_verdict": self.review_verdict,
            "review_feedback": self.review_feedback,
            "errors": self.errors,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "issues": {k: v.to_dict() for k, v in self.issues.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_iteration_id": self.last_iteration_id,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkItem":
        issues_raw = data.get("issues", {})
        issues: dict[str, RepoIssue] = {}
        if isinstance(issues_raw, dict):
            for repo, payload in issues_raw.items():
                if not isinstance(payload, dict):
                    continue
                issues[repo] = RepoIssue(
                    repo=repo,
                    number=payload.get("number"),
                    url=str(payload.get("url", "")),
                    state=str(payload.get("state", "")),
                    pr_url=str(payload.get("pr_url", "")),
                    batch_id=str(payload.get("batch_id", "")),
                    assess_status=str(payload.get("assess_status", "")),
                    assess_log=str(payload.get("assess_log", "")),
                    updated_at=str(payload.get("updated_at", now_iso())),
                )
        attempts: dict[str, int] = {}
        attempts_raw = data.get("attempts", {})
        if isinstance(attempts_raw, dict):
            for name, value in attempts_raw.items():
                key = str(name).strip().lower()
                if not key:
                    continue
                try:
                    attempts[key] = max(0, int(value))
                except (TypeError, ValueError):
                    attempts[key] = 0

        stage_history: list[dict[str, str]] = []
        stage_history_raw = data.get("stage_history", [])
        if isinstance(stage_history_raw, list):
            for item in stage_history_raw:
                if not isinstance(item, dict):
                    continue
                stage_history.append(
                    {
                        "from": normalize_stage(str(item.get("from", WorkStage.SEEDED.value))),
                        "to": normalize_stage(str(item.get("to", WorkStage.SEEDED.value))),
                        "reason": str(item.get("reason", "")).strip(),
                    }
                )

        linked_prs: dict[str, str] = {}
        linked_prs_raw = data.get("linked_prs", {})
        if isinstance(linked_prs_raw, dict):
            for repo, pr in linked_prs_raw.items():
                repo_name = str(repo).strip()
                pr_url = str(pr).strip()
                if repo_name and pr_url:
                    linked_prs[repo_name] = pr_url

        errors = [str(x).strip() for x in data.get("errors", []) if str(x).strip()]
        last_error = str(data.get("last_error", "")).strip()
        if not errors and last_error:
            errors = [last_error]

        return cls(
            item_id=str(data.get("item_id", "")),
            title=str(data.get("title", "")),
            tasks=[str(x) for x in data.get("tasks", []) if str(x).strip()],
            repos=[str(x) for x in data.get("repos", []) if str(x).strip()],
            status=str(data.get("status", "READY")),
            stage=normalize_stage(str(data.get("stage", WorkStage.SEEDED.value))),
            attempts=attempts,
            stage_history=stage_history,
            linked_prs=linked_prs,
            review_verdict=str(data.get("review_verdict", "")).strip(),
            review_feedback=dict(data.get("review_feedback", {}))
            if isinstance(data.get("review_feedback", {}), dict)
            else {},
            errors=errors,
            source_file=str(data.get("source_file", "")),
            source_line=int(data.get("source_line", 0) or 0),
            issues=issues,
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
            last_iteration_id=str(data.get("last_iteration_id", "")),
            last_error=str(data.get("last_error", "")),
        )


@dataclass(slots=True)
class IterationRecord:
    iteration_id: str
    status: str
    started_at: str
    ended_at: str = ""
    max_review_loops: int = 2
    agent_retries: int = 1
    auto_push: bool = True
    review_loops_completed: int = 0
    actionable_open: int = 0
    errors: list[str] = field(default_factory=list)
    summary_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration_id": self.iteration_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "max_review_loops": self.max_review_loops,
            "agent_retries": self.agent_retries,
            "auto_push": self.auto_push,
            "review_loops_completed": self.review_loops_completed,
            "actionable_open": self.actionable_open,
            "errors": self.errors,
            "summary_file": self.summary_file,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IterationRecord":
        return cls(
            iteration_id=str(data.get("iteration_id", "")),
            status=str(data.get("status", "")),
            started_at=str(data.get("started_at", "")),
            ended_at=str(data.get("ended_at", "")),
            max_review_loops=int(data.get("max_review_loops", 2)),
            agent_retries=int(data.get("agent_retries", 1)),
            auto_push=bool(data.get("auto_push", True)),
            review_loops_completed=int(data.get("review_loops_completed", 0)),
            actionable_open=int(data.get("actionable_open", 0)),
            errors=[str(x) for x in data.get("errors", [])],
            summary_file=str(data.get("summary_file", "")),
        )
