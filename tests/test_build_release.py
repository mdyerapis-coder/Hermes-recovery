from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BuildReleaseTests(unittest.TestCase):
    def test_builds_verified_overlay_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source = temp / "source"
            (source / "bin").mkdir(parents=True)
            (source / "VERSION").write_text("1.1.0\n")
            (source / "bootstrap.sh").write_text(
                '#!/usr/bin/env bash\nSCRIPT_DIR="$(pwd)"\nCONTROLLER="$SCRIPT_DIR/bin/hermes_rebuild.py"\n'
                'exec python3 "$CONTROLLER" --kit-root "$SCRIPT_DIR" "$@"\n'
            )
            (source / "bin" / "hermes_rebuild.py").write_text(
                'import os, pathlib\n'
                'def f(ctx, repo_url):\n'
                '    ctx.runner.run(["dnf", "config-manager", "--add-repo", repo_url])\n'
                '    prefix = pathlib.Path("/opt/hermes-tools/npm")\n'
                '    uid, gid = user_ids(ctx.runtime_user)\n'
                '    prefix.mkdir(parents=True, exist_ok=True)\n'
                '    os.chown(prefix, uid, gid)\n'
            )
            output = temp / "out"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_release.py"),
                    "--repo-root",
                    str(temp),
                    "--source-dir",
                    str(source),
                    "--overlay",
                    str(ROOT / "overlay"),
                    "--version",
                    "1.2.0",
                    "--output-dir",
                    str(output),
                ],
                check=True,
            )
            archive = output / "hermes-rebuild-kit-v1.2.0.tar.gz"
            self.assertTrue(archive.is_file())
            expected = (output / "SHA256SUMS").read_text().split()[0]
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), expected)

            part_names = [line.split("/", 1)[1] for line in (output / "payload" / "parts.txt").read_text().splitlines()]
            reconstructed = base64.b64decode(b"".join((output / "payload" / name).read_bytes() for name in part_names))
            self.assertEqual(reconstructed, archive.read_bytes())

            extract = temp / "extract"
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(extract)
            kit = extract / "hermes-rebuild-kit"
            self.assertEqual((kit / "VERSION").read_text().strip(), "1.2.0")
            self.assertTrue((kit / "lib" / "hermes_hsp" / "bitwarden.py").is_file())
            self.assertIn("hermes_rebuild_hsp.py", (kit / "bootstrap.sh").read_text())


if __name__ == "__main__":
    unittest.main()
