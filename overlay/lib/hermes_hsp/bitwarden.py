"""Bitwarden CLI backend for Hermes Secret Provider."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any

from .aliases import choose_unique, extract_secret, normalise
from .cache import SecretCache
from .errors import AuthenticationRequired, BackendUnavailable, HSPError, SecretNotFound
from .manifest import ProviderConfig
from .models import Resolution, SecretSpec
from .validators import validate_value


class BitwardenProvider:
    def __init__(self, config: ProviderConfig, *, environ: dict[str, str] | None = None) -> None:
        self.config = config
        self.environ = dict(os.environ if environ is None else environ)
        self.cache = SecretCache()
        self.bw = shutil.which("bw", path=self.environ.get("PATH"))
        self._session: str | None = self.environ.get("BW_SESSION")
        self._unlocked_here = False
        self._items: list[dict[str, Any]] | None = None

    def _command_env(self) -> dict[str, str]:
        env = dict(self.environ)
        if self._session:
            env["BW_SESSION"] = self._session
        return env

    def _run(
        self,
        args: list[str],
        *,
        capture: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if not self.bw:
            raise BackendUnavailable("Bitwarden CLI 'bw' is not installed")
        return subprocess.run(
            [self.bw, *args],
            env=self._command_env(),
            stdin=None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            check=check,
        )

    def _json(self, args: list[str]) -> Any:
        result = self._run(args)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise HSPError(f"Bitwarden returned invalid JSON for {' '.join(args)}") from exc

    def _status(self) -> str:
        data = self._json(["status", "--raw"])
        status = str(data.get("status") or "") if isinstance(data, dict) else ""
        if status not in {"unauthenticated", "locked", "unlocked"}:
            raise HSPError(f"unexpected Bitwarden status: {status or '<empty>'}")
        return status

    def ensure_session(self) -> None:
        if not self.bw:
            raise BackendUnavailable("Bitwarden CLI 'bw' is not installed")
        status = self._status()
        interactive = bool(sys.stdin.isatty() and sys.stderr.isatty())

        if status == "unauthenticated":
            if not (interactive and self.config.interactive_login):
                raise AuthenticationRequired("run 'bw login' before unattended recovery")
            self._run(["login"], capture=False)
            status = self._status()

        if status == "locked" or (status == "unlocked" and not self._session):
            if not (interactive and self.config.interactive_login):
                raise AuthenticationRequired("set BW_SESSION or unlock Bitwarden before unattended recovery")
            result = self._run(["unlock", "--raw"])
            session = result.stdout.strip()
            if not session or any(ch.isspace() for ch in session):
                raise AuthenticationRequired("Bitwarden did not return a usable session token")
            self._session = session
            self._unlocked_here = True

        if self.config.sync:
            self._run(["sync"], capture=True)

    def _folder_selected(self, folder_name: str) -> bool:
        candidate = normalise(folder_name)
        for root in self.config.scope.roots:
            exact = normalise(root)
            if candidate == exact:
                return True
            if self.config.scope.include_descendants:
                lowered = folder_name.casefold()
                root_lower = root.casefold().rstrip("/\\: ")
                if lowered.startswith(root_lower + "/") or lowered.startswith(root_lower + "\\") or lowered.startswith(root_lower + ":"):
                    return True
        return False

    def list_scoped_items(self) -> list[dict[str, Any]]:
        if self._items is not None:
            return self._items
        self.ensure_session()
        folders = self._json(["list", "folders", "--raw"])
        if not isinstance(folders, list):
            raise HSPError("Bitwarden folder listing was not a list")
        selected = [folder for folder in folders if self._folder_selected(str(folder.get("name") or ""))]
        if not selected:
            roots = ", ".join(self.config.scope.roots)
            raise SecretNotFound(f"Bitwarden folder not found: {roots}")

        items: dict[str, dict[str, Any]] = {}
        for folder in selected:
            folder_id = str(folder.get("id") or "")
            if not folder_id:
                continue
            listed = self._json(["list", "items", "--folderid", folder_id, "--raw"])
            if not isinstance(listed, list):
                raise HSPError(f"Bitwarden item listing for {folder.get('name')} was not a list")
            for item in listed:
                item_id = str(item.get("id") or "")
                if item_id:
                    items[item_id] = item
        self._items = list(items.values())
        return self._items

    def resolve(self, spec: SecretSpec) -> Resolution:
        cached = self.cache.get(spec.name)
        if cached is not None:
            return Resolution(spec.name, spec.env, "cache", "session cache", "cache", cached)

        match = choose_unique(self.list_scoped_items(), spec)
        value, source_field = extract_secret(match.item, spec)
        validate_value(spec.name, value, spec.validator)
        self.cache.set(spec.name, value)
        return Resolution(
            name=spec.name,
            env=spec.env,
            item_id=str(match.item.get("id") or ""),
            item_name=str(match.item.get("name") or ""),
            source_field=source_field,
            value=value,
        )

    def close(self) -> None:
        self.cache.clear()
        if self.config.lock_on_exit and self._unlocked_here and self.bw:
            try:
                self._run(["lock"], capture=True, check=False)
            finally:
                self._session = None
        self._items = None
