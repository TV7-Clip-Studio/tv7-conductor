from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import tomllib
from typing import Any


def _normalize_bool(value: Any, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    token = str(value or "").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return fallback


def _normalize_int(value: Any, *, fallback: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, parsed)


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    default_max_loops: int
    include_recommendations: bool
    repo_max_loops: dict[str, int]
    class_max_loops: dict[str, int]

    def max_loops_for(self, *, repo: str, task_class: str, fallback: int | None = None) -> int:
        if repo in self.repo_max_loops:
            return self.repo_max_loops[repo]
        if task_class in self.class_max_loops:
            return self.class_max_loops[task_class]
        if fallback is not None:
            return max(0, int(fallback))
        return self.default_max_loops


@lru_cache(maxsize=4)
def load_review_policy(config_path: str | None = None) -> ReviewPolicy:
    path = Path(config_path) if config_path else Path(__file__).with_name("review_policy.toml")
    raw = tomllib.loads(path.read_text())

    defaults = raw.get("defaults", {}) if isinstance(raw.get("defaults", {}), dict) else {}
    default_max_loops = _normalize_int(defaults.get("max_loops", 2), fallback=2, minimum=0)
    include_recommendations = _normalize_bool(
        defaults.get("include_recommendations", True),
        fallback=True,
    )

    repo_loops_raw = raw.get("repo_max_loops", {}) if isinstance(raw.get("repo_max_loops", {}), dict) else {}
    repo_max_loops: dict[str, int] = {}
    for repo, value in repo_loops_raw.items():
        repo_name = str(repo).strip()
        if not repo_name:
            continue
        repo_max_loops[repo_name] = _normalize_int(value, fallback=default_max_loops, minimum=0)

    class_loops_raw = raw.get("class_max_loops", {}) if isinstance(raw.get("class_max_loops", {}), dict) else {}
    class_max_loops: dict[str, int] = {}
    for task_class, value in class_loops_raw.items():
        class_name = str(task_class).strip().lower()
        if not class_name:
            continue
        class_max_loops[class_name] = _normalize_int(value, fallback=default_max_loops, minimum=0)

    return ReviewPolicy(
        default_max_loops=default_max_loops,
        include_recommendations=include_recommendations,
        repo_max_loops=repo_max_loops,
        class_max_loops=class_max_loops,
    )
