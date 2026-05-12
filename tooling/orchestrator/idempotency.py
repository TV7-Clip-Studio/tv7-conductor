from __future__ import annotations

from typing import Any

from .lifecycle import normalize_stage
from .models import now_iso



def make_idempotency_key(*, item_id: str, repo: str, batch_id: str, stage: str) -> str:
    item = str(item_id or "").strip().upper()
    repo_name = str(repo or "").strip()
    batch = str(batch_id or "").strip() or "none"
    stage_token = normalize_stage(stage)
    return f"{item}:{repo_name}:{batch}:{stage_token}"



def ensure_idempotency_store(state: dict[str, Any]) -> dict[str, Any]:
    payload = state.setdefault("idempotency", {})
    if not isinstance(payload, dict):
        payload = {}
        state["idempotency"] = payload
    keys = payload.setdefault("keys", {})
    if not isinstance(keys, dict):
        keys = {}
        payload["keys"] = keys
    payload.setdefault("updated_at", now_iso())
    return payload



def has_idempotency_key(state: dict[str, Any], key: str) -> bool:
    payload = ensure_idempotency_store(state)
    keys = payload.get("keys", {})
    return str(key) in keys



def record_idempotency_key(
    state: dict[str, Any],
    *,
    key: str,
    item_id: str,
    repo: str,
    batch_id: str,
    stage: str,
    action: str,
    note: str = "",
) -> None:
    payload = ensure_idempotency_store(state)
    keys = payload.setdefault("keys", {})
    keys[str(key)] = {
        "item_id": str(item_id),
        "repo": str(repo),
        "batch_id": str(batch_id),
        "stage": normalize_stage(stage),
        "action": str(action),
        "note": str(note),
        "ts": now_iso(),
    }
    payload["updated_at"] = now_iso()
