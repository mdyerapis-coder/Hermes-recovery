from providers.bitwarden import BitwardenProvider
from providers.models import ProviderConfig

def test_status_safe_when_cli_missing(monkeypatch):
    monkeypatch.setattr('providers.bitwarden.shutil.which', lambda _: None)
    status=BitwardenProvider(ProviderConfig('bitwarden')).status()
    assert not status.available
