from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import tomllib


@dataclass(frozen=True, slots=True)
class RoadmapConfig:
    default_batch: str
    repo_codes: dict[str, str]
    roadmap_repo: dict[str, str]
    roadmap_batch: dict[str, str]

    def repo_code(self, repo: str) -> str:
        return self.repo_codes.get(repo, "WM")

    def batch_for_roadmap(self, roadmap_name: str) -> str:
        return self.roadmap_batch.get(roadmap_name, self.default_batch)

    def repo_for_roadmap(self, roadmap_name: str) -> str:
        return self.roadmap_repo.get(roadmap_name, "")


def _sanitize_token(value: str, *, fallback: str) -> str:
    token = "".join(ch for ch in str(value).upper() if ch.isalnum())
    if not token:
        token = fallback
    return token


@lru_cache(maxsize=4)
def load_roadmap_config(config_path: str | None = None) -> RoadmapConfig:
    path = Path(config_path) if config_path else Path(__file__).with_name("roadmap_ids.toml")
    raw = tomllib.loads(path.read_text())

    defaults = raw.get("defaults", {})
    default_batch = _sanitize_token(str(defaults.get("batch", "A01")), fallback="A01")[:3].ljust(3, "0")

    repo_codes_raw = raw.get("repos", {})
    repo_codes: dict[str, str] = {}
    if isinstance(repo_codes_raw, dict):
        for repo, code in repo_codes_raw.items():
            repo_name = str(repo).strip()
            if not repo_name:
                continue
            repo_codes[repo_name] = _sanitize_token(str(code), fallback="WM")[:4]

    roadmap_repo: dict[str, str] = {}
    roadmap_batch: dict[str, str] = {}
    roadmaps_raw = raw.get("roadmaps", {})
    if isinstance(roadmaps_raw, dict):
        for roadmap_name, payload in roadmaps_raw.items():
            name = str(roadmap_name).strip()
            if not name or not isinstance(payload, dict):
                continue
            repo = str(payload.get("repo", "")).strip()
            batch = _sanitize_token(str(payload.get("batch", default_batch)), fallback=default_batch)[:3].ljust(3, "0")
            if repo:
                roadmap_repo[name] = repo
            roadmap_batch[name] = batch

    return RoadmapConfig(
        default_batch=default_batch,
        repo_codes=repo_codes,
        roadmap_repo=roadmap_repo,
        roadmap_batch=roadmap_batch,
    )
