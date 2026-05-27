from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentProfile:
    name: str
    assess_agent: str
    model: str
    reasoning: str
    personality: str
    notes: str = ""


PROFILES: dict[str, AgentProfile] = {
    "local_auto_medium": AgentProfile(
        name="local_auto_medium",
        assess_agent="auto",
        model="gpt-5-codex",
        reasoning="medium",
        personality="pragmatic",
        notes="Default local profile. Uses local CLI providers (codex first, gemini queue fallback).",
    ),
    "gemini_pro_medium": AgentProfile(
        name="gemini_pro_medium",
        assess_agent="gemini",
        model="gemini-2.5-pro",
        reasoning="medium",
        personality="pragmatic",
        notes="Gemini-oriented profile. Uses local CLI queue guidance when direct automation is unavailable.",
    ),
    "codex_fast_low": AgentProfile(
        name="codex_fast_low",
        assess_agent="codex",
        model="gpt-5-codex",
        reasoning="low",
        personality="pragmatic",
        notes="Optional fallback profile for fully automated local assessment.",
    ),
    "antigravity_queue_medium": AgentProfile(
        name="antigravity_queue_medium",
        assess_agent="antigravity",
        model="gemini-2.5-pro",
        reasoning="medium",
        personality="pragmatic",
        notes="Legacy compatibility profile; queues manual assessment payloads.",
    ),
}


def list_profiles() -> list[AgentProfile]:
    return [PROFILES[k] for k in sorted(PROFILES.keys())]


def resolve_profile(name: str) -> AgentProfile:
    key = (name or "").strip()
    if key in PROFILES:
        return PROFILES[key]
    raise KeyError(f"Unknown agent profile: {name}")
