"""Data models for Hermes Secret Provider."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SecretSpec:
    name: str
    env: str
    aliases: tuple[str, ...]
    required: bool = False
    field_names: tuple[str, ...] = ("api_key", "token", "secret", "password")
    validator: str = "nonempty"


@dataclass(frozen=True)
class FolderScope:
    roots: tuple[str, ...] = ("AI & LLM",)
    include_descendants: bool = True
    active_only: bool = True


@dataclass
class Match:
    item: dict[str, Any]
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Resolution:
    name: str
    env: str
    item_id: str
    item_name: str
    source_field: str
    value: str
