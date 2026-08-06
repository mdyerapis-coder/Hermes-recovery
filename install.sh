#!/usr/bin/env bash
# Mobile-first loader for the complete Hermes recovery kit.
# Downloads a versioned tarball, verifies its published SHA-256 and the
# archive's internal file manifest, installs it under /opt, then invokes the
# real bootstrap. No secrets are accepted or written by this loader.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

REPO="${HERMES_RECOVERY_REPO:-mdyerapis-coder/Hermes-recovery}"
REF="${HERMES_RECOVERY_REF:-main}"
KIT_VERSION="${HERMES_RECOVERY_VERSION:-1.2.0}"
INSTALL_BASE="${HERMES_RECOVERY_INSTALL_BASE:-/opt/hermes-recovery-kit}"
ARCHIVE_NAME="hermes-rebuild-kit-v${KIT_VERSION}.tar.gz"
BASE_URL="${HERMES_RECOVERY_BASE_URL:-https://raw.githubusercontent.com/${REPO}/${REF}}"
ARCHIVE_URL="${HERMES_RECOVERY_ARCHIVE_URL:-${BASE_URL}/${ARCHIVE_NAME}}"
CHECKSUM_URL="${HERMES_RECOVERY_CHECKSUM_URL:-${BASE_URL}/SHA256SUMS}"

usage() {
  cat <<'USAGE'
Usage:
  curl -fsSLO https://raw.githubusercontent.com/mdyerapis-coder/Hermes-recovery/main/install.sh
  chmod +x install.sh
  sudo ./install.sh

Unattended recovery:
  sudo ./install.sh --config /root/hermes-recovery.yaml --non-interactive

Environment overrides:
  HERMES_RECOVERY_REPO=owner/repository
  HERMES_RECOVERY_REF=branch|tag|commit
  HERMES_RECOVERY_VERSION=1.2.0
  HERMES_RECOVERY_INSTALL_BASE=/opt/hermes-recovery-kit
  HERMES_RECOVERY_BASE_URL=https://mirror/path
  HERMES_RECOVERY_ARCHIVE_URL=https://mirror/path/kit.tar.gz
  HERMES_RECOVERY_CHECKSUM_URL=https://mirror/path/SHA256SUMS
  HERMES_RECOVERY_EXPECTED_SHA256=<64-hex-digest>

The loader never accepts secrets itself. Supply secrets to the verified
bootstrap through interactive prompts, protected environment variables, or a
root-only JSON/YAML configuration file using env:/file: references.
USAGE
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

if [[ ${EUID:-$(id -u)} -ne 0 && "${HERMES_RECOVERY_ALLOW_UNPRIVILEGED_TEST:-0}" != "1" ]]; then
  echo "ERROR: run with sudo so the verified kit can be installed under /opt" >&2
  exit 77
fi

for command in curl tar sha256sum base64 python3; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command is missing: $command" >&2
    exit 69
  }
done

