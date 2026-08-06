class SecretProviderError(RuntimeError):
    """Base error for secret-provider failures."""

class ProviderUnavailableError(SecretProviderError): pass
class ProviderConfigurationError(SecretProviderError): pass
class SecretNotFoundError(SecretProviderError): pass
class SecretValidationError(SecretProviderError): pass
class AliasResolutionError(SecretProviderError): pass
class AmbiguousAliasError(AliasResolutionError): pass
class ManifestValidationError(SecretProviderError): pass
class CacheError(SecretProviderError): pass
