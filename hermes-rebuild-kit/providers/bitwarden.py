from __future__ import annotations
import json, os, shutil, subprocess
from typing import Any
from .exceptions import ProviderUnavailableError, ProviderConfigurationError, SecretNotFoundError
from .models import CredentialType, ProviderStatus, ResolvedSecret
from .provider import SecretProvider

class BitwardenProvider(SecretProvider):
    backend_name = "bitwarden"
    def __init__(self, config):
        super().__init__(config)
        self._session = os.environ.get("BW_SESSION")
        self._unlocked_by_us = False

    def _run(self, *args, input_text=None):
        env = os.environ.copy()
        if self._session:
            env["BW_SESSION"] = self._session
        proc = subprocess.run(["bw", *args], input=input_text, text=True, capture_output=True, env=env, check=False)
        if proc.returncode:
            raise ProviderUnavailableError(f"Bitwarden command failed: {' '.join(args[:2])}")
        return proc.stdout

    def status(self):
        if not shutil.which("bw"):
            return ProviderStatus("bitwarden", False, message="bw CLI not installed")
        try:
            data = json.loads(self._run("status"))
        except Exception:
            return ProviderStatus("bitwarden", True, message="status unavailable")
        state = data.get("status")
        return ProviderStatus("bitwarden", True, state in {"locked", "unlocked"}, state == "unlocked", state or "unknown")

    def start(self):
        if not shutil.which("bw"):
            raise ProviderUnavailableError("bw CLI is not installed")
        status = self.status()
        if not status.authenticated:
            raise ProviderUnavailableError("Bitwarden is not logged in")
        if not status.unlocked and not self._session:
            password = os.environ.get("BW_PASSWORD")
            if not password:
                raise ProviderUnavailableError("Bitwarden vault is locked and no BW_SESSION/BW_PASSWORD is available")
            self._session = self._run("unlock", "--raw", "--passwordenv", "BW_PASSWORD").strip()
            self._unlocked_by_us = True
        self._run("sync")

    def shutdown(self):
        try:
            if self._unlocked_by_us:
                self._run("lock")
        finally:
            self._session = None
            self._unlocked_by_us = False
            super().shutdown()

    def _extract_value(self, item: dict[str, Any], alias: str):
        login = item.get("login") or {}
        if login.get("password"):
            return login["password"], CredentialType.API_KEY
        for field in item.get("fields") or []:
            if str(field.get("name", "")).lower() in {"api key", "token", "secret", alias.lower()} and field.get("value"):
                return field["value"], CredentialType.API_KEY
        if item.get("notes"):
            return item["notes"].strip(), CredentialType.STRUCTURED
        return None, CredentialType.API_KEY

    @staticmethod
    def _normalise_name(value: str) -> str:
        return "_".join(
            part
            for part in value.strip().lower()
            .replace("-", "_")
            .replace("/", "_")
            .replace(" ", "_")
            .split("_")
            if part
        )

    def _folder_id(self) -> str:
        configured = self.config.folder.strip("/")
        if not configured:
            raise ProviderConfigurationError(
                "Bitwarden folder must not be empty"
            )

        folders = json.loads(self._run("list", "folders"))
        exact = [
            folder
            for folder in folders
            if str(folder.get("name", "")).strip("/") == configured
        ]

        if not exact:
            raise ProviderConfigurationError(
                f"configured Bitwarden folder was not found: {configured}"
            )

        if len(exact) > 1:
            raise ProviderConfigurationError(
                f"configured Bitwarden folder is ambiguous: {configured}"
            )

        folder_id = str(exact[0].get("id") or "").strip()
        if not folder_id:
            raise ProviderConfigurationError(
                f"configured Bitwarden folder has no ID: {configured}"
            )

        return folder_id

    def _resolve(self, canonical_alias):
        folder_id = self._folder_id()

        configured_name = self.config.items.get(
            canonical_alias,
            canonical_alias,
        )

        results = json.loads(
            self._run(
                "list",
                "items",
                "--search",
                configured_name,
                "--folderid",
                folder_id,
            )
        )

        canonical = self._normalise_name(configured_name)
        exact = []
        partial = []

        for item in results:
            name = self._normalise_name(str(item.get("name", "")))

            if str(item.get("folderId") or "") != folder_id:
                continue

            if name == canonical:
                exact.append(item)
            elif canonical in name or name in canonical:
                partial.append(item)

        candidates = exact or partial

        if not candidates:
            raise SecretNotFoundError(
                f"secret not found for alias {canonical_alias}"
            )

        if len(candidates) > 1:
            raise ProviderConfigurationError(
                f"multiple Bitwarden items matched alias {canonical_alias}"
            )

        item = candidates[0]
        value, ctype = self._extract_value(item, canonical_alias)

        if value is None:
            raise SecretNotFoundError(
                f"secret item has no usable credential for alias "
                f"{canonical_alias}"
            )

        return ResolvedSecret(
            canonical_alias,
            value,
            ctype,
            provider_name=canonical_alias,
            account_name=item.get("name"),
            source_backend="bitwarden",
            validation_method="presence",
            metadata={
                "item_id": item.get("id"),
                "folder_id": folder_id,
            },
        )
