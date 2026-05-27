from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


ALLOWED_AUTOMATION_LEVELS = {"observe", "plan", "apply", "execute", "review", "integrate"}


@dataclass(frozen=True)
class PreviewProfile:
    command: str = ""
    default_port: int = 0
    preview_port: int = 0
    health_url: str = ""


@dataclass(frozen=True)
class RepoProfile:
    name: str
    path: Path
    roadmap_sources: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    validation_commands: tuple[str, ...] = ()
    preview: PreviewProfile = field(default_factory=PreviewProfile)
    automation_level: str = "plan"


@dataclass(frozen=True)
class WorkspaceConfig:
    workspace_dir: Path
    state_dir: Path
    repos: dict[str, RepoProfile]


def _string_list(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    raise ValueError(f"expected string or list of strings, got {type(raw).__name__}")


def _int_value(raw: Any) -> int:
    if raw in (None, ""):
        return 0
    return int(raw)


def load_workspace_config(path: Path) -> WorkspaceConfig:
    config_path = path.expanduser().resolve()
    raw = tomllib.loads(config_path.read_text())

    workspace_raw = raw.get("workspace", {})
    if not isinstance(workspace_raw, dict):
        raise ValueError("[workspace] must be a table")
    workspace_dir = Path(str(workspace_raw.get("root", config_path.parent))).expanduser()
    if not workspace_dir.is_absolute():
        workspace_dir = (config_path.parent / workspace_dir).resolve()
    else:
        workspace_dir = workspace_dir.resolve()

    state_dir_raw = str(workspace_raw.get("state_dir", "docs/.conductor")).strip()
    state_dir = Path(state_dir_raw).expanduser()
    if not state_dir.is_absolute():
        state_dir = (config_path.parent / state_dir).resolve()
    else:
        state_dir = state_dir.resolve()

    repos_raw = raw.get("repos", {})
    if not isinstance(repos_raw, dict):
        raise ValueError("[repos] must be a table")

    repos: dict[str, RepoProfile] = {}
    for name, payload in repos_raw.items():
        if not isinstance(payload, dict):
            raise ValueError(f"[repos.{name}] must be a table")
        repo_path_raw = str(payload.get("path", name)).strip()
        repo_path = Path(repo_path_raw).expanduser()
        if not repo_path.is_absolute():
            repo_path = (workspace_dir / repo_path).resolve()
        else:
            repo_path = repo_path.resolve()

        preview_raw = payload.get("preview", {})
        if preview_raw is None:
            preview_raw = {}
        if not isinstance(preview_raw, dict):
            raise ValueError(f"[repos.{name}.preview] must be a table")

        automation_level = str(payload.get("automation_level", "plan")).strip() or "plan"
        if automation_level not in ALLOWED_AUTOMATION_LEVELS:
            allowed = ", ".join(sorted(ALLOWED_AUTOMATION_LEVELS))
            raise ValueError(f"unsupported automation_level for {name}: {automation_level}; allowed: {allowed}")

        repos[str(name)] = RepoProfile(
            name=str(name),
            path=repo_path,
            roadmap_sources=_string_list(payload.get("roadmap_sources")),
            labels=_string_list(payload.get("labels")),
            validation_commands=_string_list(payload.get("validation_commands")),
            preview=PreviewProfile(
                command=str(preview_raw.get("command", "")).strip(),
                default_port=_int_value(preview_raw.get("default_port")),
                preview_port=_int_value(preview_raw.get("preview_port")),
                health_url=str(preview_raw.get("health_url", "")).strip(),
            ),
            automation_level=automation_level,
        )

    return WorkspaceConfig(workspace_dir=workspace_dir, state_dir=state_dir, repos=repos)