[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
  echo "ERROR: invalid HERMES_RECOVERY_REPO: $REPO" >&2
  exit 64
}
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || {
  echo "ERROR: invalid HERMES_RECOVERY_REF" >&2
  exit 64
}
[[ "$KIT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "ERROR: invalid HERMES_RECOVERY_VERSION" >&2
  exit 64
}

work="$(mktemp -d -t hermes-recovery-loader.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT
archive="$work/$ARCHIVE_NAME"
checksum_file="$work/SHA256SUMS"

curl_security=(--proto '=https' --tlsv1.2)
if [[ "${HERMES_RECOVERY_ALLOW_INSECURE_TEST_URL:-0}" == "1" ]]; then
  curl_security=()
fi
curl_common=("${curl_security[@]}" --fail --location --silent --show-error --retry 4 --retry-all-errors --connect-timeout 20)

echo "Downloading verified Hermes recovery kit v${KIT_VERSION} ..."
if [[ -n "${HERMES_RECOVERY_ARCHIVE_URL:-}" ]]; then
  curl "${curl_common[@]}" "$ARCHIVE_URL" -o "$archive"
elif curl "${curl_common[@]}" "$ARCHIVE_URL" -o "$archive"; then
  : # Preferred path: download the versioned archive directly from the repository.
else
  echo "Direct archive download unavailable; reconstructing verified payload parts ..." >&2
  parts_file="$work/parts.txt"
  encoded="$work/${ARCHIVE_NAME}.b64"
  rm -f -- "$archive"
  curl "${curl_common[@]}" "${BASE_URL}/payload/parts.txt" -o "$parts_file"
  : > "$encoded"
  while IFS= read -r part || [[ -n "$part" ]]; do
    [[ -z "$part" || "$part" == \#* ]] && continue
    [[ "$part" =~ ^payload/${ARCHIVE_NAME}\.b64\.part-[0-9]{3}$ ]] || {
      echo "ERROR: invalid payload part name: $part" >&2
      exit 65
    }
    curl "${curl_common[@]}" "${BASE_URL}/${part}" >> "$encoded"
  done < "$parts_file"
  [[ -s "$encoded" ]] || {
    echo "ERROR: no recovery payload parts were downloaded" >&2
    exit 65
  }
  base64 --decode "$encoded" > "$archive"
fi

expected="${HERMES_RECOVERY_EXPECTED_SHA256:-}"
if [[ -n "$expected" ]]; then
  [[ "$expected" =~ ^[A-Fa-f0-9]{64}$ ]] || {
    echo "ERROR: HERMES_RECOVERY_EXPECTED_SHA256 is not a SHA-256 digest" >&2
    exit 64
  }
  printf '%s  %s\n' "${expected,,}" "$ARCHIVE_NAME" > "$checksum_file"
else
  curl "${curl_common[@]}" "$CHECKSUM_URL" -o "$checksum_file"
fi

(
  cd "$work"
  # The published checksum file may cover multiple release files. Require an
  # exact entry for the selected archive and verify only that entry.
  line="$(awk -v name="$ARCHIVE_NAME" '$2 == name || $2 == "*" name {print; exit}' SHA256SUMS)"
  [[ -n "$line" ]] || {
    echo "ERROR: SHA256SUMS has no entry for $ARCHIVE_NAME" >&2
    exit 65
  }
  printf '%s\n' "$line" | sha256sum --check --strict -
)

tar --extract --gzip --file "$archive" --directory "$work" --no-same-owner
source_dir="$work/hermes-rebuild-kit"
[[ -d "$source_dir" && -f "$source_dir/MANIFEST.sha256" ]] || {
  echo "ERROR: verified archive does not contain hermes-rebuild-kit/MANIFEST.sha256" >&2
  exit 65
}
(
  cd "$source_dir"
  sha256sum --check --strict MANIFEST.sha256
)

embedded_version="$(tr -d '[:space:]' < "$source_dir/VERSION")"
[[ "$embedded_version" == "$KIT_VERSION" ]] || {
  echo "ERROR: archive version $embedded_version does not match requested $KIT_VERSION" >&2
  exit 65
}

# Emergency Fedora 41+ compatibility hotfixes for recovery kit 1.1.0.
# The signed archive and internal manifest are verified before these exact,
# auditable source transformations are applied.
if [[ "$embedded_version" == "1.1.0" ]]; then
  python3 - "$source_dir/bin/hermes_rebuild.py" <<'PYHOTFIX'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

dnf4 = 'ctx.runner.run(["dnf", "config-manager", "--add-repo", repo_url])'
dnf5 = 'ctx.runner.run(["dnf", "config-manager", "addrepo", f"--from-repofile={repo_url}"])'
if dnf4 in text:
    text = text.replace(dnf4, dnf5, 1)
elif dnf5 not in text:
    raise SystemExit("ERROR: DNF5 hotfix target was not found; refusing to continue")

npm_old = """    prefix = pathlib.Path("/opt/hermes-tools/npm")
    uid, gid = user_ids(ctx.runtime_user)
    prefix.mkdir(parents=True, exist_ok=True)
    os.chown(prefix, uid, gid)
"""
npm_new = """    prefix = pathlib.Path("/opt/hermes-tools/npm")
    uid, gid = user_ids(ctx.runtime_user)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(prefix.parent, 0o755)
    prefix.mkdir(parents=True, exist_ok=True)
    os.chown(prefix, uid, gid)
    os.chmod(prefix, 0o755)
"""
if npm_old in text:
    text = text.replace(npm_old, npm_new, 1)
elif npm_new not in text:
    raise SystemExit("ERROR: npm permission hotfix target was not found; refusing to continue")

path.write_text(text, encoding="utf-8")
PYHOTFIX
  echo "Applied verified Fedora DNF5 and npm permissions hotfixes."
fi

release_dir="$INSTALL_BASE/releases/$KIT_VERSION"
mkdir -p "$INSTALL_BASE/releases"
next="$INSTALL_BASE/releases/.${KIT_VERSION}.new.$$"
rm -rf -- "$next"
mkdir -p "$next"
cp -a "$source_dir"/. "$next"/
chmod 0755 "$next/install.sh" "$next/bootstrap.sh" "$next/bin/hermes-rebuildctl" "$next/tests/run.sh"
chmod 0755 "$next/bin/hermes_rebuild.py"
rm -rf -- "$release_dir"
mv "$next" "$release_dir"
ln -sfn "releases/$KIT_VERSION" "$INSTALL_BASE/current"

printf 'Verified Hermes recovery kit %s installed at %s\n' "$KIT_VERSION" "$release_dir"
exec "$INSTALL_BASE/current/bootstrap.sh" "$@"
