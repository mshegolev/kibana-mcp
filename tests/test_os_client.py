"""Unit tests for :mod:`kibana_mcp.os_client`.

Tests cover env-var parsing, URL validation, auth selection priority
(SigV4 > token > basic > anonymous), forced auth mode validation, and
method duck-type compatibility with KibanaClient.

No network calls are made; opensearch-py transport is mocked throughout.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from kibana_mcp.errors import ConfigError
from kibana_mcp.os_client import OpenSearchClient, _parse_bool, _validate_url


def _make_boto3_mock() -> MagicMock:
    """Return a mock boto3 module with a Session that returns fake credentials.

    boto3 is an optional extra (not installed in dev env).  We inject the mock
    into sys.modules so that ``import boto3`` inside _build_sigv4_auth finds it.
    """
    mock_boto3 = MagicMock()
    mock_creds = MagicMock()
    mock_boto3.Session.return_value.get_credentials.return_value = mock_creds
    return mock_boto3


class TestParseBool:
    """Shared helper — same tests as test_client.py (identical function)."""

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "YES"])
    def test_truthy_strings(self, value: str) -> None:
        assert _parse_bool(value, default=False) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "OFF"])
    def test_falsy_strings(self, value: str) -> None:
        assert _parse_bool(value, default=True) is False

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_returns_default(self, value: str | None) -> None:
        assert _parse_bool(value, default=True) is True
        assert _parse_bool(value, default=False) is False

    def test_bool_passthrough(self) -> None:
        assert _parse_bool(True, default=False) is True
        assert _parse_bool(False, default=True) is False


class TestValidateUrl:
    """URL validation helper — adapted for OPENSEARCH_URL var name."""

    def test_strips_trailing_slash(self) -> None:
        assert (
            _validate_url("https://opensearch.example.com/")
            == "https://opensearch.example.com"
        )

    def test_strips_whitespace(self) -> None:
        assert (
            _validate_url("  https://opensearch.example.com  ")
            == "https://opensearch.example.com"
        )

    def test_http_scheme_allowed(self) -> None:
        assert _validate_url("http://opensearch.local") == "http://opensearch.local"

    def test_empty_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="OPENSEARCH_URL"):
            _validate_url("", "OPENSEARCH_URL")

    def test_missing_scheme_raises(self) -> None:
        with pytest.raises(ConfigError, match="http:// or https://"):
            _validate_url("opensearch.example.com")

    def test_wrong_scheme_raises(self) -> None:
        with pytest.raises(ConfigError, match="http:// or https://"):
            _validate_url("ftp://opensearch.example.com")

    def test_missing_host_raises(self) -> None:
        with pytest.raises(ConfigError, match="missing host"):
            _validate_url("https://")

    def test_custom_var_name_in_message(self) -> None:
        with pytest.raises(ConfigError, match="MY_URL"):
            _validate_url("", "MY_URL")


class TestOpenSearchClientInit:
    """Config validation and auth mode selection tests."""

    # ── Config validation ────────────────────────────────────────────────────

    def test_missing_url_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_URL unset → ConfigError mentioning OPENSEARCH_URL."""
        monkeypatch.delenv("OPENSEARCH_URL", raising=False)
        with patch("kibana_mcp.os_client.OpenSearch"):
            with pytest.raises(ConfigError, match="OPENSEARCH_URL"):
                OpenSearchClient()

    def test_missing_opensearch_package_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenSearch = None (package absent) → ConfigError mentioning opensearch-py."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        with patch("kibana_mcp.os_client.OpenSearch", None):
            with pytest.raises(ConfigError, match="opensearch-py"):
                OpenSearchClient()

    def test_ssl_verify_false_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KIBANA_SSL_VERIFY=false → client.ssl_verify is False."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("KIBANA_SSL_VERIFY", "false")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client = MagicMock()
        with patch("kibana_mcp.os_client.OpenSearch", return_value=mock_client):
            client = OpenSearchClient()
            assert client.ssl_verify is False

    def test_constructor_overrides_take_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructor kwargs override env vars."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://env.example.com")
        monkeypatch.setenv("KIBANA_API_KEY", "env-key")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client = MagicMock()
        with patch("kibana_mcp.os_client.OpenSearch", return_value=mock_client):
            client = OpenSearchClient(
                os_url="https://explicit.example.com",
                api_key="explicit-key",
            )
            assert client.os_url == "https://explicit.example.com"
            assert client.api_key == "explicit-key"

    # ── Auto-detect auth ─────────────────────────────────────────────────────

    def test_auto_detect_sigv4_when_aws_region_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AWS_REGION set → SigV4 auth wired via http_auth kwarg."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        # boto3 is an optional extra — inject a mock into sys.modules so that
        # the ``import boto3`` inside _build_sigv4_auth resolves without
        # requiring a real installation.
        mock_boto3 = _make_boto3_mock()
        mock_client = MagicMock()
        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            with patch(
                "kibana_mcp.os_client.OpenSearch", return_value=mock_client
            ) as mock_os_cls:
                with patch("kibana_mcp.os_client.AWSV4SignerAuth") as mock_aws_auth:
                    OpenSearchClient()
                    mock_aws_auth.assert_called_once()
                    _, kwargs = mock_os_cls.call_args
                    assert "http_auth" in kwargs

    def test_auto_detect_bearer_token_when_api_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KIBANA_API_KEY set (no AWS_REGION) → headers["Authorization"] == Bearer.

        Bearer MUST arrive via the headers kwarg — opensearch-py's http_auth
        only handles tuple or AWSV4SignerAuth; a bare string is silently
        ignored and causes a 401 at runtime.
        """
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("KIBANA_API_KEY", "test-key")
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client = MagicMock()
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            OpenSearchClient()
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"

    def test_auto_detect_basic_when_username_password_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """username+password (no AWS_REGION, no api_key) → http_auth tuple."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.setenv("KIBANA_USERNAME", "admin")
        monkeypatch.setenv("KIBANA_PASSWORD", "secret")
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client = MagicMock()
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            client = OpenSearchClient()
            assert client.username == "admin"
            assert client.password == "secret"
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("http_auth") == ("admin", "secret")

    def test_auto_detect_anonymous_when_no_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No credentials → OpenSearch() called without auth kwargs."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client = MagicMock()
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            OpenSearchClient()
            _, kwargs = mock_os_cls.call_args
            assert "http_auth" not in kwargs
            assert "Authorization" not in kwargs.get("headers", {})

    def test_ssl_kwargs_consistent_across_branches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KIBANA_SSL_VERIFY=false → verify_certs=False AND ssl_show_warn=False
        (warning-suppression parity with KibanaClient); no redundant use_ssl."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("KIBANA_SSL_VERIFY", "false")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client = MagicMock()
        for api_key_env in (None, "k"):  # anonymous and token branches
            if api_key_env is None:
                monkeypatch.delenv("KIBANA_API_KEY", raising=False)
            else:
                monkeypatch.setenv("KIBANA_API_KEY", api_key_env)
            with patch(
                "kibana_mcp.os_client.OpenSearch", return_value=mock_client
            ) as mock_os_cls:
                OpenSearchClient()
                _, kwargs = mock_os_cls.call_args
                assert kwargs.get("verify_certs") is False
                assert kwargs.get("ssl_show_warn") is False
                assert "use_ssl" not in kwargs

    def test_ssl_show_warn_true_when_verify_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (verify on) → ssl_show_warn stays True."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("KIBANA_SSL_VERIFY", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=MagicMock()
        ) as mock_os_cls:
            OpenSearchClient()
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("verify_certs") is True
            assert kwargs.get("ssl_show_warn") is True

    def test_timeout_30s_passed_to_constructor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parity with KibanaClient: every request uses a 30 s timeout
        (opensearch-py's default is 10 s)."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client = MagicMock()
        # Anonymous branch
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            OpenSearchClient()
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("timeout") == 30
        # Token branch
        monkeypatch.setenv("KIBANA_API_KEY", "k")
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            OpenSearchClient()
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("timeout") == 30

    # ── Forced OPENSEARCH_AUTH ────────────────────────────────────────────────

    def test_forced_sigv4_mode_valid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=sigv4 + AWS_REGION → SigV4 auth constructed."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "sigv4")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)

        mock_boto3 = _make_boto3_mock()
        mock_client = MagicMock()
        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            with patch(
                "kibana_mcp.os_client.OpenSearch", return_value=mock_client
            ) as mock_os_cls:
                with patch("kibana_mcp.os_client.AWSV4SignerAuth") as mock_aws_auth:
                    OpenSearchClient()
                    mock_aws_auth.assert_called_once()
                    _, kwargs = mock_os_cls.call_args
                    assert "http_auth" in kwargs

    def test_forced_sigv4_mode_missing_region_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=sigv4 without AWS_REGION → ConfigError."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "sigv4")
        monkeypatch.delenv("AWS_REGION", raising=False)

        with patch("kibana_mcp.os_client.OpenSearch"):
            with pytest.raises(ConfigError, match="AWS_REGION"):
                OpenSearchClient()

    def test_forced_basic_mode_valid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=basic + credentials → http_auth tuple."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "basic")
        monkeypatch.setenv("KIBANA_USERNAME", "admin")
        monkeypatch.setenv("KIBANA_PASSWORD", "secret")
        monkeypatch.delenv("AWS_REGION", raising=False)

        mock_client = MagicMock()
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            client = OpenSearchClient()
            assert client.username == "admin"
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("http_auth") == ("admin", "secret")

    def test_forced_basic_mode_missing_credentials_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=basic without username → ConfigError."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "basic")
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)

        with patch("kibana_mcp.os_client.OpenSearch"):
            with pytest.raises(ConfigError, match="KIBANA_USERNAME"):
                OpenSearchClient()

    def test_forced_token_mode_valid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=token + KIBANA_API_KEY → Bearer via headers kwarg."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "token")
        monkeypatch.setenv("KIBANA_API_KEY", "mykey")
        monkeypatch.delenv("AWS_REGION", raising=False)

        mock_client = MagicMock()
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            OpenSearchClient()
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("headers", {}).get("Authorization") == "Bearer mykey"

    def test_forced_token_mode_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=token without KIBANA_API_KEY → ConfigError."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "token")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)

        with patch("kibana_mcp.os_client.OpenSearch"):
            with pytest.raises(ConfigError, match="KIBANA_API_KEY"):
                OpenSearchClient()

    def test_forced_unknown_auth_mode_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=oauth2 (unknown value) → ConfigError."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "oauth2")

        with patch("kibana_mcp.os_client.OpenSearch"):
            with pytest.raises(ConfigError):
                OpenSearchClient()

    def test_auth_mode_env_whitespace_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_AUTH=' token ' (whitespace from shell/compose) still
        selects token mode instead of hitting the unknown-mode branch."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", " token ")
        monkeypatch.setenv("KIBANA_API_KEY", "mykey")
        monkeypatch.delenv("AWS_REGION", raising=False)

        mock_client = MagicMock()
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            OpenSearchClient()
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("headers", {}).get("Authorization") == "Bearer mykey"

    def test_auth_mode_constructor_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auth_mode kwarg overrides the OPENSEARCH_AUTH env var."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.setenv("OPENSEARCH_AUTH", "basic")
        monkeypatch.setenv("KIBANA_API_KEY", "mykey")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)

        mock_client = MagicMock()
        with patch(
            "kibana_mcp.os_client.OpenSearch", return_value=mock_client
        ) as mock_os_cls:
            OpenSearchClient(auth_mode="token")
            _, kwargs = mock_os_cls.call_args
            assert kwargs.get("headers", {}).get("Authorization") == "Bearer mykey"


