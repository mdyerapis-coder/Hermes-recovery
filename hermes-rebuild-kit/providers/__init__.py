from .aliases import AliasResolver, normalize_alias
from .bitwarden import BitwardenProvider
from .manifest import load_manifest
from .models import *
from .provider import SecretProvider
from .registry import ProviderRegistry, FakeProvider

if "bitwarden" not in ProviderRegistry._factories:
    ProviderRegistry.register("bitwarden", BitwardenProvider, default=True)
