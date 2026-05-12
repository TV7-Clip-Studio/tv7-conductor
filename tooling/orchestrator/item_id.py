from __future__ import annotations

from dataclasses import dataclass
import re

from .roadmap_id_config import RoadmapConfig, load_roadmap_config

_ID_RE = re.compile(
    r"^(?P<repo>[A-Z0-9]{2,4})-(?P<batch>[A-Z0-9]{2,6})-(?P<epic>[A-Z0-9]{2,6})-(?P<ticket>[A-Z0-9]{2,6})$"
)


@dataclass(frozen=True, slots=True)
class ParsedItemId:
    raw: str
    repo: str
    batch: str
    epic: str
    ticket: str



def _normalize_token(value: str, *, name: str, min_len: int, max_len: int) -> str:
    token = "".join(ch for ch in str(value).upper() if ch.isalnum())
    if len(token) < min_len or len(token) > max_len:
        raise ValueError(
            f"{name} must be {min_len}-{max_len} alphanumeric uppercase characters; got `{value}`"
        )
    return token



def _allowed_repo_codes(config: RoadmapConfig) -> set[str]:
    out = {"WM"}
    out.update({str(code).upper() for code in config.repo_codes.values() if str(code).strip()})
    return out



def parse_item_id(
    item_id: str,
    *,
    config: RoadmapConfig | None = None,
    expected_repo_code: str = "",
    allow_unknown_repo_code: bool = False,
) -> ParsedItemId:
    raw = str(item_id or "").strip().upper()
    if not raw:
        raise ValueError("item id cannot be empty")

    match = _ID_RE.match(raw)
    if not match:
        raise ValueError(
            "item id must match `REPO-BATCH-EPIC-TICKET` "
            "with 4 uppercase alphanumeric segments"
        )

    parsed = ParsedItemId(
        raw=raw,
        repo=match.group("repo"),
        batch=match.group("batch"),
        epic=match.group("epic"),
        ticket=match.group("ticket"),
    )

    cfg = config or load_roadmap_config()
    allowed = _allowed_repo_codes(cfg)
    if parsed.repo not in allowed and not allow_unknown_repo_code:
        raise ValueError(
            f"unknown repo code `{parsed.repo}`; allowed codes: {', '.join(sorted(allowed))}"
        )

    expected = _normalize_token(expected_repo_code, name="expected_repo_code", min_len=2, max_len=4) if expected_repo_code else ""
    if expected and parsed.repo != expected:
        raise ValueError(
            f"repo code mismatch: item id has `{parsed.repo}` but roadmap expects `{expected}`"
        )

    return parsed



def build_item_id(
    *,
    repo_code: str,
    batch_code: str,
    epic_code: str,
    ticket_code: str,
    config: RoadmapConfig | None = None,
) -> str:
    repo = _normalize_token(repo_code, name="repo_code", min_len=2, max_len=4)
    batch = _normalize_token(batch_code, name="batch_code", min_len=2, max_len=6)
    epic = _normalize_token(epic_code, name="epic_code", min_len=2, max_len=6)
    ticket = _normalize_token(ticket_code, name="ticket_code", min_len=2, max_len=6)

    item_id = f"{repo}-{batch}-{epic}-{ticket}"
    parse_item_id(item_id, config=config)
    return item_id