class TestOpenSearchClientMethods:
    """Duck-type method compatibility with KibanaClient."""

    def test_get_es_delegates_to_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_es(path, *, params) calls transport.perform_request("GET", ...)."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client_inst = MagicMock()
        mock_transport = MagicMock()
        mock_client_inst.transport = mock_transport
        mock_transport.perform_request.return_value = [{"index": ".kibana"}]

        with patch("kibana_mcp.os_client.OpenSearch", return_value=mock_client_inst):
            client = OpenSearchClient()
            result = client.get_es("/_cat/indices", params={"format": "json"})

        mock_transport.perform_request.assert_called_once_with(
            "GET",
            "/_cat/indices",
            params={"format": "json"},
        )
        assert result == [{"index": ".kibana"}]

    def test_post_es_delegates_to_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """post_es(path, body) calls transport.perform_request("POST", ...)."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client_inst = MagicMock()
        mock_transport = MagicMock()
        mock_client_inst.transport = mock_transport
        mock_transport.perform_request.return_value = {
            "hits": {"total": {"value": 5}, "hits": []}
        }

        with patch("kibana_mcp.os_client.OpenSearch", return_value=mock_client_inst):
            client = OpenSearchClient()
            result = client.post_es("/_search", {"query": {"match_all": {}}})

        mock_transport.perform_request.assert_called_once_with(
            "POST",
            "/_search",
            body={"query": {"match_all": {}}},
        )
        assert result == {"hits": {"total": {"value": 5}, "hits": []}}

    def test_close_is_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """close() on a constructed client raises no exception."""
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

        mock_client_inst = MagicMock()
        with patch("kibana_mcp.os_client.OpenSearch", return_value=mock_client_inst):
            client = OpenSearchClient()
            client.close()  # must not raise


class TestOpenSearchClientGetKibana:
    """get_kibana() delegation — dashboard tools keep the Kibana REST path."""

    @pytest.fixture(autouse=True)
    def _os_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("OPENSEARCH_AUTH", raising=False)

    def test_get_kibana_without_kibana_url_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No KIBANA_URL → actionable ConfigError, not AttributeError."""
        monkeypatch.delenv("KIBANA_URL", raising=False)
        with patch("kibana_mcp.os_client.OpenSearch", return_value=MagicMock()):
            client = OpenSearchClient()
            with pytest.raises(ConfigError, match="KIBANA_URL"):
                client.get_kibana("/api/saved_objects/_find")

    def test_get_kibana_delegates_to_kibana_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KIBANA_URL set → lazily constructs KibanaClient and delegates."""
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        mock_kibana = MagicMock()
        mock_kibana.get_kibana.return_value = {"saved_objects": []}
        with patch("kibana_mcp.os_client.OpenSearch", return_value=MagicMock()):
            client = OpenSearchClient()
            with patch(
                "kibana_mcp.client.KibanaClient", return_value=mock_kibana
            ) as mock_cls:
                result = client.get_kibana(
                    "/api/saved_objects/_find", params={"type": "dashboard"}
                )
                # Second call reuses the cached KibanaClient
                client.get_kibana("/api/saved_objects/_find")
        assert result == {"saved_objects": []}
        mock_cls.assert_called_once()
        mock_kibana.get_kibana.assert_any_call(
            "/api/saved_objects/_find", params={"type": "dashboard"}
        )

    def test_close_closes_lazy_kibana_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """close() also closes the lazily constructed KibanaClient."""
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        mock_kibana = MagicMock()
        with patch("kibana_mcp.os_client.OpenSearch", return_value=MagicMock()):
            client = OpenSearchClient()
            with patch("kibana_mcp.client.KibanaClient", return_value=mock_kibana):
                client.get_kibana("/api/saved_objects/_find")
            client.close()
        mock_kibana.close.assert_called_once()


class TestOpenSearchClientLiveIntegration:
    """Live integration test — skips automatically when OPENSEARCH_TEST_URL unset."""

    @pytest.mark.skipif(
        not os.environ.get("OPENSEARCH_TEST_URL"),
        reason="OPENSEARCH_TEST_URL not set — skipping live integration test",
    )
    def test_live_integration_search_skipped_when_no_url(self) -> None:
        """When OPENSEARCH_TEST_URL is set: construct real client, call _cat/health."""
        live_url = os.environ["OPENSEARCH_TEST_URL"]
        client = OpenSearchClient(os_url=live_url)
        try:
            result = client.get_es("/_cat/health", params={"format": "json"})
            assert isinstance(result, list), (
                f"Expected list from _cat/health, got {type(result)}"
            )
        finally:
            client.close()
