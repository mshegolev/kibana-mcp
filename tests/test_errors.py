"""Unit tests for :mod:`kibana_mcp.errors`."""

from __future__ import annotations

from unittest.mock import MagicMock

import requests

from kibana_mcp.errors import ConfigError, handle


def _make_http_error(status_code: int, body: str = "") -> requests.HTTPError:
    """Build a minimal HTTPError with a mocked response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    exc = requests.HTTPError(response=resp)
    exc.response = resp
    return exc


class TestHandleConfigError:
    def test_config_error_mentions_env_vars(self) -> None:
        msg = handle(ConfigError("KIBANA_URL is not set"), "listing indices")
        assert "KIBANA_URL" in msg
        assert "listing indices" in msg

    def test_config_error_mentions_api_key(self) -> None:
        msg = handle(ConfigError("missing cred"), "searching logs")
        assert "KIBANA_API_KEY" in msg or "KIBANA_USERNAME" in msg


class TestHandleHttpErrors:
    def test_400_bad_request_mentions_query_syntax(self) -> None:
        exc = _make_http_error(400, '{"error": "parse_exception"}')
        msg = handle(exc, "searching logs")
        assert "400" in msg
        assert "query" in msg.lower()
        assert "parse_exception" in msg

    def test_401_mentions_api_key(self) -> None:
        exc = _make_http_error(401)
        msg = handle(exc, "searching logs")
        assert "401" in msg
        assert "KIBANA_API_KEY" in msg

    def test_403_mentions_permissions(self) -> None:
        exc = _make_http_error(403)
        msg = handle(exc, "listing indices")
        assert "403" in msg
        assert "permission" in msg.lower() or "privilege" in msg.lower()

    def test_404_suggests_list_indices(self) -> None:
        exc = _make_http_error(404)
        msg = handle(exc, "searching logs")
        assert "404" in msg
        assert "kibana_list_indices" in msg

    def test_429_mentions_rate_limit(self) -> None:
        exc = _make_http_error(429)
        msg = handle(exc, "searching logs")
        assert "429" in msg
        assert "rate" in msg.lower()

    def test_500_server_error(self) -> None:
        exc = _make_http_error(500)
        msg = handle(exc, "aggregating logs")
        assert "500" in msg
        assert "server" in msg.lower()

    def test_503_server_error(self) -> None:
        exc = _make_http_error(503)
        msg = handle(exc, "listing dashboards")
        assert "503" in msg

    def test_unknown_http_error_with_body(self) -> None:
        exc = _make_http_error(418, "I'm a teapot")
        msg = handle(exc, "doing something")
        assert "418" in msg

    def test_http_error_none_response(self) -> None:
        exc = requests.HTTPError("generic error")
        exc.response = None  # type: ignore[assignment]
        msg = handle(exc, "searching")
        assert "Error" in msg


class TestHandleConnectionErrors:
    def test_connection_error_mentions_kibana_url(self) -> None:
        exc = requests.ConnectionError("Failed to connect")
        msg = handle(exc, "listing indices")
        assert "KIBANA_URL" in msg
        assert "connect" in msg.lower()

    def test_timeout_mentions_time_range(self) -> None:
        exc = requests.Timeout("timed out")
        msg = handle(exc, "searching logs")
        assert "time" in msg.lower()
        assert "size" in msg.lower()

    def test_value_error_mentions_parameter(self) -> None:
        exc = ValueError("sort_order must be 'asc' or 'desc'")
        msg = handle(exc, "searching logs")
        assert "sort_order" in msg

    def test_generic_exception(self) -> None:
        exc = RuntimeError("unexpected")
        msg = handle(exc, "doing something")
        assert "RuntimeError" in msg
        assert "unexpected" in msg


# ── Fake opensearch-py exception hierarchy ────────────────────────────────────
# opensearch-py is an optional extra (not installed in dev env). errors.handle
# duck-types its transport exceptions by class name in the MRO, so these fakes
# mirror the real names/attributes: TransportError(status_code, error, info),
# ConnectionError(TransportError), ConnectionTimeout(ConnectionError), and
# the status-specific subclasses.


class OpenSearchException(Exception):
    pass


class TransportError(OpenSearchException):
    def __init__(self, status_code: object = "N/A", error: str = "", info: object = None) -> None:
        super().__init__(status_code, error, info)
        self.status_code = status_code
        self.error = error
        self.info = info


class ConnectionError(TransportError):  # noqa: A001 — mirrors opensearchpy name
    pass


class ConnectionTimeout(ConnectionError):
    pass


class AuthenticationException(TransportError):
    pass


class AuthorizationException(TransportError):
    pass


class NotFoundError(TransportError):
    pass


class RequestError(TransportError):
    pass


class TestHandleOpenSearchErrors:
    """OS-mode error parity: opensearch-py transport exceptions get the same
    actionable playbooks as the requests path (MD-01)."""

    def test_401_mentions_api_key(self) -> None:
        exc = AuthenticationException(401, "AuthenticationException", {"error": "unauthorized"})
        msg = handle(exc, "searching logs")
        assert "401" in msg
        assert "KIBANA_API_KEY" in msg

    def test_403_mentions_permissions(self) -> None:
        exc = AuthorizationException(403, "AuthorizationException", {})
        msg = handle(exc, "listing indices")
        assert "403" in msg
        assert "permission" in msg.lower() or "privilege" in msg.lower()

    def test_404_suggests_list_indices(self) -> None:
        exc = NotFoundError(404, "index_not_found_exception", {})
        msg = handle(exc, "searching logs")
        assert "404" in msg
        assert "kibana_list_indices" in msg

    def test_400_mentions_query_syntax(self) -> None:
        exc = RequestError(400, "parse_exception", {"error": "parse_exception"})
        msg = handle(exc, "searching logs")
        assert "400" in msg
        assert "query" in msg.lower()
        assert "parse_exception" in msg

    def test_500_server_error(self) -> None:
        exc = TransportError(503, "unavailable", {})
        msg = handle(exc, "aggregating logs")
        assert "503" in msg
        assert "server" in msg.lower()

    def test_connection_error_mentions_opensearch_url(self) -> None:
        exc = ConnectionError("N/A", "connection refused", None)
        msg = handle(exc, "listing indices")
        assert "OPENSEARCH_URL" in msg
        assert "connect" in msg.lower()

    def test_connection_timeout_mentions_time_range(self) -> None:
        exc = ConnectionTimeout("N/A", "timed out", None)
        msg = handle(exc, "searching logs")
        assert "timed out" in msg.lower()
        assert "time_from" in msg
        assert "size" in msg.lower()

    def test_non_integer_status_code_falls_back_gracefully(self) -> None:
        exc = TransportError("N/A", "weird", None)
        msg = handle(exc, "doing something")
        assert "Error" in msg
        assert "AttributeError" not in msg

    def test_builtin_connection_error_not_misclassified(self) -> None:
        """Python's builtin ConnectionError has no TransportError in its MRO —
        it must fall through to the generic branch, not the OS branch."""
        import builtins

        exc = builtins.ConnectionError("refused")
        msg = handle(exc, "doing something")
        assert "OPENSEARCH_URL" not in msg
