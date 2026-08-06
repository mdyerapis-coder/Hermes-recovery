#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python3 -m compileall -q "$ROOT/overlay/lib" "$ROOT/overlay/bin" "$ROOT/scripts"
PYTHONPATH="$ROOT/overlay/lib" python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
