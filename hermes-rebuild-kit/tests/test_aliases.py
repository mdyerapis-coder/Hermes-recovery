import pytest
from providers.aliases import AliasResolver, normalize_alias
from providers.exceptions import AmbiguousAliasError, AliasResolutionError

def test_normalisation(): assert normalize_alias(" OpenAI-API Key ") == "openai_api_key"
def test_canonical_resolution(): assert AliasResolver().resolve("OpenAI API Key") == "openai"
def test_opencode_variants():
    r=AliasResolver(); assert r.resolve("opencode") == "opencode"; assert r.resolve("zen-key") == "opencode_zen"; assert r.resolve("go-key") == "opencode_go"
def test_ambiguous_go():
    with pytest.raises(AmbiguousAliasError): AliasResolver().resolve("go")
def test_scoped_go(): assert AliasResolver(project_overrides={"go":"opencode_go"}).resolve("go") == "opencode_go"
def test_unknown():
    with pytest.raises(AliasResolutionError): AliasResolver().resolve("missing")
