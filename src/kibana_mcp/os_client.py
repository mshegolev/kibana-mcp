"""OpenSearch REST client via opensearch-py SDK.

Duck-type compatible with :class:`kibana_mcp.client.KibanaClient` — exposes
``get_es`` / ``post_es`` / ``get_kibana`` / ``close`` with identical signatures
so ``tools.py`` requires no changes when ``OPENSEARCH_MODE=true``.
``get_kibana`` delegates to a lazily constructed ``KibanaClient`` (requires
``KIBANA_URL``) because dashboard tools keep using the Kibana REST path
regardless of mode — OS mode affects only ES-endpoint calls.

Auth priority (auto-detect, no ``OPENSEARCH_AUTH`` set):
- ``AWS_REGION`` set → SigV4 via :class:`opensearchpy.AWSV4SignerAuth` (boto3)
- Else ``KIBANA_API_KEY`` set → Bearer token (Authorization header)
- Else ``KIBANA_USERNAME`` + ``KIBANA_PASSWORD`` → HTTP Basic
- Else → anonymous (no auth)

Optional ``OPENSEARCH_AUTH`` env forces a mode:
- ``"sigv4"`` → require ``AWS_REGION``, use AWSV4SignerAuth
- ``"basic"`` → require ``KIBANA_USERNAME`` + ``KIBANA_PASSWORD``
- ``"token"`` → require ``KIBANA_API_KEY``

**Bearer token wiring note:** opensearch-py's ``http_auth`` param only accepts
a tuple (Basic auth) or :class:`opensearchpy.AWSV4SignerAuth` (SigV4). Passing
a bare string there is silently ignored — the request goes out without an
Authorization header and returns 401. Bearer tokens MUST be wired via:
``headers={"Authorization": f"Bearer {api_key}"}``.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from kibana_mcp.errors import ConfigError

# Lazy import — importable without opensearch-py installed.
# ConfigError with install hint is raised at OpenSearchClient() construction
# if OPENSEARCH_MODE=true and the package is missing.
try:
    from opensearchpy import AWSV4SignerAuth, OpenSearch
except ImportError:
    OpenSearch = None  # type: ignore[assignment,misc]
    AWSV4SignerAuth = None  # type: ignore[assignment,misc]


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


def _validate_url(url: str, var_name: str = "OPENSEARCH_URL") -> str:
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
        raise ConfigError(
            f"{var_name} must start with http:// or https:// (got: {url!r})"
        )
    if not parsed.netloc:
        raise ConfigError(f"{var_name} is missing host (got: {url!r})")
    return cleaned.rstrip("/")


class OpenSearchClient:
    """OpenSearch REST client via opensearch-py SDK.

    Reads ``OPENSEARCH_URL``, ``AWS_REGION``, ``KIBANA_API_KEY``,
    ``KIBANA_USERNAME``, ``KIBANA_PASSWORD``, ``OPENSEARCH_AUTH``,
    ``KIBANA_SSL_VERIFY`` from env.

    Auth priority (auto-detect):
    - If ``AWS_REGION`` is set → SigV4 via AWSV4SignerAuth (boto3)
    - Else if ``KIBANA_API_KEY`` is set → Token (Bearer <key>)
    - Else if ``KIBANA_USERNAME`` + ``KIBANA_PASSWORD`` → HTTP Basic
    - Else → anonymous (no auth)

    Optional ``OPENSEARCH_AUTH`` env forces a specific mode:
    - ``"sigv4"`` → require AWS_REGION, use AWSV4SignerAuth
    - ``"basic"`` → require KIBANA_USERNAME + KIBANA_PASSWORD
    - ``"token"`` → require KIBANA_API_KEY

    Args:
        os_url: Override ``OPENSEARCH_URL`` env var. If ``None``, read from env.
        aws_region: Override ``AWS_REGION``. If ``None``, read from env.
        api_key: Override ``KIBANA_API_KEY`` (reuses ES token var). If ``None``,
            read from env.
        username: Override ``KIBANA_USERNAME``. If ``None``, read from env.
        password: Override ``KIBANA_PASSWORD``. If ``None``, read from env.
        ssl_verify: Override ``KIBANA_SSL_VERIFY``. If ``None``, read from env.

    Raises:
        ConfigError: If ``OPENSEARCH_URL`` is missing, or if ``opensearch-py``
            is not installed.
    """

    def __init__(
        self,
        os_url: str | None = None,
        aws_region: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        ssl_verify: bool | None = None,
    ) -> None:
        # 1. Validate opensearch-py is installed
        if OpenSearch is None:
            raise ConfigError(
                "opensearch-py is not installed. "
                "Install it with: pip install 'kibana-mcp[opensearch]'"
            )

        # 2. Validate OPENSEARCH_URL — no fallback to ELASTICSEARCH_URL
        raw_os_url = os_url if os_url is not None else os.environ.get("OPENSEARCH_URL", "")
        self.os_url = _validate_url(raw_os_url, "OPENSEARCH_URL")

        # 3. Read credentials from arg or env
        self.aws_region = (
            aws_region if aws_region is not None else os.environ.get("AWS_REGION", "")
        )
        self.api_key = (
            api_key if api_key is not None else os.environ.get("KIBANA_API_KEY", "")
        )
        self.username = (
            username if username is not None else os.environ.get("KIBANA_USERNAME", "")
        )
        self.password = (
            password if password is not None else os.environ.get("KIBANA_PASSWORD", "")
        )

        # 4. Read forced auth mode
        auth_mode = os.environ.get("OPENSEARCH_AUTH", "").lower()

        # 5. SSL verification — reuse KIBANA_SSL_VERIFY
        if ssl_verify is None:
            ssl_verify = _parse_bool(os.environ.get("KIBANA_SSL_VERIFY"), default=True)
        self.ssl_verify = ssl_verify

        # Lazily constructed KibanaClient for get_kibana() delegation
        # (dashboard tools keep the Kibana REST path even in OS mode).
        self._kibana: Any | None = None

        # Common kwargs shared by every auth branch. The scheme is parsed
        # from the URL string in `hosts` by opensearch-py, so an explicit
        # `use_ssl` kwarg is redundant. ssl_show_warn=False when verification
        # is disabled mirrors KibanaClient's InsecureRequestWarning
        # suppression (no TLS warning spam in insecure mode).
        common_kwargs: dict[str, Any] = {
            "hosts": [self.os_url],
            "verify_certs": self.ssl_verify,
            "ssl_show_warn": self.ssl_verify,
            "timeout": 30,
        }

        # Resolve auth strategy and build the OpenSearch client in one pass.
        # Each auth mode calls OpenSearch() once with the correct kwargs.
        # Bearer tokens MUST be passed via headers={"Authorization": "Bearer <key>"}
        # because opensearch-py's http_auth only handles tuple or AWSV4SignerAuth;
        # a bare string is silently dropped, causing 401 at runtime.
        if auth_mode == "sigv4":
            if not self.aws_region:
                raise ConfigError(
                    "OPENSEARCH_AUTH=sigv4 requires AWS_REGION env var to be set"
                )
            self._client = OpenSearch(
                http_auth=self._build_sigv4_auth(), **common_kwargs
            )
        elif auth_mode == "basic":
            if not self.username or not self.password:
                raise ConfigError(
                    "OPENSEARCH_AUTH=basic requires KIBANA_USERNAME and "
                    "KIBANA_PASSWORD env vars"
                )
            self._client = OpenSearch(
                http_auth=(self.username, self.password), **common_kwargs
            )
        elif auth_mode == "token":
            if not self.api_key:
                raise ConfigError(
                    "OPENSEARCH_AUTH=token requires KIBANA_API_KEY env var"
                )
            self._client = OpenSearch(
                headers={"Authorization": f"Bearer {self.api_key}"},
                **common_kwargs,
            )
        elif auth_mode == "":
            # Auto-detect: AWS_REGION → SigV4; api_key → Bearer; basic; else anon
            if self.aws_region:
                self._client = OpenSearch(
                    http_auth=self._build_sigv4_auth(), **common_kwargs
                )
            elif self.api_key:
                self._client = OpenSearch(
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    **common_kwargs,
                )
            elif self.username and self.password:
                self._client = OpenSearch(
                    http_auth=(self.username, self.password), **common_kwargs
                )
            else:
                self._client = OpenSearch(**common_kwargs)
        else:
            raise ConfigError(
                f"OPENSEARCH_AUTH must be 'sigv4', 'basic', 'token', or unset "
                f"(got: {auth_mode!r})"
            )

    def _build_sigv4_auth(self) -> Any:
        """Build AWSV4SignerAuth using boto3 credential resolution.

        Raises:
            ConfigError: If boto3 is not installed or AWS credentials not found.
        """
        if AWSV4SignerAuth is None:
            raise ConfigError(
                "opensearchpy.AWSV4SignerAuth is unavailable — upgrade "
                "opensearch-py: pip install 'kibana-mcp[opensearch]'"
            )
        try:
            import boto3
        except ImportError as exc:
            raise ConfigError(
                "boto3 is required for SigV4 auth. "
                "Install it with: pip install 'kibana-mcp[opensearch]'"
            ) from exc

        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise ConfigError(
                "AWS_REGION is set but boto3 could not find AWS credentials. "
                "Set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY, or use an IAM instance role."
            )

        return AWSV4SignerAuth(credentials, self.aws_region, "es")

    def _build_bearer_auth(self) -> str:
        """Return the raw API key (no prefix).

        NOTE: The caller is responsible for passing this as
        ``headers={"Authorization": f"Bearer {self._build_bearer_auth()}"}``
        to the OpenSearch() constructor. Do NOT pass it via ``http_auth`` —
        opensearch-py only accepts tuple or AWSV4SignerAuth there; a bare
        string is silently dropped.
        """
        return self.api_key

    # ── Duck-type methods (same signatures as KibanaClient) ─────────────────

    def get_es(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET an OpenSearch endpoint; return parsed JSON."""
        return self._client.transport.perform_request(
            "GET",
            path,
            params=params,
        )

    def post_es(self, path: str, body: dict[str, Any]) -> Any:
        """POST a JSON body to an OpenSearch endpoint; return parsed JSON."""
        return self._client.transport.perform_request(
            "POST",
            path,
            body=body,
        )

    def get_kibana(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET a Kibana REST endpoint; return parsed JSON.

        Dashboard tools keep using the Kibana REST path regardless of mode —
        ``OPENSEARCH_MODE`` affects only ES-endpoint calls. Delegates to a
        lazily constructed :class:`kibana_mcp.client.KibanaClient`, which
        requires ``KIBANA_URL`` to be set.

        Raises:
            ConfigError: If ``KIBANA_URL`` is not set — Kibana Saved Objects
                API is not part of OpenSearch, so dashboard tools need an
                explicit Kibana endpoint even in OpenSearch mode.
        """
        if self._kibana is None:
            if not os.environ.get("KIBANA_URL", "").strip():
                raise ConfigError(
                    "Kibana dashboard tools require KIBANA_URL even when "
                    "OPENSEARCH_MODE=true — set KIBANA_URL, or avoid the "
                    "dashboard tools in OpenSearch mode"
                )
            from kibana_mcp.client import KibanaClient  # noqa: PLC0415

            self._kibana = KibanaClient()
        return self._kibana.get_kibana(path, params=params)

    def close(self) -> None:
        """Close the underlying OpenSearch connection (and the lazy
        KibanaClient, if dashboard tools constructed one)."""
        if getattr(self, "_kibana", None) is not None:
            try:
                self._kibana.close()
            except Exception:
                pass
            self._kibana = None
        if hasattr(self, "_client"):
            try:
                self._client.close()
            except Exception:
                pass
