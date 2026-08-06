from __future__ import annotations
from pathlib import Path
import re
import yaml
from .aliases import AliasResolver
from .exceptions import ManifestValidationError
from .models import SecretManifest, SecretRequirement

_ALLOWED_TOP = {"project", "secrets", "aliases"}
_ALLOWED_SECRET_KEYS = {"required", "optional"}
_ALLOWED_PURPOSES = {
    "primary_llm", "fallback_llm", "repository_access", "coding_provider",
    "alternate_provider", "opencode_auth", "messaging", "network_access",
    "compute_provider", "vault_access", "recovery"
}

def _requirement(raw, optional, resolver):
    if not isinstance(raw, dict) or set(raw) - {"alias", "purpose", "validation_method"}:
        raise ManifestValidationError("invalid secret requirement")
    alias = raw.get("alias")
    purpose = raw.get("purpose")
    if not isinstance(alias, str) or not isinstance(purpose, str):
        raise ManifestValidationError("secret requirement needs string alias and purpose")
    if purpose not in _ALLOWED_PURPOSES:
        raise ManifestValidationError(f"unsupported secret purpose: {purpose}")
    return SecretRequirement(resolver.resolve(alias), purpose, optional, raw.get("validation_method"))

def load_manifest(path: str | Path) -> SecretManifest:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ManifestValidationError(f"cannot load manifest: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or set(data) - _ALLOWED_TOP:
        raise ManifestValidationError("manifest contains unknown top-level fields")
    project = data.get("project")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z][a-z0-9_\-]*", project):
        raise ManifestValidationError("invalid project identifier")
    aliases = data.get("aliases") or {}
    if not isinstance(aliases, dict):
        raise ManifestValidationError("aliases must be a mapping")
    resolver = AliasResolver(project_overrides=aliases)
    secrets = data.get("secrets") or {}
    if not isinstance(secrets, dict) or set(secrets) - _ALLOWED_SECRET_KEYS:
        raise ManifestValidationError("invalid secrets section")
    required = tuple(_requirement(x, False, resolver) for x in secrets.get("required", []))
    optional = tuple(_requirement(x, True, resolver) for x in secrets.get("optional", []))
    names = [x.alias for x in required + optional]
    if len(names) != len(set(names)):
        raise ManifestValidationError("duplicate secret alias in manifest")
    return SecretManifest(project, required, optional, aliases)
