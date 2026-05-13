from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .item_id import build_item_id, parse_item_id
from .models import WorkItem, now_iso
from .planner import merge_work_items
from .roadmap_id_config import load_roadmap_config
from .tickets import expand_repo_issues

RE_BULLET = re.compile(r"^(?P<indent>\s*)(?:-|\d+\.)\s+(?P<body>.*\S)\s*$")
RE_CHECKBOX = re.compile(r"^\[(?: |x|X|p|P)\]\s+(?P<body>.*\S)\s*$")
RE_REPO = re.compile(r"\b(tv7-[a-z0-9-]+)\b")
RE_ITEM_ID = re.compile(r"\b([A-Z0-9]{2,4}-[A-Z0-9]{2,6}-[A-Z0-9]{2,6}-[A-Z0-9]{2,6})\b")
RE_HINT = re.compile(r"\b(?P<key>repos?|batch|epic)\s*[:=]\s*(?P<value>[A-Za-z0-9_,.-]+)", re.IGNORECASE)


def _split_csv(raw: str) -> list[str]:
    out: list[str] = []
    for token in str(raw).split(","):
        value = token.strip()
        if value:
            out.append(value)
    return out


def _normalize_batch_code(raw: str, *, fallback: str) -> str:
    token = "".join(ch for ch in str(raw).upper() if ch.isalnum())
    if not token:
        token = fallback
    return token[:3].ljust(3, "0")


def _normalize_epic_code(raw: str, *, fallback: str) -> str:
    token = "".join(ch for ch in str(raw).upper() if ch.isalnum())
    if not token:
        token = fallback
    if len(token) == 1:
        token = f"0{token}"
    return token[:2]


def _extract_hints(raw: str) -> dict[str, str]:
    hints: dict[str, str] = {}
    for match in RE_HINT.finditer(raw):
        key = str(match.group("key") or "").lower()
        value = str(match.group("value") or "").strip()
        if not value:
            continue
        if key in {"repo", "repos"}:
            hints["repos"] = value
        elif key == "batch":
            hints["batch"] = value
        elif key == "epic":
            hints["epic"] = value
    return hints


def _extract_explicit_item_id(raw: str) -> str:
    upper = str(raw).upper()
    match = RE_ITEM_ID.search(upper)
    if not match:
        return ""
    token = match.group(1).upper()
    try:
        return parse_item_id(token).raw
    except ValueError:
        return ""


def _clean_title(raw: str, *, explicit_item_id: str) -> str:
    text = str(raw).strip()
    if explicit_item_id:
        text = re.sub(rf"\[\s*{re.escape(explicit_item_id)}\s*\]", "", text, flags=re.IGNORECASE)
        text = text.replace(explicit_item_id, "")
    text = RE_HINT.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -:\t")
    return text


def _extract_repos(raw: str, *, default_repo: str, hinted_repos: str) -> list[str]:
    repos: dict[str, None] = {}
    for repo in _split_csv(hinted_repos):
        if repo.startswith("tv7-"):
            repos[repo] = None
    for repo in RE_REPO.findall(raw):
        repos[repo] = None
    if not repos and default_repo:
        repos[default_repo] = None
    return sorted(repos.keys())


@dataclass(slots=True)
class IdeaRecord:
    item_id: str
    title: str
    repos: list[str]
    batch_code: str
    epic_code: str
    ticket_code: str
    source_file: str
    source_line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "repos": list(self.repos),
            "batch_code": self.batch_code,
            "epic_code": self.epic_code,
            "ticket_code": self.ticket_code,
            "source_file": self.source_file,
            "source_line": self.source_line,
        }

    def to_work_item(self) -> WorkItem:
        return WorkItem(
            item_id=self.item_id,
            title=self.title,
            tasks=[self.title],
            repos=list(self.repos),
            status="READY",
            source_file=self.source_file,
            source_line=self.source_line,
            updated_at=now_iso(),
        )


@dataclass(slots=True)
class QueueIntent:
    intent_id: str
    item_id: str
    repo: str
    title: str
    batch_code: str
    epic_code: str
    source_file: str
    source_line: int
    status: str = "queued"
    handoff_action: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "item_id": self.item_id,
            "repo": self.repo,
            "title": self.title,
            "batch_code": self.batch_code,
            "epic_code": self.epic_code,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "status": self.status,
            "handoff_action": self.handoff_action,
            "updated_at": self.updated_at or now_iso(),
        }


