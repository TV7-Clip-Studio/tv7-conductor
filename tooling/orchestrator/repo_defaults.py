from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import tomllib


@dataclass(frozen=True, slots=True)
class RepoDefaults:
    validation_commands: tuple[str, ...] = ()
    extra_labels: tuple[str, ...] = ()
    owner_hints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RepoDefaultsConfig:
    defaults: RepoDefaults
    repos: dict[str, RepoDefaults]

    def for_repo(self, repo: str) -> RepoDefaults:
        key = str(repo).strip()
        if key in self.repos:
            return self.repos[key]
        return self.defaults



def _normalize_tokens(values: object, *, lowercase: bool = False) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        token = str(raw).strip()
        if not token:
            continue
        normalized = token.lower() if lowercase else token
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)



def _to_defaults(payload: object) -> RepoDefaults:
    data = payload if isinstance(payload, dict) else {}
    return RepoDefaults(
        validation_commands=_normalize_tokens(data.get("validation_commands"), lowercase=False),
        extra_labels=_normalize_tokens(data.get("extra_labels"), lowercase=True),
        owner_hints=_normalize_tokens(data.get("owner_hints"), lowercase=False),
    )


@lru_cache(maxsize=4)
def load_repo_defaults(config_path: str | None = None) -> RepoDefaultsConfig:
    path = Path(config_path) if config_path else Path(__file__).with_name("repo_defaults.toml")
    raw = tomllib.loads(path.read_text())

    defaults = _to_defaults(raw.get("defaults", {}))
    repos: dict[str, RepoDefaults] = {}
    repos_raw = raw.get("repos", {})
    if isinstance(repos_raw, dict):
        for repo_name, payload in repos_raw.items():
            key = str(repo_name).strip()
            if not key:
                continue
            repo_cfg = _to_defaults(payload)
            repos[key] = RepoDefaults(
                validation_commands=repo_cfg.validation_commands or defaults.validation_commands,
                extra_labels=repo_cfg.extra_labels or defaults.extra_labels,
                owner_hints=repo_cfg.owner_hints or defaults.owner_hints,
            )

    return RepoDefaultsConfig(defaults=defaults, repos=repos)



def repo_defaults_for(repo: str, *, config_path: str | None = None) -> RepoDefaults:
    cfg = load_repo_defaults(config_path)
    return cfg.for_repo(repo)
