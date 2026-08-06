#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python3 -m py_compile "$ROOT/bin/hermes_rebuild.py" "$ROOT/tests/smoke_test.py" "$ROOT/tests/e2e_test.py"
bash -n "$ROOT/install.sh"
bash -n "$ROOT/bootstrap.sh"
bash -n "$ROOT/bin/hermes-rebuildctl"
python3 "$ROOT/tests/smoke_test.py"
python3 "$ROOT/tests/e2e_test.py"
echo "Static and end-to-end validation: PASS"
