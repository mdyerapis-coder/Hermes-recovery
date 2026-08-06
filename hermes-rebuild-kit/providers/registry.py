from __future__ import annotations
from typing import Callable
from .exceptions import ProviderConfigurationError, ProviderUnavailableError, SecretNotFoundError
from .models import ProviderConfig, ProviderStatus, ResolvedSecret
from .provider import SecretProvider

ProviderFactory = Callable[[ProviderConfig], SecretProvider]

class ProviderRegistry:
    _factories: dict[str, ProviderFactory] = {}
    _default: str | None = None

    @classmethod
    def register(cls, name: str, factory: ProviderFactory, *, default=False):
        key = name.strip().lower()
        if key in cls._factories:
            raise ProviderConfigurationError(f"provider already registered: {key}")
        cls._factories[key] = factory
        if default:
            cls._default = key

    @classmethod
    def unregister(cls, name: str):
        cls._factories.pop(name.strip().lower(), None)

    @classmethod
    def create(cls, config: ProviderConfig) -> SecretProvider:
        key = config.backend.strip().lower()
        factory = cls._factories.get(key)
        if not factory:
            raise ProviderUnavailableError(f"configured provider is unavailable: {key}")
        return factory(config)

    @classmethod
    def default(cls, config: ProviderConfig | None = None) -> SecretProvider:
        if config:
            return cls.create(config)
        if not cls._default:
            raise ProviderUnavailableError("no default provider configured")
        return cls.create(ProviderConfig(backend=cls._default))

class FakeProvider(SecretProvider):
    backend_name = "fake"
    def __init__(self, config, values=None):
        super().__init__(config)
        self.values = values or {}
    def status(self):
        return ProviderStatus("fake", True, True, True, "test provider")
    def _resolve(self, canonical_alias):
        if canonical_alias not in self.values:
            raise SecretNotFoundError(f"secret not found for alias {canonical_alias}")
        value = self.values[canonical_alias]
        return value if isinstance(value, ResolvedSecret) else ResolvedSecret(canonical_alias, value, source_backend="fake")
