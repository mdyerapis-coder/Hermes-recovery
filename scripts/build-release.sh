#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
kit="$root/hermes-rebuild-kit"
version="$(tr -d '[:space:]' < "$kit/VERSION")"
[[ "$version" == "1.2.0" ]] || { echo "unexpected VERSION: $version" >&2; exit 1; }
(
 cd "$kit"
 find . -type f ! -name MANIFEST.sha256 ! -path './.pytest_cache/*' ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256
)
tar -C "$root" -czf "$root/hermes-rebuild-kit-v${version}.tar.gz" hermes-rebuild-kit
sha256sum "$root/hermes-rebuild-kit-v${version}.tar.gz" > "$root/SHA256SUMS.v${version}"
