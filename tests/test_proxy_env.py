import httpx

from moviebox_api.proxy_env import sanitize_no_proxy_env, sanitize_no_proxy_value


def test_bracketed_ipv6_entries_are_unbracketed():
    assert sanitize_no_proxy_value("localhost,[::1],127.0.0.1") == "localhost,::1,127.0.0.1"
    assert sanitize_no_proxy_value("[fd8b:4f84:7d32:99::1]") == "fd8b:4f84:7d32:99::1"
    assert sanitize_no_proxy_value("localhost,127.0.0.1") == "localhost,127.0.0.1"


def test_sanitize_env_allows_httpx_client_creation(monkeypatch):
    # This is the exact environment shape that used to crash httpx with
    # InvalidURL("Invalid port: ':1]'") at AsyncClient creation time.
    monkeypatch.setenv(
        "NO_PROXY",
        "localhost,127.0.0.1,::1,[::1],fd8b:4f84:7d32:99::1,[fd8b:4f84:7d32:99::1]",
    )
    monkeypatch.setenv(
        "no_proxy",
        "localhost,127.0.0.1,::1,[::1],fd8b:4f84:7d32:99::1,[fd8b:4f84:7d32:99::1]",
    )
    assert sanitize_no_proxy_env() is True
    client = httpx.AsyncClient()
    assert client is not None
