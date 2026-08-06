#!/usr/bin/env python3
"""Install an auditable HSP candidate beside the current recovery release."""
from __future__ import annotations

import argparse
import compileall
import os
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_release import apply_fedora_hotfixes, apply_overlay, patch_bootstrap, write_manifest


def install(repo_root: Path, install_base: Path, version: str) -> tuple[Path, Path]:
    if os.geteuid() != 0 and os.environ.get("HERMES_RECOVERY_ALLOW_UNPRIVILEGED_TEST") != "1":
        raise PermissionError("run the local candidate installer as root")
    current = install_base / "current"
    if not current.exists():
        raise FileNotFoundError(f"current recovery kit not found: {current}")
    previous = current.resolve()
    releases = install_base / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / version
    staging = releases / f".{version}.new.{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(previous, staging, symlinks=True)

    apply_fedora_hotfixes(staging / "bin" / "hermes_rebuild.py")
    apply_overlay(staging, repo_root / "overlay")
    patch_bootstrap(staging)
    (staging / "VERSION").write_text(version + "\n", encoding="utf-8")
    os.chmod(staging / "bin" / "hermes_rebuild_hsp.py", 0o755)
    os.chmod(staging / "bin" / "install_bw_cli.py", 0o755)
    if not compileall.compile_dir(staging / "lib", quiet=1):
        raise RuntimeError("HSP library compilation failed")
    if not compileall.compile_file(staging / "bin" / "hermes_rebuild_hsp.py", quiet=1):
        raise RuntimeError("HSP wrapper compilation failed")
    if not compileall.compile_file(staging / "bin" / "install_bw_cli.py", quiet=1):
        raise RuntimeError("Bitwarden installer compilation failed")
    write_manifest(staging)

    if release.exists():
        archived = releases / f".{version}.previous.{int(time.time())}"
        release.replace(archived)
    staging.replace(release)

    temporary_link = install_base / f".current.new.{os.getpid()}"
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(Path("releases") / version)
    temporary_link.replace(current)
    return previous, release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--install-base", default="/opt/hermes-recovery-kit")
    parser.add_argument("--version", default="1.2.0-candidate")
    args = parser.parse_args()
    previous, release = install(Path(args.repo_root).resolve(), Path(args.install_base).resolve(), args.version)
    print(f"Installed Hermes Recovery candidate at {release}")
    print(f"Previous release remains at {previous}")
    print(f"Rollback: ln -sfn {previous} {Path(args.install_base).resolve() / 'current'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
