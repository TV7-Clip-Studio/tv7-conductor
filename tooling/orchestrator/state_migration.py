from __future__ import annotations

from pathlib import Path
import hashlib
import json


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _digest(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_state_roots(legacy_root: Path, active_root: Path) -> dict[str, object]:
    legacy_state = legacy_root / "state.json"
    active_state = active_root / "state.json"
    legacy = _read_json(legacy_state)
    active = _read_json(active_state)
    legacy_items = legacy.get("work_items", [])
    active_items = active.get("work_items", [])
    legacy_runs = legacy.get("agent_runs", [])
    active_runs = active.get("agent_runs", [])
    legacy_ids = {
        str(item.get("item_id", "")).strip()
        for item in legacy_items
        if isinstance(item, dict) and str(item.get("item_id", "")).strip()
    } if isinstance(legacy_items, list) else set()
    active_ids = {
        str(item.get("item_id", "")).strip()
        for item in active_items
        if isinstance(item, dict) and str(item.get("item_id", "")).strip()
    } if isinstance(active_items, list) else set()
    return {
        "legacy_root": str(legacy_root),
        "active_root": str(active_root),
        "legacy_exists": legacy_state.exists(),
        "active_exists": active_state.exists(),
        "same_digest": bool(_digest(legacy_state) and _digest(legacy_state) == _digest(active_state)),
        "legacy_digest": _digest(legacy_state),
        "active_digest": _digest(active_state),
        "legacy_work_items": len(legacy_items) if isinstance(legacy_items, list) else 0,
        "active_work_items": len(active_items) if isinstance(active_items, list) else 0,
        "legacy_agent_runs": len(legacy_runs) if isinstance(legacy_runs, list) else 0,
        "active_agent_runs": len(active_runs) if isinstance(active_runs, list) else 0,
        "only_legacy_item_ids": sorted(legacy_ids - active_ids),
        "only_active_item_ids": sorted(active_ids - legacy_ids),
    }
