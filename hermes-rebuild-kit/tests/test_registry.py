import pytest
from datetime import datetime, timedelta, timezone
from providers.models import ProviderConfig, ResolvedSecret, CredentialType
from providers.registry import ProviderRegistry, FakeProvider
from providers.exceptions import ProviderConfigurationError, ProviderUnavailableError, SecretValidationError

def reset(): ProviderRegistry._factories={}; ProviderRegistry._default=None

def test_registration_and_default():
    reset(); ProviderRegistry.register('fake', lambda c: FakeProvider(c, {'openai':'x'}), default=True)
    assert ProviderRegistry.default().resolve('openai').value == 'x'
def test_duplicate_registration():
    reset(); ProviderRegistry.register('fake', lambda c: FakeProvider(c))
    with pytest.raises(ProviderConfigurationError): ProviderRegistry.register('fake', lambda c: FakeProvider(c))
def test_missing_backend():
    reset()
    with pytest.raises(ProviderUnavailableError): ProviderRegistry.create(ProviderConfig('missing'))
def test_secret_redaction_and_expiry():
    s=ResolvedSecret('opencode','TOPSECRET',CredentialType.OAUTH_TOKEN,expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    assert 'TOPSECRET' not in repr(s); assert s.safe_dict()['value']=='<redacted>'
    p=FakeProvider(ProviderConfig('fake'), {'opencode':s})
    with pytest.raises(SecretValidationError) as e: p.resolve('opencode')
    assert 'TOPSECRET' not in str(e.value)
