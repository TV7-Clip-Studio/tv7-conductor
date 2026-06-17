from __future__ import annotations


def _gemini_capacity_error(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in ["capacity", "exhausted", "quota", "429", "rate limit", "too many requests"]
    )


def _gemini_command(model: str) -> list[str]:
    return ["gemini", "-m", model, "-p", "-", "-o", "json"]
