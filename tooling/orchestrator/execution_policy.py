from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import tomllib
from typing import Any

VALID_MODES = {"none", "worktree", "container"}


def _normalize_mode(raw: Any, *, fallback: str) -> str:
    value = str(raw or "").strip().lower()
    if value in VALID_MODES:
        return value
    return fallback


def _keyword_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        token = str(entry or "").strip().lower()
        if token:
            out.append(token)
    return out


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    code_mode: str
    docs_mode: str
    fallback_mode: str
    default_timeout_sec: int
    mode_timeouts: dict[str, int]
    repo_modes: dict[str, str]
    docs_keywords: list[str]
    code_keywords: list[str]

    def classify_work_type(self, item: dict[str, Any]) -> str:
        text_parts: list[str] = []
        text_parts.append(str(item.get("title", "")))
        for task in item.get("tasks", []):
            text_parts.append(str(task))
        source_file = str(item.get("source_file", ""))
        if source_file:
            text_parts.append(source_file)
        text = " ".join(text_parts).lower()

        has_docs = any(keyword in text for keyword in self.docs_keywords)
        has_code = any(keyword in text for keyword in self.code_keywords)
        if source_file:
            lowered = source_file.lower()
            if "/docs/" in lowered or lowered.endswith(".md"):
                has_docs = True
        if has_docs and not has_code:
            return "docs"
        return "code"

    def sandbox_mode_for(self, *, item: dict[str, Any], repo: str) -> str:
        override = self.repo_modes.get(repo)
        if override:
            return override
        kind = self.classify_work_type(item)
        if kind == "docs":
            return self.docs_mode
        return self.code_mode

    def timeout_for_mode(self, mode: str) -> int:
        value = int(self.mode_timeouts.get(mode, self.default_timeout_sec) or self.default_timeout_sec)
        return max(30, value)


@lru_cache(maxsize=4)
def load_execution_policy(config_path: str | None = None) -> ExecutionPolicy:
    path = Path(config_path) if config_path else Path(__file__).with_name("execution_policy.toml")
    raw = tomllib.loads(path.read_text())

    defaults = raw.get("defaults", {}) if isinstance(raw.get("defaults", {}), dict) else {}
    fallback_mode = _normalize_mode(defaults.get("fallback_mode"), fallback="worktree")
    code_mode = _normalize_mode(defaults.get("code_mode"), fallback=fallback_mode)
    docs_mode = _normalize_mode(defaults.get("docs_mode"), fallback="none")
    default_timeout_sec = max(30, int(defaults.get("default_timeout_sec", 1200) or 1200))

    timeouts_raw = raw.get("timeouts", {}) if isinstance(raw.get("timeouts", {}), dict) else {}
    mode_timeouts: dict[str, int] = {}
    for mode in VALID_MODES:
        if mode in timeouts_raw:
            mode_timeouts[mode] = max(30, int(timeouts_raw.get(mode, default_timeout_sec) or default_timeout_sec))
    if code_mode not in mode_timeouts:
        mode_timeouts[code_mode] = default_timeout_sec
    if docs_mode not in mode_timeouts:
        mode_timeouts[docs_mode] = default_timeout_sec

    repo_modes_raw = raw.get("repo_modes", {}) if isinstance(raw.get("repo_modes", {}), dict) else {}
    repo_modes: dict[str, str] = {}
    for repo, mode_raw in repo_modes_raw.items():
        repo_name = str(repo).strip()
        if not repo_name:
            continue
        repo_modes[repo_name] = _normalize_mode(mode_raw, fallback=fallback_mode)

    classifiers = raw.get("classifiers", {}) if isinstance(raw.get("classifiers", {}), dict) else {}
    docs_keywords = _keyword_list(classifiers.get("docs_keywords", []))
    code_keywords = _keyword_list(classifiers.get("code_keywords", []))

    return ExecutionPolicy(
        code_mode=code_mode,
        docs_mode=docs_mode,
        fallback_mode=fallback_mode,
        default_timeout_sec=default_timeout_sec,
        mode_timeouts=mode_timeouts,
        repo_modes=repo_modes,
        docs_keywords=docs_keywords,
        code_keywords=code_keywords,
    )
