from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib


ALLOWED_ROLES = {"planner", "executor", "reviewer"}
ALLOWED_KINDS = {"local_command", "ao", "api", "local_llm"}


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    name: str
    role: str
    kind: str
    command: str = ""
    model: str = ""
    timeout_sec: int = 600
    retries: int = 0
    paid: bool = False
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    providers: dict[str, ProviderProfile]
    defaults: dict[str, str]
    allow_paid_review: bool = False


def _default_path() -> Path:
    return Path(__file__).resolve().with_name("provider_config.toml")


def _int_value(raw: Any, *, default: int) -> int:
    if raw in (None, ""):
        return default
    return max(0, int(raw))


def _provider_from_payload(name: str, payload: dict[str, Any]) -> ProviderProfile:
    role = str(payload.get("role", "")).strip()
    if role not in ALLOWED_ROLES:
        allowed = ", ".join(sorted(ALLOWED_ROLES))
        raise ValueError(f"unsupported provider role for {name}: {role}; allowed: {allowed}")
    kind = str(payload.get("kind", "")).strip()
    if kind not in ALLOWED_KINDS:
        allowed = ", ".join(sorted(ALLOWED_KINDS))
        raise ValueError(f"unsupported provider kind for {name}: {kind}; allowed: {allowed}")
    return ProviderProfile(
        name=name,
        role=role,
        kind=kind,
        command=str(payload.get("command", "")).strip(),
        model=str(payload.get("model", "")).strip(),
        timeout_sec=_int_value(payload.get("timeout_sec"), default=600),
        retries=_int_value(payload.get("retries"), default=0),
        paid=bool(payload.get("paid", False)),
        notes=str(payload.get("notes", "")).strip(),
    )


def load_provider_config(path: Path | None = None) -> ProviderConfig:
    config_path = (path or _default_path()).expanduser().resolve()
    raw = tomllib.loads(config_path.read_text())

    providers_raw = raw.get("providers", {})
    if not isinstance(providers_raw, dict):
        raise ValueError("[providers] must be a table")
    providers: dict[str, ProviderProfile] = {}
    for name, payload in providers_raw.items():
        if not isinstance(payload, dict):
            raise ValueError(f"[providers.{name}] must be a table")
        provider_name = str(name).strip()
        if not provider_name:
            raise ValueError("provider names must not be empty")
        providers[provider_name] = _provider_from_payload(provider_name, payload)

    defaults_raw = raw.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise ValueError("[defaults] must be a table")
    defaults: dict[str, str] = {}
    for role in ALLOWED_ROLES:
        selected = str(defaults_raw.get(role, "")).strip()
        if not selected:
            raise ValueError(f"missing default provider for role: {role}")
        provider = providers.get(selected)
        if provider is None:
            raise ValueError(f"default provider for {role} does not exist: {selected}")
        if provider.role != role:
            raise ValueError(
                f"default provider for {role} has role {provider.role}: {selected}"
            )
        defaults[role] = selected

    return ProviderConfig(
        providers=providers,
        defaults=defaults,
        allow_paid_review=bool(defaults_raw.get("allow_paid_review", False)),
    )


def provider_for_role(
    config: ProviderConfig,
    role: str,
    *,
    name: str = "",
    allow_paid: bool | None = None,
) -> ProviderProfile:
    role_name = str(role).strip()
    if role_name not in ALLOWED_ROLES:
        allowed = ", ".join(sorted(ALLOWED_ROLES))
        raise ValueError(f"unsupported provider role: {role}; allowed: {allowed}")
    provider_name = str(name).strip() or config.defaults[role_name]
    provider = config.providers.get(provider_name)
    if provider is None:
        raise KeyError(f"unknown provider: {provider_name}")
    if provider.role != role_name:
        raise ValueError(f"provider {provider_name} has role {provider.role}, not {role_name}")
    paid_allowed = config.allow_paid_review if allow_paid is None else bool(allow_paid)
    if role_name == "reviewer" and provider.paid and not paid_allowed:
        raise PermissionError(
            f"paid reviewer provider is disabled by policy: {provider_name}"
        )
    return provider
