from __future__ import annotations
from abc import ABC, abstractmethod
from contextlib import contextmanager
from .cache import SessionCache
from .exceptions import SecretNotFoundError, SecretValidationError
from .models import ProviderConfig, ProviderStatus, ResolvedSecret, SecretRequirement, ValidationResult

class SecretProvider(ABC):
    backend_name = "abstract"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.cache = SessionCache()

    def start(self) -> None: pass

    def shutdown(self) -> None:
        self.cache.clear()

    @contextmanager
    def session(self):
        self.start()
        try:
            yield self
        finally:
            self.shutdown()

    @abstractmethod
    def status(self) -> ProviderStatus: ...

    @abstractmethod
    def _resolve(self, canonical_alias: str) -> ResolvedSecret: ...

    def validate(self, secret: ResolvedSecret) -> ValidationResult:
        if secret.is_expired():
            return ValidationResult(False, secret.validation_method, "credential expired")
        if secret.value is None and secret.credential_type.value != "local_session":
            return ValidationResult(False, secret.validation_method, "credential missing")
        return ValidationResult(True, secret.validation_method, "validated")

    def resolve(self, canonical_alias: str) -> ResolvedSecret:
        try:
            secret = self.cache.get(canonical_alias)
        except KeyError:
            secret = self._resolve(canonical_alias)
            self.cache.set(canonical_alias, secret)
        if self.config.validate:
            result = self.validate(secret)
            if not result.valid:
                raise SecretValidationError(f"validation failed for alias {canonical_alias}: {result.message}")
        return secret

    def resolve_requirements(self, requirements: tuple[SecretRequirement, ...]):
        resolved = {}
        for req in requirements:
            try:
                resolved[req.alias] = self.resolve(req.alias)
            except SecretNotFoundError:
                if not req.optional:
                    raise
        return resolved
