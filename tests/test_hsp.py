from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay" / "lib"))

from hermes_hsp.aliases import choose_unique, extract_secret, score_item
from hermes_hsp.bitwarden import BitwardenProvider
from hermes_hsp.cache import SecretCache
from hermes_hsp.errors import AmbiguousSecret, InvalidSecret, SecretNotFound
from hermes_hsp.manifest import load_manifest
from hermes_hsp.models import SecretSpec
from hermes_hsp.provider import resolve_environment
from hermes_hsp.validators import validate_value


class AliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = SecretSpec(
            name="openai",
            env="OPENAI_API_KEY",
            aliases=("OpenAI", "OpenAI API Key"),
            field_names=("api_key", "token"),
            validator="token",
        )

    def test_exact_name_beats_partial(self) -> None:
        items = [
            {"id": "1", "name": "OpenAI development"},
            {"id": "2", "name": "OpenAI"},
        ]
        self.assertEqual(choose_unique(items, self.spec).item["id"], "2")

    def test_canonical_field_wins(self) -> None:
        item = {
            "id": "1",
            "name": "Production model provider",
            "fields": [{"name": "hermes_secret", "value": "openai"}],
        }
        self.assertGreaterEqual(score_item(item, self.spec).score, 200)

    def test_retired_item_is_penalised(self) -> None:
        active = {"id": "1", "name": "OpenAI active"}
        old = {"id": "2", "name": "OpenAI revoked"}
        self.assertGreater(score_item(active, self.spec).score, score_item(old, self.spec).score)

    def test_ambiguous_exact_matches_fail(self) -> None:
        items = [{"id": "1", "name": "OpenAI"}, {"id": "2", "name": "OpenAI"}]
        with self.assertRaises(AmbiguousSecret):
            choose_unique(items, self.spec)

    def test_missing_match_fails(self) -> None:
        with self.assertRaises(SecretNotFound):
            choose_unique([{"id": "1", "name": "Unrelated"}], self.spec)

    def test_custom_field_precedes_password(self) -> None:
        item = {
            "name": "OpenAI",
            "fields": [{"name": "api_key", "value": "field-value"}],
            "login": {"password": "password-value"},
        }
        self.assertEqual(extract_secret(item, self.spec), ("field-value", "custom:api_key"))


class ValidatorTests(unittest.TestCase):
    def test_telegram(self) -> None:
        validate_value("telegram", "123456:abcdefghijklmnopqrstuvwxyz_123", "telegram_bot_token")
        with self.assertRaises(InvalidSecret):
            validate_value("telegram", "not-a-token", "telegram_bot_token")

    def test_tailscale(self) -> None:
        validate_value("tailscale", "tskey-auth-abc123", "tailscale_auth_key")
        with self.assertRaises(InvalidSecret):
            validate_value("tailscale", "abc123", "tailscale_auth_key")


class CacheTests(unittest.TestCase):
    def test_clear(self) -> None:
        cache = SecretCache()
        cache.set("openai", "secret")
        self.assertEqual(cache.get("openai"), "secret")
        cache.clear()
        self.assertIsNone(cache.get("openai"))
        self.assertEqual(len(cache), 0)


class ManifestTests(unittest.TestCase):
    def test_default_manifest_loads(self) -> None:
        config = load_manifest(ROOT / "overlay" / "manifests" / "secrets.json")
        self.assertEqual(config.backend, "bitwarden")
        self.assertIn("AI & LLM", config.scope.roots)
        self.assertTrue(any(spec.env == "OPENAI_API_KEY" for spec in config.secrets))


class FakeBitwarden(BitwardenProvider):
    def __init__(self, config, items):
        super().__init__(config, environ={"PATH": os.environ.get("PATH", "")})
        self._fake_items = items

    def list_scoped_items(self):
        return self._fake_items

    def close(self):
        self.cache.clear()


class ProviderTests(unittest.TestCase):
    def test_preserves_existing_environment(self) -> None:
        config = load_manifest(ROOT / "overlay" / "manifests" / "secrets.json")
        provider = FakeBitwarden(config, [])
        env, events = resolve_environment(config, {"OPENAI_API_KEY": "existing"}, provider)
        self.assertEqual(env["OPENAI_API_KEY"], "existing")
        self.assertTrue(any(event.status == "PRESERVE" for event in events))

    def test_resolves_unique_item_without_printing_value(self) -> None:
        config = load_manifest(ROOT / "overlay" / "manifests" / "secrets.json")
        openai = next(spec for spec in config.secrets if spec.name == "openai")
        provider = FakeBitwarden(
            config,
            [{"id": "item-1", "name": "OpenAI", "fields": [{"name": "api_key", "value": "sk-test-123456789"}]}],
        )
        resolution = provider.resolve(openai)
        self.assertEqual(resolution.value, "sk-test-123456789")
        self.assertNotIn(resolution.value, resolution.item_name)


if __name__ == "__main__":
    unittest.main()
