from __future__ import annotations

from enum import Enum
from typing import Any


class WorkStage(str, Enum):
    SEEDED = "seeded"
    EXPANDED = "expanded"
    EXECUTING = "executing"
    LOCAL_REVIEW = "local_review"
    PR_READY = "pr_ready"
    EXTERNAL_REVIEW = "external_review"
    REVIEW_FAILED = "review_failed"
    REVIEW_PASSED = "review_passed"
    INTEGRATED = "integrated"
    DONE = "done"
    NEEDS_HUMAN = "needs_human"


LEGAL_TRANSITIONS: dict[str, set[str]] = {
    WorkStage.SEEDED.value: {
        WorkStage.SEEDED.value,
        WorkStage.EXPANDED.value,
        WorkStage.EXECUTING.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.EXPANDED.value: {
        WorkStage.EXPANDED.value,
        WorkStage.EXECUTING.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.EXECUTING.value: {
        WorkStage.EXECUTING.value,
        WorkStage.LOCAL_REVIEW.value,
        WorkStage.EXTERNAL_REVIEW.value,
        WorkStage.REVIEW_FAILED.value,
        WorkStage.REVIEW_PASSED.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.LOCAL_REVIEW.value: {
        WorkStage.LOCAL_REVIEW.value,
        WorkStage.EXECUTING.value,
        WorkStage.PR_READY.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.PR_READY.value: {
        WorkStage.PR_READY.value,
        WorkStage.EXTERNAL_REVIEW.value,
        WorkStage.INTEGRATED.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.EXTERNAL_REVIEW.value: {
        WorkStage.EXTERNAL_REVIEW.value,
        WorkStage.EXECUTING.value,
        WorkStage.INTEGRATED.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.REVIEW_FAILED.value: {
        WorkStage.REVIEW_FAILED.value,
        WorkStage.EXECUTING.value,
        WorkStage.LOCAL_REVIEW.value,
        WorkStage.EXTERNAL_REVIEW.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.REVIEW_PASSED.value: {
        WorkStage.REVIEW_PASSED.value,
        WorkStage.INTEGRATED.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.INTEGRATED.value: {
        WorkStage.INTEGRATED.value,
        WorkStage.DONE.value,
        WorkStage.NEEDS_HUMAN.value,
    },
    WorkStage.NEEDS_HUMAN.value: {
        WorkStage.NEEDS_HUMAN.value,
        WorkStage.EXECUTING.value,
        WorkStage.LOCAL_REVIEW.value,
        WorkStage.PR_READY.value,
        WorkStage.EXTERNAL_REVIEW.value,
        WorkStage.DONE.value,
    },
    WorkStage.DONE.value: {WorkStage.DONE.value},
}



def normalize_stage(stage: str, *, fallback: str = WorkStage.SEEDED.value) -> str:
    candidate = str(stage or "").strip().lower()
    if not candidate:
        return fallback
    if candidate in LEGAL_TRANSITIONS:
        return candidate
    return fallback



def allowed_transitions(stage: str) -> set[str]:
    normalized = normalize_stage(stage)
    return set(LEGAL_TRANSITIONS.get(normalized, {WorkStage.SEEDED.value}))



def assert_stage_transition(from_stage: str, to_stage: str) -> None:
    current = normalize_stage(from_stage)
    target = normalize_stage(to_stage)
    allowed = LEGAL_TRANSITIONS.get(current, {WorkStage.SEEDED.value})
    if target not in allowed:
        readable = ", ".join(sorted(allowed))
        raise ValueError(
            f"illegal stage transition `{current}` -> `{target}`; allowed: {readable}"
        )



def transition_item_stage(item: dict[str, Any], to_stage: str, *, reason: str = "") -> None:
    current = normalize_stage(str(item.get("stage", "")))
    target = normalize_stage(to_stage)
    assert_stage_transition(current, target)
    if current == target:
        item["stage"] = target
        return

    history = item.setdefault("stage_history", [])
    if not isinstance(history, list):
        history = []
        item["stage_history"] = history
    history.append(
        {
            "from": current,
            "to": target,
            "reason": str(reason or "").strip(),
        }
    )
    item["stage"] = target
