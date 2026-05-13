from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .item_id import _allowed_repo_codes, build_item_id, parse_item_id
from .models import WorkItem, now_iso
from .roadmap_id_config import load_roadmap_config

RE_TASK = re.compile(r"^\s*-\s\[( |x|X|p|P)\]\s+(.*)$")
RE_SUBTASK = re.compile(r"^\s{2,}(?:-\s+|\d+\.\s+)(.*)$")
RE_REPO = re.compile(r"\b(tv7-[a-z0-9-]+)\b")
RE_TASK_CODE = re.compile(
    r"^\[(?P<code>[A-Z0-9]{2,4}-[A-Z0-9]{2,6}-[A-Z0-9]{2,6}-[A-Z0-9]{2,6})\]\s+(?P<title>.+)$"
)
RE_ROADMAP_CODE = re.compile(r"^\s*Roadmap Code:\s*([A-Z]{2,4})\s*$", re.IGNORECASE)
RE_BATCH_CODE = re.compile(r"^\s*Batch Code:\s*([A-Z0-9]{2,6})\s*$", re.IGNORECASE)

def _infer_repos(title: str, tasks: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for text in [title, *tasks]:
        for repo in RE_REPO.findall(text):
            seen[repo] = None

    if seen:
        return sorted(seen.keys())

    full = f"{title}\n" + "\n".join(tasks)
    hint_map = {
        "backend": "tv7-fastapi-backend",
        "fastapi": "tv7-fastapi-backend",
        "flutter": "tv7-flutter-studio",
        "studio": "tv7-flutter-studio",
        "engine": "tv7-axum-engine",
        "axum": "tv7-axum-engine",
        "astro": "tv7-astro-site",
        "site": "tv7-astro-site",
        "core": "tv7-core",
    }
    out: list[str] = []
    lowered = full.lower()
    for key, repo in hint_map.items():
        if key in lowered and repo not in out:
            out.append(repo)
    return sorted(out)


def _roadmap_code(
    roadmap_file: Path,
    lines: list[str],
    *,
    repo_hint: str,
) -> str:
    cfg = load_roadmap_config()
    for line in lines[:30]:
        m = RE_ROADMAP_CODE.match(line)
        if m:
            return m.group(1).upper()
    if repo_hint:
        return cfg.repo_code(repo_hint)
    fallback_repo = cfg.repo_for_roadmap(roadmap_file.name)
    if fallback_repo:
        return cfg.repo_code(fallback_repo)
    return "WM"


def _batch_code(roadmap_file: Path, lines: list[str]) -> str:
    cfg = load_roadmap_config()
    for line in lines[:30]:
        m = RE_BATCH_CODE.match(line)
        if m:
            token = "".join(ch for ch in m.group(1).upper() if ch.isalnum())
            if token:
                return token[:3].ljust(3, "0")
    return cfg.batch_for_roadmap(roadmap_file.name)


def _epic_code_by_task_line(lines: list[str]) -> dict[int, str]:
    headings: list[int] = [i for i, line in enumerate(lines, start=1) if line.startswith("## ")]
    heading_to_epic: dict[int, str] = {}
    mapping: dict[int, str] = {}

    for i, line in enumerate(lines, start=1):
        if not RE_TASK.match(line):
            continue
        heading = 0
        for h in headings:
            if h <= i:
                heading = h
            else:
                break
        if heading not in heading_to_epic:
            epic_num = min(len(heading_to_epic) + 1, 99)
            heading_to_epic[heading] = f"{epic_num:02d}"
        mapping[i] = heading_to_epic[heading]
    return mapping


def parse_roadmap_markdown(roadmap_file: Path, default_repos: list[str] | None = None) -> list[WorkItem]:
    lines = roadmap_file.read_text().splitlines()
    items: list[WorkItem] = []
    repo_hint = default_repos[0] if default_repos and len(default_repos) == 1 else ""
    roadmap_code = _roadmap_code(roadmap_file, lines, repo_hint=repo_hint)
    batch_code = _batch_code(roadmap_file, lines)
    epic_by_line = _epic_code_by_task_line(lines)
    seq_by_epic: dict[str, int] = {}

    i = 0
    while i < len(lines):
        line = lines[i]
        m = RE_TASK.match(line)
        if not m:
            i += 1
            continue

        marker = m.group(1)
        checked = marker.lower() == "x"
        in_progress = marker.lower() == "p"
        raw_title = m.group(2).strip()
        line_no = i + 1
        epic_code = epic_by_line.get(line_no, "01")
        seq_by_epic[epic_code] = seq_by_epic.get(epic_code, 0) + 1
        ticket_code = f"{seq_by_epic[epic_code]:02d}"
        code_match = RE_TASK_CODE.match(raw_title)
        if code_match:
            explicit_id = code_match.group("code").upper()
            cfg = load_roadmap_config()
            allow_unknown_repo_code = roadmap_code not in _allowed_repo_codes(cfg)
            try:
                item_id = parse_item_id(
                    explicit_id,
                    expected_repo_code=roadmap_code,
                    config=cfg,
                    allow_unknown_repo_code=allow_unknown_repo_code,
                ).raw
            except ValueError as exc:
                raise ValueError(
                    f"{roadmap_file}:{line_no}: invalid task id `{explicit_id}`: {exc}"
                ) from exc
            title = code_match.group("title").strip()
        else:
            item_id = build_item_id(
                repo_code=roadmap_code,
                batch_code=batch_code,
                epic_code=epic_code,
                ticket_code=ticket_code,
            )
            title = raw_title
        tasks: list[str] = [title]

        j = i + 1
        while j < len(lines):
            next_line = lines[j]
            if RE_TASK.match(next_line):
                break
            sm = RE_SUBTASK.match(next_line)
            if sm:
                t = sm.group(1).strip()
                if t:
                    tasks.append(t)
            j += 1

        if not checked:
            repos = _infer_repos(title, tasks)
            if default_repos:
                repos = sorted({*repos, *[x for x in default_repos if x]})
            status = "IN_PROGRESS" if in_progress else "READY"
            items.append(
                WorkItem(
                    item_id=item_id,
                    title=title,
                    tasks=tasks,
                    repos=repos,
                    status=status,
                    source_file=str(roadmap_file),
                    source_line=line_no,
                    updated_at=now_iso(),
                )
            )

        i = j
    return items


def merge_work_items(state: dict[str, Any], new_items: list[WorkItem]) -> tuple[int, int]:
    existing = {x.get("item_id"): x for x in state.get("work_items", []) if isinstance(x, dict)}
    created = 0
    updated = 0
    for item in new_items:
        payload = item.to_dict()
        current = existing.get(item.item_id)
        if current is None:
            state["work_items"].append(payload)
            created += 1
            continue
        current["title"] = payload["title"]
        current["tasks"] = payload["tasks"]
        current["repos"] = payload["repos"]
        current["status"] = payload["status"]
        current["source_file"] = payload["source_file"]
        current["source_line"] = payload["source_line"]
        current["updated_at"] = now_iso()
        updated += 1
    return created, updated
