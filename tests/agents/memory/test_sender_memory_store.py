from app.agents.memory.sender_memory_store import _sender_key


def test_sender_key_uses_domain():
    assert _sender_key("alice@example.com") == "example.com"


def test_sender_key_falls_back_without_at():
    assert _sender_key("not-an-email") == "not-an-email"


def test_sender_key_is_case_insensitive():
    assert _sender_key("Bob@Example.COM") == "example.com"
