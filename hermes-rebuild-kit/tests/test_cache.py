import time, pytest
from providers.cache import SessionCache

def test_insert_lookup_empty_and_clear():
    c=SessionCache(); c.set("x", ""); assert c.contains("x"); assert c.get("x") == ""; c.clear(); assert not c.contains("x")
def test_expiry():
    c=SessionCache(); c.set("x", "secret", ttl_seconds=.01); time.sleep(.02)
    with pytest.raises(KeyError): c.get("x")
def test_repr_redacts_values():
    c=SessionCache(); c.set("x", "TOPSECRET"); assert "TOPSECRET" not in repr(c)
