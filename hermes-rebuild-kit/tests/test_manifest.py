import pytest
from pathlib import Path
from providers.manifest import load_manifest
from providers.exceptions import ManifestValidationError

def write(tmp_path, text):
    p=tmp_path/'m.yaml'; p.write_text(text); return p

def test_valid_opencode_manifest():
    m=load_manifest(Path(__file__).parents[1]/'manifests'/'opencode.yaml')
    assert m.project == 'opencode'; assert any(x.alias=='opencode_go' for x in m.optional)
def test_unknown_top_level(tmp_path):
    with pytest.raises(ManifestValidationError): load_manifest(write(tmp_path, 'project: x\nbad: true\n'))
def test_duplicate_alias(tmp_path):
    text='''project: x\nsecrets:\n  required:\n    - {alias: openai, purpose: primary_llm}\n  optional:\n    - {alias: openai_api_key, purpose: fallback_llm}\n'''
    with pytest.raises(ManifestValidationError): load_manifest(write(tmp_path,text))
def test_malformed(tmp_path):
    with pytest.raises(ManifestValidationError): load_manifest(write(tmp_path, 'project: [\n'))
