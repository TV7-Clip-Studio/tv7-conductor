from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import IterationRecord, WorkItem, now_iso

STATE_SCHEMA_NAME = "project-conductor-state"
STATE_SCHEMA_VERSION = 3


def storage_dir_name() -> str:
    raw = str(os.environ.get("TV7_STATE_ROOT_NAME", ".orchestrator")).strip()
    if not raw:
        return ".orchestrator"
    if "/" in raw or "\\" in raw:
        return ".orchestrator"
    if not raw.startswith("."):
        raw = f".{raw}"
    return raw


def explicit_state_root() -> Path | None:
    raw = str(os.environ.get("TV7_CONDUCTOR_STATE_DIR", "")).strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _default_state() -> dict[str, Any]:
    return {
        "version": STATE_SCHEMA_VERSION,
        "schema": {"name": STATE_SCHEMA_NAME, "version": STATE_SCHEMA_VERSION},
        "updated_at": now_iso(),
        "settings": {"auto_push": True},
        "work_items": [],
        "iterations": [],
        "agent_runs": [],
        "idempotency": {"keys": {}, "updated_at": now_iso()},
        "planning": {"idea_runs": [], "intent_queue": [], "updated_at": now_iso()},
    }


def paths(core_dir: Path) -> dict[str, Path]:
    root = explicit_state_root() or (core_dir / "docs" / storage_dir_name())
    return {
        "root": root,
        "state": root / "state.json",
        "events": root / "events.jsonl",
        "lock": root / "lock",
        "runs": root / "runs",
    }


def ensure_layout(core_dir: Path) -> dict[str, Path]:
    p = paths(core_dir)
    p["root"].mkdir(parents=True, exist_ok=True)
    p["runs"].mkdir(parents=True, exist_ok=True)
    if not p["state"].exists():
        p["state"].write_text(json.dumps(_default_state(), indent=2) + "\n")
    if not p["events"].exists():
        p["events"].write_text("")
    return p


def _normalize_idempotency(raw: Any) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    keys_raw = payload.get("keys", {})
    keys_out: dict[str, dict[str, str]] = {}
    if isinstance(keys_raw, dict):
        for key, meta in keys_raw.items():
            key_s = str(key).strip()
            if not key_s:
                continue
            if isinstance(meta, dict):
                keys_out[key_s] = {
                    "item_id": str(meta.get("item_id", "")),
                    "repo": str(meta.get("repo", "")),
                    "batch_id": str(meta.get("batch_id", "")),
                    "stage": str(meta.get("stage", "")),
                    "action": str(meta.get("action", "")),
                    "note": str(meta.get("note", "")),
                    "ts": str(meta.get("ts", now_iso())),
                }
                continue
            keys_out[key_s] = {
                "item_id": "",
                "repo": "",
                "batch_id": "",
                "stage": "",
                "action": "legacy",
                "note": str(meta),
                "ts": now_iso(),
            }
    return {"keys": keys_out, "updated_at": str(payload.get("updated_at", now_iso()))}


def _normalize_planning(raw: Any) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}

    runs_raw = payload.get("idea_runs", [])
    runs_out: list[dict[str, Any]] = []
    if isinstance(runs_raw, list):
        for run in runs_raw:
            if isinstance(run, dict):
                runs_out.append(dict(run))

    queue_raw = payload.get("intent_queue", [])
    queue_out: list[dict[str, Any]] = []
    if isinstance(queue_raw, list):
        for entry in queue_raw:
            if not isinstance(entry, dict):
                continue
            intent_id = str(entry.get("intent_id", "")).strip()
            if not intent_id:
                continue
            queue_out.append(
                {
                    "intent_id": intent_id,
                    "item_id": str(entry.get("item_id", "")),
                    "repo": str(entry.get("repo", "")),
                    "title": str(entry.get("title", "")),
                    "batch_code": str(entry.get("batch_code", "")),
                    "epic_code": str(entry.get("epic_code", "")),
                    "source_file": str(entry.get("source_file", "")),
                    "source_line": int(entry.get("source_line", 0) or 0),
                    "status": str(entry.get("status", "queued")),
                    "handoff_action": str(entry.get("handoff_action", "")),
                    "updated_at": str(entry.get("updated_at", now_iso())),
                }
            )
    queue_out.sort(key=lambda x: str(x.get("intent_id", "")))
    return {
        "idea_runs": runs_out,
        "intent_queue": queue_out,
        "updated_at": str(payload.get("updated_at", now_iso())),
    }


def load_state(core_dir: Path) -> dict[str, Any]:
    p = ensure_layout(core_dir)
    raw = json.loads(p["state"].read_text())
    data = raw if isinstance(raw, dict) else {}
    work_items = [
        WorkItem.from_dict(x).to_dict()
        for x in data.get("work_items", [])
        if isinstance(x, dict)
    ]
    iterations = [
        IterationRecord.from_dict(x).to_dict()
        for x in data.get("iterations", [])
        if isinstance(x, dict)
    ]
    normalized = {
        "version": STATE_SCHEMA_VERSION,
        "schema": {"name": STATE_SCHEMA_NAME, "version": STATE_SCHEMA_VERSION},
        "updated_at": str(data.get("updated_at", now_iso())),
        "settings": dict(data.get("settings", {"auto_push": True})),
        "work_items": work_items,
        "iterations": iterations,
        "agent_runs": list(data.get("agent_runs", [])),
        "idempotency": _normalize_idempotency(data.get("idempotency", {})),
        "planning": _normalize_planning(data.get("planning", {})),
    }
    raw_version = int(data.get("version", 1) or 1)
    has_schema = isinstance(data.get("schema"), dict)
    has_idempotency = isinstance(data.get("idempotency"), dict)
    has_planning = isinstance(data.get("planning"), dict)
    if raw_version != STATE_SCHEMA_VERSION or not has_schema or not has_idempotency or not has_planning:
        p["state"].write_text(json.dumps(normalized, indent=2) + "\n")
    return normalized


def save_state(core_dir: Path, state: dict[str, Any]) -> None:
    p = ensure_layout(core_dir)
    state["version"] = STATE_SCHEMA_VERSION
    state["schema"] = {"name": STATE_SCHEMA_NAME, "version": STATE_SCHEMA_VERSION}
    state["idempotency"] = _normalize_idempotency(state.get("idempotency", {}))
    state["planning"] = _normalize_planning(state.get("planning", {}))
    state["updated_at"] = now_iso()
    p["state"].write_text(json.dumps(state, indent=2) + "\n")


def append_event(core_dir: Path, event_type: str, message: str, context: dict[str, Any] | None = None) -> None:
    p = ensure_layout(core_dir)
    payload = {
        "ts": now_iso(),
        "event_type": event_type,
        "message": message,
        "context": context or {},
    }
    with p["events"].open("a") as f:
        f.write(json.dumps(payload) + "\n")


def read_events(core_dir: Path, limit: int = 30) -> list[dict[str, Any]]:
    p = ensure_layout(core_dir)
    lines = p["events"].read_text().splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@contextmanager
def run_lock(core_dir: Path) -> Iterator[None]:
    p = ensure_layout(core_dir)
    lock = p["lock"]
    fd: int | None = None
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {now_iso()}".encode())
        yield
    finally:
        if fd is not None:
            os.close(fd)
        if lock.exists():
            lock.unlink()