def ingest_idea_records(
    *,
    idea_file: Path,
    default_repo: str,
    roadmap_code: str,
    batch_code: str,
    default_epic: str,
) -> list[IdeaRecord]:
    lines = idea_file.read_text().splitlines()
    cfg = load_roadmap_config()
    resolved_roadmap_code = "".join(ch for ch in str(roadmap_code).upper() if ch.isalnum())
    if not resolved_roadmap_code:
        resolved_roadmap_code = cfg.repo_code(default_repo)
    resolved_batch = _normalize_batch_code(batch_code, fallback=cfg.default_batch)
    resolved_default_epic = _normalize_epic_code(default_epic, fallback="01")

    seq_by_epic: dict[str, int] = {}
    seen_ids: set[str] = set()
    records: list[IdeaRecord] = []

    for line_no, raw_line in enumerate(lines, start=1):
        match = RE_BULLET.match(raw_line)
        if not match:
            continue
        if match.group("indent"):
            # Only top-level bullets are treated as idea records.
            continue
        body = str(match.group("body") or "").strip()
        checkbox = RE_CHECKBOX.match(body)
        if checkbox:
            body = str(checkbox.group("body") or "").strip()
        if not body or body.startswith("#"):
            continue

        hints = _extract_hints(body)
        explicit_item_id = _extract_explicit_item_id(body)
        title = _clean_title(body, explicit_item_id=explicit_item_id)
        hinted_repos = str(hints.get("repos", "")).strip()
        repos = _extract_repos(body, default_repo=default_repo, hinted_repos=hinted_repos)
        if not repos:
            continue

        if explicit_item_id:
            parsed = parse_item_id(explicit_item_id)
            item_id = parsed.raw
            item_batch = parsed.batch
            item_epic = parsed.epic
            item_ticket = parsed.ticket
        else:
            item_batch = _normalize_batch_code(str(hints.get("batch", "")), fallback=resolved_batch)
            item_epic = _normalize_epic_code(str(hints.get("epic", "")), fallback=resolved_default_epic)
            seq_by_epic[item_epic] = seq_by_epic.get(item_epic, 0) + 1
            item_ticket = f"{seq_by_epic[item_epic]:02d}"
            item_id = build_item_id(
                repo_code=resolved_roadmap_code,
                batch_code=item_batch,
                epic_code=item_epic,
                ticket_code=item_ticket,
            )

        if item_id in seen_ids:
            raise ValueError(f"{idea_file}:{line_no}: duplicate item id `{item_id}` in IDEA input")
        seen_ids.add(item_id)

        if not title:
            title = f"Idea item {line_no}"

        records.append(
            IdeaRecord(
                item_id=item_id,
                title=title,
                repos=repos,
                batch_code=item_batch,
                epic_code=item_epic,
                ticket_code=item_ticket,
                source_file=str(idea_file),
                source_line=line_no,
            )
        )

    return records


def emit_queue_intents(records: list[IdeaRecord]) -> list[QueueIntent]:
    queue: list[QueueIntent] = []
    for record in records:
        for repo in sorted(record.repos):
            queue.append(
                QueueIntent(
                    intent_id=f"{record.item_id}:{repo}",
                    item_id=record.item_id,
                    repo=repo,
                    title=record.title,
                    batch_code=record.batch_code,
                    epic_code=record.epic_code,
                    source_file=record.source_file,
                    source_line=record.source_line,
                    status="queued",
                    updated_at=now_iso(),
                )
            )
    queue.sort(key=lambda x: x.intent_id)
    return queue


def _planning_payload(state: dict[str, Any]) -> dict[str, Any]:
    payload = state.setdefault("planning", {})
    if not isinstance(payload, dict):
        payload = {}
        state["planning"] = payload
    payload.setdefault("idea_runs", [])
    payload.setdefault("intent_queue", [])
    payload["updated_at"] = now_iso()
    return payload


def upsert_intent_queue(
    *,
    state: dict[str, Any],
    queue_records: list[QueueIntent],
) -> tuple[int, int]:
    planning = _planning_payload(state)
    queue_raw = planning.get("intent_queue", [])
    if not isinstance(queue_raw, list):
        queue_raw = []
        planning["intent_queue"] = queue_raw

    by_id: dict[str, dict[str, Any]] = {}
    for entry in queue_raw:
        if not isinstance(entry, dict):
            continue
        intent_id = str(entry.get("intent_id", "")).strip()
        if intent_id:
            by_id[intent_id] = entry

    created = 0
    updated = 0
    for queue in queue_records:
        payload = queue.to_dict()
        current = by_id.get(queue.intent_id)
        if current is None:
            queue_raw.append(payload)
            by_id[queue.intent_id] = payload
            created += 1
            continue
        current.update(payload)
        updated += 1

    queue_raw.sort(key=lambda x: str(x.get("intent_id", "")))
    planning["updated_at"] = now_iso()
    return created, updated


