"""Load a dependency-free JSON manifest for Hermes Secret Provider."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import FolderScope, SecretSpec


@dataclass(frozen=True)
class ProviderConfig:
    backend: str
    scope: FolderScope
    sync: bool
    interactive_login: bool
    lock_on_exit: bool
    secrets: tuple[SecretSpec, ...]


def _as_tuple(values: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"{field} must be a non-empty string list")
    return tuple(values)


def load_manifest(path: str | Path) -> ProviderConfig:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    provider = document.get("secret_provider") or {}
    if provider.get("backend", "bitwarden") != "bitwarden":
        raise ValueError("only the bitwarden backend is supported in v1.2")

    scope_doc = provider.get("folder_scope") or {}
    roots = _as_tuple(scope_doc.get("roots", ["AI & LLM"]), field="folder_scope.roots")
    scope = FolderScope(
        roots=roots,
        include_descendants=bool(scope_doc.get("include_descendants", True)),
    )

    specs: list[SecretSpec] = []
    for raw in document.get("secrets") or []:
        name = str(raw["name"])
        env = str(raw["env"])
        aliases = _as_tuple(raw.get("aliases", [name]), field=f"{name}.aliases")
        field_names = _as_tuple(
            raw.get("field_names", ["api_key", "token", "secret", "password"]),
            field=f"{name}.field_names",
        )
        specs.append(
            SecretSpec(
                name=name,
                env=env,
                aliases=aliases,
                required=bool(raw.get("required", False)),
                field_names=field_names,
                validator=str(raw.get("validator", "nonempty")),
            )
        )

    return ProviderConfig(
        backend="bitwarden",
        scope=scope,
        sync=bool(provider.get("sync", True)),
        interactive_login=bool(provider.get("interactive_login", True)),
        lock_on_exit=bool(provider.get("lock_on_exit", True)),
        secrets=tuple(specs),
    )
