"""E2E mode-selection tests for :mod:`kibana_mcp._mcp`.

Tests verify that OPENSEARCH_MODE env var correctly selects between
KibanaClient (requests-based) and OpenSearchClient (opensearch-py-based)
backends. All backends are duck-type compatible (get_es / post_es).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
def os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide env vars for OpenSearch mode."""
    monkeypatch.setenv("OPENSEARCH_MODE", "true")
    monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
    monkeypatch.delenv("KIBANA_URL", raising=False)
    monkeypatch.delenv("KIBANA_API_KEY", raising=False)
    monkeypatch.delenv("KIBANA_USERNAME", raising=False)
    monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)


@pytest.fixture()
def kibana_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide env vars for Kibana/ES mode."""
    monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
    monkeypatch.delenv("OPENSEARCH_MODE", raising=False)
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    monkeypatch.delenv("KIBANA_API_KEY", raising=False)


class TestOSModeSwitch:
    def test_opensearch_mode_unset_selects_kibana_client(self, kibana_env: None) -> None:
        """OPENSEARCH_MODE unset → get_client() returns KibanaClient."""
        client = get_client()
        assert isinstance(client, KibanaClient)

    def test_opensearch_mode_false_selects_kibana_client(
        self, kibana_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_MODE=false → get_client() returns KibanaClient."""
        monkeypatch.setenv("OPENSEARCH_MODE", "false")
        client = get_client()
        assert isinstance(client, KibanaClient)

    def test_opensearch_mode_true_selects_opensearch_client(self, os_env: None) -> None:
        """OPENSEARCH_MODE=true, OPENSEARCH_URL set, OpenSearch mocked →
        get_client() returns OpenSearchClient."""
        from kibana_mcp.os_client import OpenSearchClient

        mock_inner = MagicMock()
        with patch("kibana_mcp.os_client.OpenSearch", return_value=mock_inner):
            client = get_client()
        assert isinstance(client, OpenSearchClient)

    def test_opensearch_mode_true_missing_url_raises(self, os_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENSEARCH_MODE=true, OPENSEARCH_URL unset → ConfigError
        mentioning OPENSEARCH_URL."""
        from kibana_mcp.errors import ConfigError

        monkeypatch.delenv("OPENSEARCH_URL", raising=False)
        with patch("kibana_mcp.os_client.OpenSearch", MagicMock()):
            with pytest.raises(ConfigError, match="OPENSEARCH_URL"):
                get_client()

    def test_opensearch_mode_true_missing_package_raises(self, os_env: None) -> None:
        """OPENSEARCH_MODE=true, opensearch-py absent → ConfigError
        mentioning opensearch-py."""
        from kibana_mcp.errors import ConfigError

        with patch("kibana_mcp.os_client.OpenSearch", None):
            with pytest.raises(ConfigError, match="opensearch-py"):
                get_client()

    def test_dashboard_tool_in_os_mode_without_kibana_url_is_actionable(self, os_env: None) -> None:
        """OS mode + dashboard tool + no KIBANA_URL → ToolError mentioning
        KIBANA_URL (actionable ConfigError), NOT a raw AttributeError."""
        from mcp.server.fastmcp.exceptions import ToolError

        from kibana_mcp.tools import kibana_list_dashboards

        with patch("kibana_mcp.os_client.OpenSearch", return_value=MagicMock()):
            with pytest.raises(ToolError, match="KIBANA_URL") as excinfo:
                kibana_list_dashboards()
        assert "AttributeError" not in str(excinfo.value)

    def test_dashboard_tool_in_os_mode_with_kibana_url_delegates(
        self, os_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OS mode + KIBANA_URL set → dashboard tool works via the
        delegated KibanaClient (Kibana REST path kept regardless of mode)."""
        from kibana_mcp.tools import kibana_list_dashboards

        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        mock_kibana = MagicMock()
        mock_kibana.get_kibana.return_value = {
            "total": 1,
            "saved_objects": [{"id": "d1", "attributes": {"title": "Ops"}, "updated_at": None}],
        }
        with patch("kibana_mcp.os_client.OpenSearch", return_value=MagicMock()):
            with patch("kibana_mcp.client.KibanaClient", return_value=mock_kibana):
                result = kibana_list_dashboards()
        assert result.structuredContent["total"] == 1
        assert result.structuredContent["dashboards"][0]["id"] == "d1"
