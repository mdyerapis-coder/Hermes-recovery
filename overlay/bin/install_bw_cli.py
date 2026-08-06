#!/usr/bin/env python3
"""Install a pinned Bitwarden CLI release from official signed-release assets."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import tempfile
import urllib.request
import zipfile

DEFAULT_VERSION = "2026.4.2"
RELEASE_BASE = "https://github.com/bitwarden/clients/releases/download/cli-v{version}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-Recovery/1.2"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        if response.geturl().split(":", 1)[0].lower() != "https":
            raise RuntimeError("Bitwarden download redirected away from HTTPS")
        shutil.copyfileobj(response, output)


def parse_checksum(path: Path, archive_name: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        digest = parts[0].lower()
        filename = parts[-1].lstrip("*") if len(parts) > 1 else archive_name
        if filename == archive_name and len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
            return digest
    raise RuntimeError("official Bitwarden checksum file has no usable Linux archive digest")


def install(version: str, install_root: Path, link_path: Path) -> Path:
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        raise RuntimeError(f"Bitwarden CLI pinned installer does not support architecture {machine}")
    if os.geteuid() != 0 and os.environ.get("HERMES_RECOVERY_ALLOW_UNPRIVILEGED_TEST") != "1":
        raise PermissionError("run the Bitwarden CLI installer as root")

    archive_name = f"bw-linux-{version}.zip"
    checksum_name = f"bw-linux-sha256-{version}.txt"
    base = RELEASE_BASE.format(version=version)
    with tempfile.TemporaryDirectory(prefix="hermes-bw-") as temp_name:
        temp = Path(temp_name)
        archive = temp / archive_name
        checksum = temp / checksum_name
        download(f"{base}/{archive_name}", archive)
        download(f"{base}/{checksum_name}", checksum)
        expected = parse_checksum(checksum, archive_name)
        actual = sha256(archive)
        if actual != expected:
            raise RuntimeError(f"Bitwarden CLI checksum mismatch: expected {expected}, got {actual}")

        with zipfile.ZipFile(archive) as zipped:
            candidates = [name for name in zipped.namelist() if Path(name).name == "bw"]
            if len(candidates) != 1:
                raise RuntimeError("Bitwarden archive does not contain exactly one bw executable")
            target_dir = install_root / version
            next_dir = install_root / f".{version}.new.{os.getpid()}"
            shutil.rmtree(next_dir, ignore_errors=True)
            next_dir.mkdir(parents=True, mode=0o755)
            target = next_dir / "bw"
            with zipped.open(candidates[0]) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755)
            shutil.rmtree(target_dir, ignore_errors=True)
            next_dir.replace(target_dir)

    link_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_link = link_path.with_name(f".{link_path.name}.new.{os.getpid()}")
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(target_dir / "bw")
    temporary_link.replace(link_path)
    return target_dir / "bw"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--install-root", default="/opt/hermes-tools/bitwarden")
    parser.add_argument("--link", default="/usr/local/bin/bw")
    args = parser.parse_args()
    installed = install(args.version, Path(args.install_root), Path(args.link))
    print(f"Installed verified Bitwarden CLI {args.version} at {installed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
