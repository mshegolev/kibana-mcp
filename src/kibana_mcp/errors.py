"""Actionable error messages for Kibana / Elasticsearch HTTP errors."""

from __future__ import annotations

import requests


class ConfigError(ValueError):
    """Raised when required environment variables are missing or malformed.

    Subclass of :class:`ValueError` so callers can use
    ``isinstance(..., ValueError)``, but narrow enough that :func:`handle`
    can distinguish config errors from Pydantic validation errors.
    """


def _status_message(code: int | None, action: str, body: str) -> str:
    """Map an HTTP status code to an actionable message.

    Shared by the ``requests.HTTPError`` path (ES/Kibana mode) and the
    opensearch-py ``TransportError`` path (OS mode) so both backends produce
    the same playbooks.
    """
    if code == 400:
        return (
            f"Error: bad request (HTTP 400) while {action}. "
            "The Elasticsearch query string may be malformed — check the `query` parameter syntax. "
            f"Server response: {body[:300]}"
        )

    if code == 401:
        return (
            f"Error: authentication failed (HTTP 401) while {action}. "
            "Verify KIBANA_API_KEY is valid (format: base64(id:api_key)), or "
            "check KIBANA_USERNAME + KIBANA_PASSWORD are correct and not expired."
        )

    if code == 403:
        return (
            f"Error: forbidden (HTTP 403) while {action}. "
            "The API key or user account lacks read permission on this index/resource. "
            "Grant cluster:monitor or indices:data/read privileges via Kibana → Stack Management → Roles."
        )

    if code == 404:
        return (
            f"Error: resource not found (HTTP 404) while {action}. "
            "The index pattern or dashboard ID does not exist. "
            "Use `kibana_list_indices` to discover available indices, "
            "or `kibana_list_dashboards` for dashboard IDs."
        )

    if code == 429:
        return (
            f"Error: rate-limited (HTTP 429) while {action}. "
            "Wait 30-60s before retrying. Reduce `size` to lower resource usage."
        )

    if code is not None and 500 <= code < 600:
        return (
            f"Error: Elasticsearch/Kibana server error (HTTP {code}) while {action}. "
            "This is usually transient — retry in a few seconds. "
            "Check Kibana status at KIBANA_URL/api/status."
        )

    return f"Error: HTTP {code} while {action}. Response: {body[:200]}"


def _timeout_message(action: str) -> str:
    return (
        f"Error: request timed out while {action}. "
        "The query may be too broad — add time_from/time_to to narrow the range, "
        "or reduce `size`."
    )


def _handle_opensearch(exc: Exception, action: str) -> str | None:
    """Map opensearch-py transport exceptions to actionable messages.

    opensearch-py is an optional extra, so it may not be importable here.
    Instead of importing it, duck-type by class names in the exception's MRO:
    every transport failure derives from ``opensearchpy.TransportError``
    (``ConnectionError`` / ``ConnectionTimeout`` are subclasses), and HTTP
    failures carry an integer ``status_code`` attribute.

    Returns ``None`` when ``exc`` does not look like an opensearch-py
    transport exception.
    """
    mro_names = {cls.__name__ for cls in type(exc).__mro__}
    if "TransportError" not in mro_names:
        return None

    if "ConnectionTimeout" in mro_names:
        return _timeout_message(action)

    if "ConnectionError" in mro_names:
        return (
            f"Error: could not connect while {action}. "
            "Check OPENSEARCH_URL, network access, and proxy settings."
        )

    code = getattr(exc, "status_code", None)
    if not isinstance(code, int):
        code = None
    info = getattr(exc, "info", None)
    body = "" if info is None else str(info)
    return _status_message(code, action, body)


def handle(exc: Exception, action: str) -> str:
    """Convert an exception raised while performing ``action`` into an
    LLM-readable string with a suggested next step.

    The goal is that the agent sees *why* the call failed and *what it could
    do about it* without needing to inspect a Python traceback.
    """
    if isinstance(exc, ConfigError):
        return (
            f"Error: configuration problem while {action} — {exc}. "
            "Check KIBANA_URL, KIBANA_API_KEY (or KIBANA_USERNAME + KIBANA_PASSWORD), "
            "ELASTICSEARCH_URL, KIBANA_SSL_VERIFY environment variables."
        )

    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else None
        body = ""
        if exc.response is not None:
            try:
                body = exc.response.text
            except Exception:
                pass
        return _status_message(code, action, body)

    if isinstance(exc, requests.ConnectionError):
        return (
            f"Error: could not connect while {action}. "
            "Check KIBANA_URL, ELASTICSEARCH_URL, network access, and proxy settings."
        )

    if isinstance(exc, requests.Timeout):
        return _timeout_message(action)

    opensearch_msg = _handle_opensearch(exc, action)
    if opensearch_msg is not None:
        return opensearch_msg

    if isinstance(exc, ValueError):
        return f"Error: invalid parameter while {action}: {exc}"

    return f"Error: unexpected {type(exc).__name__} while {action}: {exc}"
