"""HTTP client for Kibana REST API and Elasticsearch REST API.

Thin wrapper around :mod:`requests` — reads config from env vars, supports
ApiKey auth, Basic auth, or anonymous access, handles SSL-verify toggling,
and exposes ``get_es`` / ``post_es`` / ``get_kibana`` methods for Elasticsearch
and Kibana endpoints respectively.

**Auth priority:** ApiKey > Basic (username + password) > anonymous.

**URL routing:** Elasticsearch calls use ``ELASTICSEARCH_URL`` if set, otherwise
fall back to the Kibana Console proxy (``KIBANA_URL/api/console/proxy?path=...``).
Set ``ELASTICSEARCH_URL`` for direct ES access (faster, no proxy overhead).

**Threading model.** The client uses ``requests`` (synchronous). FastMCP runs
synchronous ``@mcp.tool`` in a worker thread via ``anyio.to_thread.run_sync``,
so blocking HTTP calls don't block the asyncio event loop.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3

from kibana_mcp.errors import ConfigError


def _parse_bool(value: str | bool | None, *, default: bool) -> bool:
    """Parse an env-var boolean.

    Accepts true/false/1/0/yes/no/on/off (case-insensitive). Returns
    ``default`` when ``value`` is ``None`` or empty.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def _validate_url(url: str, var_name: str = "KIBANA_URL") -> str:
    """Validate that ``url`` is a well-formed HTTP/HTTPS URL.

    Returns the URL with leading/trailing whitespace and any trailing slash
    stripped. Raises :class:`ConfigError` if the URL is missing scheme/host
    or uses an unsupported scheme.
    """
    if not url:
        raise ConfigError(f"{var_name} is not set — configure the env var")

    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(f"{var_name} must start with http:// or https:// (got: {url!r})")
    if not parsed.netloc:
        raise ConfigError(f"{var_name} is missing host (got: {url!r})")
    return cleaned.rstrip("/")


class KibanaClient:
    """Minimal Kibana + Elasticsearch REST client.

    Reads ``KIBANA_URL``, ``ELASTICSEARCH_URL``, ``KIBANA_API_KEY``,
    ``KIBANA_USERNAME``, ``KIBANA_PASSWORD``, ``KIBANA_SSL_VERIFY`` from env.

    Auth selection:
    - If ``KIBANA_API_KEY`` is set → ``Authorization: ApiKey <key>``
    - Else if ``KIBANA_USERNAME`` + ``KIBANA_PASSWORD`` → HTTP Basic
    - Else → anonymous (no auth header)

    Args:
        kibana_url: Override ``KIBANA_URL`` env var. If ``None``, read from env.
        es_url: Override ``ELASTICSEARCH_URL`` env var. If ``None``, read from env.
        api_key: Override ``KIBANA_API_KEY``. If ``None``, read from env.
        username: Override ``KIBANA_USERNAME``. If ``None``, read from env.
        password: Override ``KIBANA_PASSWORD``. If ``None``, read from env.
        ssl_verify: Override ``KIBANA_SSL_VERIFY``. If ``None``, read from env.

    Raises:
        ConfigError: If ``KIBANA_URL`` is missing or malformed.
    """

    def __init__(
        self,
        kibana_url: str | None = None,
        es_url: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        ssl_verify: bool | None = None,
    ) -> None:
        raw_kibana = kibana_url if kibana_url is not None else os.environ.get("KIBANA_URL", "")
        self.kibana_url = _validate_url(raw_kibana, "KIBANA_URL")

        raw_es = es_url if es_url is not None else os.environ.get("ELASTICSEARCH_URL", "")
        if raw_es:
            self.es_url = _validate_url(raw_es, "ELASTICSEARCH_URL")
            self._use_kibana_proxy = False
        else:
            self.es_url = self.kibana_url
            self._use_kibana_proxy = True

        self.api_key = api_key if api_key is not None else os.environ.get("KIBANA_API_KEY", "")
        self.username = username if username is not None else os.environ.get("KIBANA_USERNAME", "")
        self.password = password if password is not None else os.environ.get("KIBANA_PASSWORD", "")

        if ssl_verify is None:
            ssl_verify = _parse_bool(os.environ.get("KIBANA_SSL_VERIFY"), default=True)
        self.ssl_verify = ssl_verify

        self._es_session = requests.Session()
        self._kibana_session = requests.Session()

        # Auth — ApiKey takes priority over Basic, both over anonymous.
        if self.api_key:
            auth_header = {"Authorization": f"ApiKey {self.api_key}"}
            self._es_session.headers.update(auth_header)
            self._kibana_session.headers.update(auth_header)
        elif self.username and self.password:
            self._es_session.auth = (self.username, self.password)
            self._kibana_session.auth = (self.username, self.password)
        # else: anonymous — no auth header

        # Common headers
        self._es_session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self._kibana_session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "kbn-xsrf": "true",  # Required by Kibana's CSRF guard
            }
        )

        for sess in (self._es_session, self._kibana_session):
            sess.verify = self.ssl_verify
            # Disable env-based proxy: Kibana/ES are often internal services
            # only reachable directly (not through a corporate proxy).
            sess.trust_env = False

        if not self.ssl_verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ── Elasticsearch methods ────────────────────────────────────────────────

    def _es_url_for(self, path: str) -> str:
        """Build the full URL for an Elasticsearch request.

        If no direct ``ELASTICSEARCH_URL`` is configured, routes through the
        Kibana Console proxy (``/api/console/proxy?path=<path>&method=<method>``).
        When using the proxy the caller must use GET with a body (Kibana proxies
        any ES method), so we always send JSON bodies via GET params for the proxy.
        """
        if not self._use_kibana_proxy:
            return f"{self.es_url}{path}"
        # Kibana proxy: path must be URL-encoded in the query string
        return f"{self.kibana_url}/api/console/proxy?path={requests.utils.quote(path, safe='/:?=&')}&method=GET"

    def get_es(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET an Elasticsearch endpoint; return parsed JSON."""
        url = self._es_url_for(path)
        resp = self._es_session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def post_es(self, path: str, body: dict[str, Any]) -> Any:
        """POST a JSON body to an Elasticsearch endpoint; return parsed JSON.

        When routing through Kibana Console proxy, the body is sent as-is
        in the POST body (Kibana forwards it to ES).
        """
        if self._use_kibana_proxy:
            # Kibana Console proxy: POST to proxy URL, body is the ES request body
            url = f"{self.kibana_url}/api/console/proxy?path={requests.utils.quote(path, safe='/:?=&')}&method=POST"
            resp = self._kibana_session.post(url, json=body, timeout=30)
        else:
            url = f"{self.es_url}{path}"
            resp = self._es_session.post(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ── Kibana Saved Objects methods ─────────────────────────────────────────

    def get_kibana(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET a Kibana REST endpoint; return parsed JSON."""
        url = f"{self.kibana_url}{path}"
        resp = self._kibana_session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        """Close the underlying HTTP sessions (called from lifespan on shutdown)."""
        self._es_session.close()
        self._kibana_session.close()
