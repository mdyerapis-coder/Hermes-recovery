from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml

from .manifest import load_manifest
from .models import ProviderConfig
from .registry import ProviderRegistry


def load_provider_config(path: str | Path) -> ProviderConfig | None:
    """Load optional HSP configuration.

    Existing v1.1.0 configurations without a secret_provider section remain
    valid and continue using env/file/prompt secret resolution.
    """
    config_path = Path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if "secret_provider" not in data:
        return None

    raw = data.get("secret_provider")
    if not isinstance(raw, dict):
        raise ValueError("secret_provider must be a mapping")

    allowed = {
        "backend",
        "vault",
        "folder",
        "cache",
        "validate",
        "fail_closed",
        "items",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            "unknown secret_provider fields: "
            + ", ".join(sorted(unknown))
        )

    backend = str(raw.get("backend", "")).strip()
    if not backend:
        raise ValueError("secret_provider.backend is required")

    return ProviderConfig(**raw)


@contextmanager
def provider_session(
    config_path: str | Path,
    manifest_path: str | Path,
) -> Iterator[dict[str, object]]:
    """Resolve secrets while keeping the provider session open.

    Secret values are returned only in memory. Provider shutdown, cache
    clearing and any backend-specific vault relocking occur on context exit.
    """
    config = load_provider_config(config_path)

    if config is None:
        yield {}
        return

    manifest = load_manifest(manifest_path)
    provider = ProviderRegistry.create(config)

    with provider.session():
        required = provider.resolve_requirements(manifest.required)
        optional = provider.resolve_requirements(manifest.optional)
        resolved = {**required, **optional}

        yield {
            alias: secret.value
            for alias, secret in resolved.items()
        }
