from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workspace_config import WorkspaceConfig, load_workspace_config


LEVEL_ORDER = {
    "observe": 0,
    "plan": 1,
    "apply": 2,
    "execute": 3,
    "review": 4,
    "integrate": 5,
}


@dataclass(frozen=True, slots=True)
class AutomationViolation:
    repo: str
    configured_level: str
    required_level: str
    command: str

    def message(self) -> str:
        return (
            f"{self.command} requires automation_level >= {self.required_level} "
            f"for {self.repo}; configured={self.configured_level}"
        )


def level_allows(configured: str, required: str) -> bool:
    configured_rank = LEVEL_ORDER.get(str(configured).strip(), -1)
    required_rank = LEVEL_ORDER.get(str(required).strip(), 999)
    return configured_rank >= required_rank


def load_workspace_config_optional(path: str | Path) -> WorkspaceConfig | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    config_path = Path(raw).expanduser().resolve()
    if not config_path.exists():
        return None
    return load_workspace_config(config_path)


def automation_violations(
    *,
    workspace_config: WorkspaceConfig | None,
    repos: list[str],
    required_level: str,
    command: str,
) -> list[AutomationViolation]:
    if workspace_config is None:
        return []
    targets = [str(repo).strip() for repo in repos if str(repo).strip()]
    if not targets:
        targets = sorted(workspace_config.repos.keys())

    violations: list[AutomationViolation] = []
    for repo in targets:
        profile = workspace_config.repos.get(repo)
        if profile is None:
            continue
        configured = str(profile.automation_level)
        if not level_allows(configured, required_level):
            violations.append(
                AutomationViolation(
                    repo=repo,
                    configured_level=configured,
                    required_level=required_level,
                    command=command,
                )
            )
    return violations
