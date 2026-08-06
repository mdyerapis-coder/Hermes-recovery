from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

REDACTED = "<redacted>"

class CredentialType(str, Enum):
    API_KEY = "api_key"
    OAUTH_TOKEN = "oauth_token"
    BEARER_TOKEN = "bearer_token"
    STRUCTURED = "structured"
    LOCAL_SESSION = "local_session"

@dataclass(frozen=True)
class SecretReference:
    alias: str
    purpose: str | None = None
    optional: bool = False

@dataclass
class ResolvedSecret:
    alias: str
    value: Any
    credential_type: CredentialType = CredentialType.API_KEY
    provider_name: str | None = None
    account_name: str | None = None
    expires_at: datetime | None = None
    renewable: bool = False
    source_backend: str | None = None
    validation_method: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"ResolvedSecret(alias={self.alias!r}, value={REDACTED!r}, credential_type={self.credential_type.value!r})"

    __str__ = __repr__

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= now

    def safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["value"] = REDACTED
        if self.expires_at:
            data["expires_at"] = self.expires_at.isoformat()
        data["credential_type"] = self.credential_type.value
        return data

@dataclass(frozen=True)
class SecretRequirement:
    alias: str
    purpose: str
    optional: bool = False
    validation_method: str | None = None

@dataclass(frozen=True)
class SecretManifest:
    project: str
    required: tuple[SecretRequirement, ...] = ()
    optional: tuple[SecretRequirement, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class ProviderConfig:
    backend: str
    vault: str = "default"
    folder: str = "AI & LLM/API Keys/Active"
    cache: str = "session"
    validate: bool = True
    fail_closed: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ProviderStatus:
    backend: str
    available: bool
    authenticated: bool = False
    unlocked: bool = False
    message: str = ""

@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    method: str | None = None
    message: str = ""
