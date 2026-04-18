"""Tests for the thread-safe client cache in :mod:`kibana_mcp._mcp`."""

from __future__ import annotations

import threading

import pytest

import kibana_mcp._mcp as _mcp_module
from kibana_mcp._mcp import get_client
from kibana_mcp.client import KibanaClient


@pytest.fixture(autouse=True)
def reset_client(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[return]
    """Reset the module-level _client to None before each test."""
    monkeypatch.setattr(_mcp_module, "_client", None)
    yield
    # Cleanup: close and reset after test
    if _mcp_module._client is not None:
        try:
            _mcp_module._client.close()
        except Exception:
            pass
        _mcp_module._client = None


@pytest.fixture()
def kibana_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide minimal env vars to build a KibanaClient."""
    monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
    monkeypatch.delenv("KIBANA_API_KEY", raising=False)
    monkeypatch.delenv("KIBANA_USERNAME", raising=False)
    monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)


def test_get_client_returns_kibana_client(kibana_env: None) -> None:
    client = get_client()
    assert isinstance(client, KibanaClient)


def test_get_client_returns_same_instance(kibana_env: None) -> None:
    c1 = get_client()
    c2 = get_client()
    assert c1 is c2


def test_get_client_thread_safe(kibana_env: None) -> None:
    """Concurrent first-calls must all receive the same single instance."""
    instances: list[KibanaClient] = []
    lock = threading.Lock()

    def fetch() -> None:
        c = get_client()
        with lock:
            instances.append(c)

    threads = [threading.Thread(target=fetch) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(instances) == 10
    assert all(c is instances[0] for c in instances)


def test_get_client_missing_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIBANA_URL", raising=False)
    monkeypatch.delenv("KIBANA_API_KEY", raising=False)
    monkeypatch.delenv("KIBANA_USERNAME", raising=False)
    monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    from kibana_mcp.errors import ConfigError

    with pytest.raises(ConfigError):
        get_client()
