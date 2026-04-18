"""Unit tests for :mod:`kibana_mcp.client`.

Tests cover env-var parsing, URL validation, auth selection precedence,
anonymous fallback, and session configuration. No network calls are made.
"""

from __future__ import annotations

import pytest

from kibana_mcp.client import KibanaClient, _parse_bool, _validate_url
from kibana_mcp.errors import ConfigError


class TestParseBool:
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
    def test_strips_trailing_slash(self) -> None:
        assert _validate_url("https://kibana.example.com/") == "https://kibana.example.com"

    def test_strips_whitespace(self) -> None:
        assert _validate_url("  https://kibana.example.com  ") == "https://kibana.example.com"

    def test_http_scheme_allowed(self) -> None:
        assert _validate_url("http://kibana.local") == "http://kibana.local"

    def test_empty_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="KIBANA_URL"):
            _validate_url("")

    def test_missing_scheme_raises(self) -> None:
        with pytest.raises(ConfigError, match="http:// or https://"):
            _validate_url("kibana.example.com")

    def test_wrong_scheme_raises(self) -> None:
        with pytest.raises(ConfigError, match="http:// or https://"):
            _validate_url("ftp://kibana.example.com")

    def test_missing_host_raises(self) -> None:
        with pytest.raises(ConfigError, match="missing host"):
            _validate_url("https://")

    def test_custom_var_name_in_message(self) -> None:
        with pytest.raises(ConfigError, match="ELASTICSEARCH_URL"):
            _validate_url("", "ELASTICSEARCH_URL")


class TestKibanaClientInit:
    def test_missing_kibana_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIBANA_URL", raising=False)
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        with pytest.raises(ConfigError, match="KIBANA_URL"):
            KibanaClient()

    def test_anonymous_auth_when_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient()
        try:
            assert "Authorization" not in client._es_session.headers
            assert client._es_session.auth is None
        finally:
            client.close()

    def test_api_key_auth_sets_authorization_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.setenv("KIBANA_API_KEY", "dGVzdDp0ZXN0")
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient()
        try:
            assert client._es_session.headers.get("Authorization") == "ApiKey dGVzdDp0ZXN0"
            assert client._kibana_session.headers.get("Authorization") == "ApiKey dGVzdDp0ZXN0"
        finally:
            client.close()

    def test_basic_auth_when_username_password_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.setenv("KIBANA_USERNAME", "elastic")
        monkeypatch.setenv("KIBANA_PASSWORD", "changeme")  # pragma: allowlist secret
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient()
        try:
            assert client._es_session.auth == ("elastic", "changeme")
            assert client._kibana_session.auth == ("elastic", "changeme")
            assert "Authorization" not in client._es_session.headers
        finally:
            client.close()

    def test_api_key_takes_priority_over_basic_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.setenv("KIBANA_API_KEY", "mykey")
        monkeypatch.setenv("KIBANA_USERNAME", "elastic")
        monkeypatch.setenv("KIBANA_PASSWORD", "changeme")  # pragma: allowlist secret
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient()
        try:
            assert "ApiKey" in client._es_session.headers.get("Authorization", "")
            assert client._es_session.auth is None
        finally:
            client.close()

    def test_direct_es_url_disables_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.setenv("ELASTICSEARCH_URL", "https://es.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        client = KibanaClient()
        try:
            assert client.es_url == "https://es.example.com"
            assert client._use_kibana_proxy is False
        finally:
            client.close()

    def test_no_es_url_enables_kibana_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        client = KibanaClient()
        try:
            assert client._use_kibana_proxy is True
            assert client.es_url == "https://kibana.example.com"
        finally:
            client.close()

    def test_ssl_verify_false_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.setenv("KIBANA_SSL_VERIFY", "false")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient()
        try:
            assert client.ssl_verify is False
            assert client._es_session.verify is False
        finally:
            client.close()

    def test_trust_env_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient()
        try:
            assert client._es_session.trust_env is False
            assert client._kibana_session.trust_env is False
        finally:
            client.close()

    def test_kbn_xsrf_header_on_kibana_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient()
        try:
            assert client._kibana_session.headers.get("kbn-xsrf") == "true"
        finally:
            client.close()

    def test_overrides_take_precedence_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://env.example.com")
        monkeypatch.setenv("KIBANA_API_KEY", "env-key")
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        client = KibanaClient(kibana_url="https://explicit.example.com", api_key="explicit-key")
        try:
            assert client.kibana_url == "https://explicit.example.com"
            assert client.api_key == "explicit-key"
        finally:
            client.close()

    def test_es_url_for_direct_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.setenv("ELASTICSEARCH_URL", "https://es.example.com:9200")
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        client = KibanaClient()
        try:
            url = client._es_url_for("/_cat/indices")
            assert url == "https://es.example.com:9200/_cat/indices"
        finally:
            client.close()

    def test_es_url_for_proxy_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
        monkeypatch.delenv("KIBANA_API_KEY", raising=False)
        monkeypatch.delenv("KIBANA_USERNAME", raising=False)
        monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
        client = KibanaClient()
        try:
            url = client._es_url_for("/_cat/indices")
            assert "api/console/proxy" in url
            assert "method=GET" in url
        finally:
            client.close()
