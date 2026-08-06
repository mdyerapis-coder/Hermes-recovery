"""Hermes Secret Provider public API."""

from .bitwarden import BitwardenProvider
from .errors import HSPError
from .manifest import ProviderConfig, load_manifest
from .provider import ResolutionEvent, resolve_environment

__all__ = [
    "BitwardenProvider",
    "HSPError",
    "ProviderConfig",
    "ResolutionEvent",
    "load_manifest",
    "resolve_environment",
]
