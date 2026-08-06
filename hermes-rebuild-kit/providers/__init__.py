from .bitwarden import BitwardenProvider
from .registry import ProviderRegistry

try:
    ProviderRegistry.register(
        "bitwarden",
        BitwardenProvider,
        default=True,
    )
except Exception as exc:
    if "already registered" not in str(exc):
        raise

__all__ = [
    "BitwardenProvider",
    "ProviderRegistry",
]