def _update_queue_handoff_status(
    *,
    state: dict[str, Any],
    actions: list[dict[str, Any]],
) -> None:
    planning = _planning_payload(state)
    queue_raw = planning.get("intent_queue", [])
    if not isinstance(queue_raw, list):
        return
    by_id: dict[str, dict[str, Any]] = {}
    for entry in queue_raw:
        if not isinstance(entry, dict):
            continue
        intent_id = str(entry.get("intent_id", "")).strip()
        if intent_id:
            by_id[intent_id] = entry

    for action in actions:
        item_id = str(action.get("item_id", "")).strip()
        repo = str(action.get("repo", "")).strip()
        if not item_id or not repo:
            continue
        intent_id = f"{item_id}:{repo}"
        current = by_id.get(intent_id)
        if current is None:
            continue
        action_name = str(action.get("action", "")).strip()
        if action_name in {"expanded", "already_expanded"}:
            current["status"] = "handed_off"
        elif action_name == "would_expand":
            current["status"] = "handoff_planned"
        elif action_name.startswith("skip_"):
            current["status"] = "handoff_skipped"
        current["handoff_action"] = action_name
        current["updated_at"] = now_iso()


def _record_idea_run(state: dict[str, Any], run_summary: dict[str, Any]) -> None:
    planning = _planning_payload(state)
    runs = planning.get("idea_runs", [])
    if not isinstance(runs, list):
        runs = []
        planning["idea_runs"] = runs
    runs.append(copy.deepcopy(run_summary))
    planning["updated_at"] = now_iso()


def run_planning_pipeline(
    *,
    core_dir: Path,
    state: dict[str, Any],
    idea_file: Path,
    default_repo: str,
    roadmap_code: str,
    batch_code: str,
    default_epic: str,
    owner: str,
    apply_changes: bool,
    handoff_expand: bool,
    strict_expand: bool,
    batch_id: str,
    progress: bool,
    limit: int,
    resume_from: str,
    dry_run_diff: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = ingest_idea_records(
        idea_file=idea_file,
        default_repo=default_repo,
        roadmap_code=roadmap_code,
        batch_code=batch_code,
        default_epic=default_epic,
    )
    created, updated = merge_work_items(state, [record.to_work_item() for record in records])

    queue_records = emit_queue_intents(records)
    queue_created, queue_updated = upsert_intent_queue(state=state, queue_records=queue_records)

    queue_actions: list[dict[str, Any]] = [
        {
            "action": "queue_emit",
            "item_id": intent.item_id,
            "repo": intent.repo,
            "title": intent.title,
            "url": "",
            "note": f"intent={intent.intent_id}",
        }
        for intent in queue_records
    ]

    handoff_actions: list[dict[str, Any]] = []
    handoff_summary = {
        "handoff_enabled": bool(handoff_expand),
        "expanded": 0,
        "already_expanded": 0,
        "skipped": 0,
        "fallback": 0,
        "work_items": 0,
        "repo_targets": 0,
    }

    if handoff_expand and queue_records:
        repo_filters = sorted({record.repo for record in queue_records})
        item_prefix_filters = sorted({record.item_id for record in queue_records})
        (
            expanded,
            unchanged,
            skipped,
            fallback,
            item_count,
            target_count,
            handoff_actions,
        ) = expand_repo_issues(
            core_dir=core_dir,
            state=state,
            owner=owner,
            apply_changes=apply_changes,
            strict_expand=strict_expand,
            batch_id=batch_id,
            progress=progress,
            repo_filters=repo_filters,
            roadmap_filters=[],
            item_prefix_filters=item_prefix_filters,
            limit=limit,
            resume_from=resume_from,
            dry_run_diff=dry_run_diff,
        )
        handoff_summary = {
            "handoff_enabled": True,
            "expanded": expanded,
            "already_expanded": unchanged,
            "skipped": skipped,
            "fallback": fallback,
            "work_items": item_count,
            "repo_targets": target_count,
        }
        _update_queue_handoff_status(state=state, actions=handoff_actions)

    run_summary = {
        "run_id": f"graph-plan-{now_iso().replace(':', '').replace('-', '').replace('+00:00', 'z')}",
        "ts": now_iso(),
        "idea_file": str(idea_file),
        "mode": "apply" if apply_changes else "dry-run",
        "ingested": len(records),
        "work_items_created": created,
        "work_items_updated": updated,
        "queue_records": len(queue_records),
        "queue_created": queue_created,
        "queue_updated": queue_updated,
        "default_repo": default_repo,
        "roadmap_code": "".join(ch for ch in str(roadmap_code).upper() if ch.isalnum()),
        "batch_code": _normalize_batch_code(batch_code, fallback=load_roadmap_config().default_batch),
        "default_epic": _normalize_epic_code(default_epic, fallback="01"),
        "handoff": handoff_summary,
    }
    _record_idea_run(state, run_summary)

    actions = [*queue_actions, *handoff_actions]
    return run_summary, actions
