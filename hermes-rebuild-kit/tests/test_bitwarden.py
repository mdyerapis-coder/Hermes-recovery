import json
from providers.bitwarden import BitwardenProvider
from providers.models import ProviderConfig

def test_status_safe_when_cli_missing(monkeypatch):
    monkeypatch.setattr('providers.bitwarden.shutil.which', lambda _: None)
    status=BitwardenProvider(ProviderConfig('bitwarden')).status()
    assert not status.available


def test_explicit_item_mapping_is_used():
    mapped_config = ProviderConfig(
        backend="bitwarden",
        folder="AI & LLM/API Keys/Active",
        items={
            "openrouter": "OpenRouter — API Key — Primary",
        },
    )
    p = BitwardenProvider(mapped_config)

    def fake_run(*args, **kwargs):
        if args == ("list", "folders"):
            return json.dumps(
                [
                    {
                        "id": "folder-active",
                        "name": "AI & LLM/API Keys/Active",
                    }
                ]
            )

        assert args == (
            "list",
            "items",
            "--search",
            "OpenRouter — API Key — Primary",
            "--folderid",
            "folder-active",
        )

        return json.dumps(
            [
                {
                    "id": "primary-openrouter",
                    "name": "OpenRouter — API Key — Primary",
                    "folderId": "folder-active",
                    "login": {"password": "test-secret"},
                }
            ]
        )

    p._run = fake_run

    resolved = p._resolve("openrouter")

    assert resolved.value == "test-secret"
    assert resolved.metadata["item_id"] == "primary-openrouter"
