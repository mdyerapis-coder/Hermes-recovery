from __future__ import annotations
import yaml
from pathlib import Path
from .manifest import load_manifest
from .models import ProviderConfig
from .registry import ProviderRegistry


def load_provider_config(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = data.get("secret_provider") or {}
    allowed = {"backend", "vault", "folder", "cache", "validate", "fail_closed"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown secret_provider fields: {', '.join(sorted(unknown))}")
    return ProviderConfig(**raw)


def preflight_secrets(config_path, manifest_path):
    config = load_provider_config(config_path)
    manifest = load_manifest(manifest_path)
    provider = ProviderRegistry.create(config)
    with provider.session():
        required = provider.resolve_requirements(manifest.required)
        optional = provider.resolve_requirements(manifest.optional)
    return {**required, **optional}
