#!/usr/bin/env bash
# All-in-One Hermes Rebuild Bootstrap
# Thin, auditable entrypoint. The Python controller performs the actual work.
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CONTROLLER="$SCRIPT_DIR/bin/hermes_rebuild.py"

usage() {
  cat <<'USAGE'
Usage:
  sudo ./bootstrap.sh                         # interactive recovery
  sudo ./bootstrap.sh --config FILE --non-interactive  # JSON/YAML unattended recovery
  sudo ./bootstrap.sh --config FILE --plan    # render plan only
  sudo ./bootstrap.sh --health                # run health checks only
  sudo ./bootstrap.sh --summary               # print state summary
  sudo ./bootstrap.sh --rollback latest       # restore latest rollback point

Important:
  * Run on a systemd-based Linux host.
  * OAuth logins require a real interactive terminal and cannot be embedded.
  * Config files should reference secrets as env:NAME or file:/secure/path.
USAGE
}

if [[ ${EUID:-$(id -u)} -ne 0 && "${HERMES_RECOVERY_ALLOW_UNPRIVILEGED_TEST:-0}" != "1" ]]; then
  echo "ERROR: run as root (for example: sudo ./bootstrap.sh)" >&2
  exit 77
fi

if [[ ! -f "$CONTROLLER" ]]; then
  echo "ERROR: controller not found: $CONTROLLER" >&2
  exit 66
fi

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

# Bootstrap only the parser/runtime dependencies. Distribution-specific platform
# packages and all application components are managed by the controller.
install_bootstrap_dependencies() {
  if command -v python3 >/dev/null 2>&1 && python3 - <<'PY' >/dev/null 2>&1
import yaml
PY
  then
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends python3 python3-yaml ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pyyaml ca-certificates
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pyyaml ca-certificates
  else
    echo "ERROR: unsupported package manager; install Python 3 and PyYAML first" >&2
    exit 69
  fi
}

install_bootstrap_dependencies
exec python3 "$CONTROLLER" --kit-root "$SCRIPT_DIR" "$@"
