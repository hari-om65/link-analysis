import pytest

from app.exceptions import BlockedURLError, InvalidURLError, LLMError
from app.services.llm_service import parse_json
from app.utils.validators import ensure_public_url, normalize_url, same_site


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "https://example.com/"),
        ("  https://Example.com  ", "https://example.com/"),
        ("http://example.com/about?x=1#team", "http://example.com/about?x=1"),
        ("https://example.com:443/", "https://example.com:443/"),
    ],
)
def test_normalize_valid_urls(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not a url", "ftp://example.com", "javascript:alert(1)", "https://user:pass@example.com", "https://intranet"],
)
def test_invalid_urls(raw):
    with pytest.raises(InvalidURLError):
        normalize_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1",
        "http://10.1.2.3",
        "http://172.16.0.1",
        "http://192.168.0.1",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0",
        "http://100.64.0.1",
        "http://localhost",
        "http://printer.local",
        "http://metadata.google.internal",
        "https://example.com:22",
    ],
)
def test_internal_targets_are_blocked(raw):
    with pytest.raises(BlockedURLError):
        normalize_url(raw)


async def test_dns_resolving_to_private_ip_is_blocked():
    with pytest.raises(BlockedURLError):
        await ensure_public_url("https://rebind.example")


async def test_public_host_passes():
    assert await ensure_public_url("greennest.example") == "https://greennest.example/"


def test_same_site_ignores_www():
    assert same_site("https://www.example.com/a", "https://example.com/b")
    assert not same_site("https://example.com", "https://example.org")


def test_parse_json_handles_code_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": 2} hope this helps') == {"a": 2}
    assert parse_json('[{"a": 3}]') == {"a": 3}
    with pytest.raises(LLMError):
        parse_json("no json here")
