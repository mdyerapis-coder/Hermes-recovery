#!/usr/bin/env python3
"""Offline smoke tests for the Hermes rebuild controller."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin/hermes_rebuild.py"
spec = importlib.util.spec_from_file_location("hermes_rebuild", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["hermes_rebuild"] = module
spec.loader.exec_module(module)

assert module.deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}

try:
    module.resolve_secret_spec("literal-secret")
except ValueError:
    pass
else:
    raise AssertionError("literal secret was accepted")

os.environ["TEST_HERMES_SECRET"] = "abc123"
assert module.resolve_secret_spec("env:TEST_HERMES_SECRET") == "abc123"

config = module.default_config()
module.apply_version_policy(config, {"memory_class": "minimal"})
assert config["versions"]["python"] == "3.13"
assert config["versions"]["node_major"] == 22

with tempfile.TemporaryDirectory() as temp:
    state = module.State(pathlib.Path(temp), module.Log(False))
    state.component("test", "ok", "fine", api_key="must-not-persist")
    data = json.loads((pathlib.Path(temp) / "state.json").read_text())
    assert data["components"]["test"]["metadata"]["api_key"] == "<redacted>"

compile(module.AGENT_FORGE_ADAPTER, "<agent-forge-adapter>", "exec")
print("Hermes rebuild smoke tests: PASS")
