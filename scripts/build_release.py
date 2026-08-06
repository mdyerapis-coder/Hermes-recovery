#!/usr/bin/env python3
"""Build a verified Hermes recovery release from the prior release plus overlay."""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile


BOOTSTRAP_OLD = 'exec python3 "$CONTROLLER" --kit-root "$SCRIPT_DIR" "$@"'
BOOTSTRAP_NEW = 'exec python3 "$SCRIPT_DIR/bin/hermes_rebuild_hsp.py" --kit-root "$SCRIPT_DIR" --controller "$CONTROLLER" -- "$@"'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_archive_hash(repo_root: Path, archive_name: str) -> str:
    for line in (repo_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == archive_name:
            return parts[0].lower()
    raise RuntimeError(f"SHA256SUMS has no entry for {archive_name}")


def numeric_part_key(path: Path) -> int:
    match = re.search(r"\.part-(\d+)$", path.name)
    if not match:
        raise RuntimeError(f"invalid payload part name: {path}")
    return int(match.group(1))


def reconstruct_archive(repo_root: Path, version: str, output: Path) -> None:
    pattern = f"hermes-rebuild-kit-v{version}.tar.gz.b64.part-*"
    parts = sorted((repo_root / "payload").glob(pattern), key=numeric_part_key)
    if not parts:
        raise RuntimeError(f"no payload parts matched {pattern}")
    encoded = b"".join(part.read_bytes() for part in parts)
    try:
        output.write_bytes(base64.b64decode(encoded, validate=False))
    except ValueError as exc:
        raise RuntimeError("base64 payload could not be decoded") from exc
    archive_name = f"hermes-rebuild-kit-v{version}.tar.gz"
    expected = expected_archive_hash(repo_root, archive_name)
    actual = sha256(output)
    if actual != expected:
        raise RuntimeError(f"source archive checksum mismatch: expected {expected}, got {actual}")


def safe_extract(archive: Path, destination: Path) -> None:
    destination_real = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if target != destination_real and destination_real not in target.parents:
                raise RuntimeError(f"unsafe archive member: {member.name}")
        tar.extractall(destination)


def apply_overlay(source: Path, overlay: Path) -> None:
    for path in sorted(overlay.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(overlay)
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def apply_fedora_hotfixes(controller: Path) -> None:
    text = controller.read_text(encoding="utf-8")
    dnf4 = 'ctx.runner.run(["dnf", "config-manager", "--add-repo", repo_url])'
    dnf5 = 'ctx.runner.run(["dnf", "config-manager", "addrepo", f"--from-repofile={repo_url}"])'
    if dnf4 in text:
        text = text.replace(dnf4, dnf5, 1)
    elif dnf5 not in text:
        raise RuntimeError("DNF5 hotfix target not found")

    npm_old = '''    prefix = pathlib.Path("/opt/hermes-tools/npm")
    uid, gid = user_ids(ctx.runtime_user)
    prefix.mkdir(parents=True, exist_ok=True)
    os.chown(prefix, uid, gid)
'''
    npm_new = '''    prefix = pathlib.Path("/opt/hermes-tools/npm")
    uid, gid = user_ids(ctx.runtime_user)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(prefix.parent, 0o755)
    prefix.mkdir(parents=True, exist_ok=True)
    os.chown(prefix, uid, gid)
    os.chmod(prefix, 0o755)
'''
    if npm_old in text:
        text = text.replace(npm_old, npm_new, 1)
    elif npm_new not in text:
        raise RuntimeError("npm permissions hotfix target not found")
    controller.write_text(text, encoding="utf-8")


def patch_bootstrap(source: Path) -> None:
    bootstrap = source / "bootstrap.sh"
    text = bootstrap.read_text(encoding="utf-8")
    if BOOTSTRAP_OLD in text:
        text = text.replace(BOOTSTRAP_OLD, BOOTSTRAP_NEW, 1)
    elif BOOTSTRAP_NEW not in text:
        raise RuntimeError("bootstrap exec target not found")
    bootstrap.write_text(text, encoding="utf-8")


def write_manifest(source: Path) -> None:
    manifest = source / "MANIFEST.sha256"
    lines: list[str] = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and path != manifest:
            lines.append(f"{sha256(path)}  {path.relative_to(source).as_posix()}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def deterministic_tar(source: Path, archive: Path) -> None:
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as tar:
                for path in [source, *sorted(source.rglob("*"))]:
                    arcname = Path("hermes-rebuild-kit") / path.relative_to(source)
                    info = tar.gettarinfo(str(path), arcname.as_posix())
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = 0
                    if path.is_file():
                        with path.open("rb") as handle:
                            tar.addfile(info, handle)
                    else:
                        tar.addfile(info)


def split_payload(archive: Path, payload_dir: Path, *, chunk_size: int = 60000) -> list[Path]:
    encoded = base64.b64encode(archive.read_bytes()).decode("ascii")
    payload_dir.mkdir(parents=True, exist_ok=True)
    stem = archive.name + ".b64.part-"
    parts: list[Path] = []
    for index, offset in enumerate(range(0, len(encoded), chunk_size)):
        part = payload_dir / f"{stem}{index:03d}"
        part.write_text(encoded[offset : offset + chunk_size] + "\n", encoding="ascii")
        parts.append(part)
    (payload_dir / "parts.txt").write_text(
        "".join(f"payload/{part.name}\n" for part in parts),
        encoding="utf-8",
    )
    return parts


def build(args: argparse.Namespace) -> None:
    repo_root = Path(args.repo_root).resolve()
    overlay = Path(args.overlay).resolve()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hermes-release-") as temp_name:
        temp = Path(temp_name)
        archive = temp / f"hermes-rebuild-kit-v{args.source_version}.tar.gz"
        if args.source_dir:
            extracted_source = Path(args.source_dir).resolve()
            source = temp / "hermes-rebuild-kit"
            shutil.copytree(extracted_source, source)
        else:
            reconstruct_archive(repo_root, args.source_version, archive)
            safe_extract(archive, temp)
            source = temp / "hermes-rebuild-kit"

        apply_fedora_hotfixes(source / "bin" / "hermes_rebuild.py")
        apply_overlay(source, overlay)
        patch_bootstrap(source)
        (source / "VERSION").write_text(args.version + "\n", encoding="utf-8")
        os.chmod(source / "bin" / "hermes_rebuild_hsp.py", 0o755)
        write_manifest(source)

        release_archive = output / f"hermes-rebuild-kit-v{args.version}.tar.gz"
        deterministic_tar(source, release_archive)
        (output / "SHA256SUMS").write_text(
            f"{sha256(release_archive)}  {release_archive.name}\n",
            encoding="utf-8",
        )
        (output / "VERSION").write_text(args.version + "\n", encoding="utf-8")
        split_payload(release_archive, output / "payload")

        verify_dir = temp / "verify"
        verify_dir.mkdir()
        safe_extract(release_archive, verify_dir)
        verify_source = verify_dir / "hermes-rebuild-kit"
        for line in (verify_source / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
            digest, relative = line.split(maxsplit=1)
            relative = relative.lstrip("* ")
            if sha256(verify_source / relative) != digest:
                raise RuntimeError(f"internal manifest verification failed: {relative}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--overlay", default="overlay")
    parser.add_argument("--source-version", default="1.1.0")
    parser.add_argument("--source-dir")
    parser.add_argument("--version", default="1.2.0")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    build(parse_args())
