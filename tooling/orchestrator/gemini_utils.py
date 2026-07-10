from __future__ import annotations


def _gemini_capacity_error(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in ["capacity", "exhausted", "quota", "429", "rate limit", "too many requests"]
    )


def _gemini_error_summary(output: str, model: str) -> str:
    lowered = output.lower()
    if "subscription_required" in lowered or "valid license" in lowered or "request a license" in lowered:
        return f"{model} unavailable: license required (403)"
    if "permission_denied" in lowered or "forbidden" in lowered or "403" in lowered:
        return f"{model} unavailable: permission denied (403)"
    if _gemini_capacity_error(output):
        return f"{model} unavailable: capacity or quota limit"
    if "failed to connect to ide companion" in lowered:
        return f"{model} unavailable: Gemini CLI IDE companion is not connected"
    return f"{model} unavailable: Gemini CLI failed"


def _gemini_command(model: str) -> list[str]:
    return ["gemini", "-m", model, "-p", "-", "-o", "json"]
