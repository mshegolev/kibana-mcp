"""Unit tests for LogClient and LogHit facade.

Tests cover field extraction (trace_id, service_name, request/response JSON),
dotted-key resolution, missing-field tolerance, timestamp parsing, and both
ES/OS backends without actual HTTP (mocked backend).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from kibana_mcp.log_client import LogClient, LogHit

# ── TestLogHitTimestamp ───────────────────────────────────────────────────────


class TestLogHitTimestamp:
    """Test timestamp_utc parsing from @timestamp field."""

    def test_timestamp_utc_with_z_suffix(self) -> None:
        """ISO string with trailing Z is parsed as UTC-aware datetime."""
        source = {"@timestamp": "2026-06-12T10:00:00Z"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is not None
        assert hit.timestamp_utc.tzinfo is not None
        assert hit.timestamp_utc.tzinfo == timezone.utc
        assert hit.timestamp_utc.year == 2026
        assert hit.timestamp_utc.hour == 10

    def test_timestamp_utc_with_offset(self) -> None:
        """ISO string with +00:00 offset is parsed as UTC-aware datetime."""
        source = {"@timestamp": "2026-06-12T10:00:00+00:00"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is not None
        assert hit.timestamp_utc.tzinfo is not None

    def test_timestamp_utc_missing_is_none(self) -> None:
        """Missing @timestamp field gives timestamp_utc == None."""
        source = {"message": "no timestamp"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is None

    def test_timestamp_utc_malformed_is_none(self) -> None:
        """Malformed timestamp string gives None, no exception raised."""
        source = {"@timestamp": "not-a-date"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is None

    def test_timestamp_utc_nanosecond_precision(self) -> None:
        """9-digit fractional seconds (ES date_nanos) parse, truncated to us."""
        source = {"@timestamp": "2026-01-01T12:00:00.123456789Z"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc == datetime(
            2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc
        )

    def test_timestamp_utc_offset_converted_to_utc(self) -> None:
        """Non-UTC offset (+03:00) is CONVERTED to UTC, not kept as-is."""
        source = {"@timestamp": "2026-01-01T12:00:00+03:00"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc == datetime(
            2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc
        )
        assert hit.timestamp_utc.utcoffset() == timezone.utc.utcoffset(None)

    def test_timestamp_utc_naive_treated_as_utc(self) -> None:
        """Naive ISO string (no tz) yields an AWARE UTC datetime."""
        source = {"@timestamp": "2026-01-01T12:00:00"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc == datetime(
            2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc
        )
        # comparable against aware datetimes — no TypeError
        assert hit.timestamp_utc < datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_timestamp_utc_epoch_ms_int(self) -> None:
        """Numeric epoch-ms @timestamp (ES epoch_millis) parses, no raise."""
        source = {"@timestamp": 1700000000000}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc == datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
        )

    def test_timestamp_utc_epoch_seconds_int(self) -> None:
        """Numeric epoch-seconds @timestamp (< 1e11) parses as seconds."""
        source = {"@timestamp": 1700000000}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc == datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
        )

    def test_timestamp_utc_epoch_ms_float(self) -> None:
        """Float epoch-ms @timestamp parses as UTC-aware datetime."""
        source = {"@timestamp": 1700000000000.0}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is not None
        assert hit.timestamp_utc.tzinfo == timezone.utc
        assert hit.timestamp_utc.year == 2023

    def test_timestamp_utc_numeric_string_epoch_ms(self) -> None:
        """Numeric-string epoch-ms @timestamp parses as epoch."""
        source = {"@timestamp": "1700000000000"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc == datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
        )

    def test_timestamp_utc_dict_is_none(self) -> None:
        """dict @timestamp gives None, no exception raised."""
        source = {"@timestamp": {"nested": "value"}}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is None

    def test_timestamp_utc_list_is_none(self) -> None:
        """list @timestamp gives None, no exception raised."""
        source = {"@timestamp": ["2026-06-12T10:00:00Z"]}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is None

    def test_timestamp_utc_bool_is_none(self) -> None:
        """bool @timestamp gives None (not coerced to epoch 0/1)."""
        source = {"@timestamp": True}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is None

    def test_timestamp_utc_garbage_string_is_none(self) -> None:
        """Arbitrary garbage string gives None, no exception raised."""
        source = {"@timestamp": "🔥🔥🔥 totally not a timestamp 🔥🔥🔥"}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is None

    def test_timestamp_utc_overflow_epoch_is_none(self) -> None:
        """Out-of-range epoch value gives None, no OverflowError."""
        source = {"@timestamp": 1e30}
        hit = LogHit.from_source(source)
        assert hit.timestamp_utc is None


# ── TestLogHitTraceId ─────────────────────────────────────────────────────────


class TestLogHitTraceId:
    """Test trace_id candidate-list resolution."""

    def test_trace_id_dotted_key_form(self) -> None:
        """Dotted key 'trace.id' is resolved when stored as a flat key."""
        source = {"trace.id": "abc"}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "abc"

    def test_trace_id_nested_dict_form(self) -> None:
        """Nested dict {'trace': {'id': 'abc'}} is resolved."""
        source = {"trace": {"id": "abc"}}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "abc"

    def test_trace_id_second_candidate_traceId(self) -> None:
        """Second candidate 'traceId' is used when 'trace.id' absent."""
        source = {"traceId": "xyz"}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "xyz"

    def test_trace_id_third_candidate_trace_id(self) -> None:
        """Third candidate 'trace_id' is used when others absent."""
        source = {"trace_id": "zzz"}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "zzz"

    def test_trace_id_absent_returns_none(self) -> None:
        """No trace field gives trace_id == None."""
        source = {"message": "no trace"}
        hit = LogHit.from_source(source)
        assert hit.trace_id is None

    def test_trace_id_first_candidate_wins(self) -> None:
        """First candidate wins when multiple are present."""
        source = {"trace.id": "first", "traceId": "second"}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "first"

    def test_trace_id_numeric_scalar_coerced_to_str(self) -> None:
        """Numeric trace value is coerced to str."""
        source = {"trace.id": 12345}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "12345"

    def test_trace_id_dict_value_skipped(self) -> None:
        """Non-scalar (dict) trace value is skipped, next candidate wins."""
        source = {"trace.id": {"junk": True}, "traceId": "real"}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "real"

    def test_trace_id_empty_string_falls_through(self) -> None:
        """Empty-string candidate is treated as absent."""
        source = {"trace.id": "", "traceId": "next"}
        hit = LogHit.from_source(source)
        assert hit.trace_id == "next"

    def test_level_dict_value_is_none(self) -> None:
        """Non-scalar level (dict) yields None, not the dict itself."""
        source = {"level": {"name": "ERROR"}}
        hit = LogHit.from_source(source)
        assert hit.level is None

    def test_level_int_coerced_to_str(self) -> None:
        """Numeric level (syslog severity) is coerced to str."""
        source = {"level": 3}
        hit = LogHit.from_source(source)
        assert hit.level == "3"

    def test_message_dict_value_is_none(self) -> None:
        """Non-scalar message (dict) yields None."""
        source = {"message": {"text": "hi"}}
        hit = LogHit.from_source(source)
        assert hit.message is None

    def test_trace_id_custom_candidates_stored_on_client(self) -> None:
        """Custom trace_id_candidates are stored on LogClient instance."""
        mock_backend = MagicMock()
        client = LogClient(
            backend=mock_backend,
            trace_id_candidates=["custom.trace", "trace_key"],
        )
        assert client.trace_id_candidates == ["custom.trace", "trace_key"]


# ── TestLogHitServiceName ─────────────────────────────────────────────────────


class TestLogHitServiceName:
    """Test service_name candidate-list resolution."""

    def test_service_name_dotted_key_form(self) -> None:
        """Dotted key 'service.name' stored as flat key is resolved."""
        source = {"service.name": "api"}
        hit = LogHit.from_source(source)
        assert hit.service_name == "api"

    def test_service_name_nested_dict_form(self) -> None:
        """Nested dict {'service': {'name': 'api'}} is resolved."""
        source = {"service": {"name": "api"}}
        hit = LogHit.from_source(source)
        assert hit.service_name == "api"

    def test_service_name_second_candidate_serviceName(self) -> None:
        """Second candidate 'serviceName' used when 'service.name' absent."""
        source = {"serviceName": "api"}
        hit = LogHit.from_source(source)
        assert hit.service_name == "api"

    def test_service_name_absent_returns_none(self) -> None:
        """No service field gives service_name == None."""
        source = {"message": "no service"}
        hit = LogHit.from_source(source)
        assert hit.service_name is None


# ── TestRequestJsonExtraction ─────────────────────────────────────────────────


class TestRequestJsonExtraction:
    """Test request_json extraction — direct fields, string JSON, nested."""

    def test_request_json_direct_dict(self) -> None:
        """Direct 'request' field as dict is returned as-is."""
        source = {"request": {"method": "POST"}}
        hit = LogHit.from_source(source)
        assert hit.request_json == {"method": "POST"}

    def test_request_json_string_parsed(self) -> None:
        """Direct 'request' field as JSON string is parsed to dict."""
        source = {"request": '{"method": "POST"}'}
        hit = LogHit.from_source(source)
        assert hit.request_json == {"method": "POST"}

    def test_request_json_broken_string_returns_none(self) -> None:
        """Broken JSON string in request field returns None, no exception."""
        source = {"request": "not json"}
        hit = LogHit.from_source(source)
        assert hit.request_json is None

    def test_request_json_http_nested(self) -> None:
        """Nested http.request.body.content path is used as fallback."""
        source = {"http": {"request": {"body": {"content": '{"data": 1}'}}}}
        hit = LogHit.from_source(source)
        assert hit.request_json == {"data": 1}

    def test_request_json_http_dotted_key(self) -> None:
        """Dotted key 'http.request.body.content' is resolved."""
        source = {"http.request.body.content": '{"x": 2}'}
        hit = LogHit.from_source(source)
        assert hit.request_json == {"x": 2}

    def test_request_json_plain_text_request_falls_through_to_body(self) -> None:
        """Plain-text 'request' does not suppress JSON in later candidate."""
        source = {
            "request": "GET /api/v1/orders",
            "http": {"request": {"body": {"content": '{"a": 1}'}}},
        }
        hit = LogHit.from_source(source)
        assert hit.request_json == {"a": 1}

    def test_request_json_non_dict_json_falls_through(self) -> None:
        """JSON that parses to a non-dict (list) does not stop the scan."""
        source = {
            "request": "[1, 2, 3]",
            "http.request.body.content": '{"b": 2}',
        }
        hit = LogHit.from_source(source)
        assert hit.request_json == {"b": 2}

    def test_request_json_absent_returns_none(self) -> None:
        """No request field gives request_json == None."""
        source = {"message": "no request"}
        hit = LogHit.from_source(source)
        assert hit.request_json is None


# ── TestResponseJsonExtraction ────────────────────────────────────────────────


class TestResponseJsonExtraction:
    """Test response_json extraction."""

    def test_response_json_direct_dict(self) -> None:
        """Direct 'response' field as dict is returned as-is."""
        source = {"response": {"status": 200}}
        hit = LogHit.from_source(source)
        assert hit.response_json == {"status": 200}

    def test_response_json_absent_returns_none(self) -> None:
        """No response field gives response_json == None."""
        source = {"message": "no response"}
        hit = LogHit.from_source(source)
        assert hit.response_json is None

    def test_response_json_broken_string_returns_none(self) -> None:
        """Broken JSON string in response field returns None, no exception."""
        source = {"response": "not json"}
        hit = LogHit.from_source(source)
        assert hit.response_json is None

    def test_response_json_http_nested(self) -> None:
        """Nested http.response.body.content path is used as fallback."""
        source = {
            "http": {"response": {"body": {"content": '{"code": 200}'}}}
        }
        hit = LogHit.from_source(source)
        assert hit.response_json == {"code": 200}


# ── TestSearchLogs ────────────────────────────────────────────────────────────


class TestSearchLogs:
    """Test LogClient.search_logs() with mocked backend."""

    def _make_backend(self) -> MagicMock:
        mock_backend = MagicMock()
        mock_backend.post_es.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_id": "1",
                        "_index": "logs",
                        "_score": 1.0,
                        "_source": {
                            "@timestamp": "2026-06-12T10:00:00Z",
                            "message": "ok",
                        },
                    }
                ],
            },
            "took": 5,
        }
        return mock_backend

    def test_search_logs_returns_loghit_list(self) -> None:
        """search_logs() returns a list[LogHit]."""
        client = LogClient(backend=self._make_backend())
        hits = client.search_logs("*", index="logs-*")
        assert isinstance(hits, list)
        assert len(hits) == 1
        assert isinstance(hits[0], LogHit)

    def test_search_logs_calls_post_es_with_correct_path(self) -> None:
        """search_logs() calls backend.post_es with /{index}/_search path."""
        backend = self._make_backend()
        client = LogClient(backend=backend)
        client.search_logs("*", index="logs-*")
        backend.post_es.assert_called_once()
        call_args = backend.post_es.call_args
        assert call_args[0][0] == "/logs-*/_search"

    def test_search_logs_index_outer_whitespace_stripped(self) -> None:
        """Outer whitespace in index is stripped before path interpolation."""
        backend = self._make_backend()
        client = LogClient(backend=backend)
        client.search_logs("*", index=" logs-* ")
        assert backend.post_es.call_args[0][0] == "/logs-*/_search"

    def test_search_logs_index_validation_raises(self) -> None:
        """index with injection chars raises ValueError before backend call."""
        client = LogClient(backend=self._make_backend())
        with pytest.raises(ValueError):
            client.search_logs("*", index="logs/_delete?")

    def test_search_logs_invalid_sort_raises(self) -> None:
        """sort outside {'asc','desc'} raises ValueError before backend call."""
        backend = self._make_backend()
        client = LogClient(backend=backend)
        with pytest.raises(ValueError, match="sort"):
            client.search_logs("*", sort="ascending")
        backend.post_es.assert_not_called()

    @pytest.mark.parametrize("bad_size", [0, -5, 10001])
    def test_search_logs_invalid_size_raises(self, bad_size: int) -> None:
        """size outside 1..10000 raises ValueError before backend call."""
        backend = self._make_backend()
        client = LogClient(backend=backend)
        with pytest.raises(ValueError, match="size"):
            client.search_logs("*", size=bad_size)
        backend.post_es.assert_not_called()

    def test_search_logs_with_datetime_time_from(self) -> None:
        """time_from as datetime object is converted to ISO string."""
        backend = self._make_backend()
        client = LogClient(backend=backend)
        client.search_logs(
            "*", index="logs-*",
            time_from=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        # Backend must be called (no exception from datetime handling)
        backend.post_es.assert_called_once()

    def test_search_logs_fields_only_hit_populates_loghit(self) -> None:
        """Hit without _source but with `fields` (fields API) is extracted."""
        backend = MagicMock()
        backend.post_es.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_id": "1",
                        "_index": "logs",
                        "fields": {
                            "@timestamp": ["2026-06-12T10:00:00Z"],
                            "trace.id": ["abc"],
                            "message": ["ok"],
                        },
                    }
                ],
            },
            "took": 2,
        }
        client = LogClient(backend=backend)
        hits = client.search_logs("*")
        assert len(hits) == 1
        assert hits[0].trace_id == "abc"
        assert hits[0].message == "ok"
        assert hits[0].timestamp_utc == datetime(
            2026, 6, 12, 10, 0, 0, tzinfo=timezone.utc
        )

    def test_search_logs_empty_hits_returns_empty_list(self) -> None:
        """Empty hits list from backend returns []."""
        backend = MagicMock()
        backend.post_es.return_value = {
            "hits": {"total": {"value": 0}, "hits": []},
            "took": 1,
        }
        client = LogClient(backend=backend)
        hits = client.search_logs("*")
        assert hits == []


# ── TestGetRequestResponseJson ────────────────────────────────────────────────


class TestGetRequestResponseJson:
    """Test LogClient.get_request_response_json()."""

    def test_returns_dict_with_request_and_response_keys(self) -> None:
        """Return dict has exactly 'request' and 'response' keys."""
        client = LogClient(backend=MagicMock())
        hit = LogHit.from_source({"request": {"a": 1}, "response": {"b": 2}})
        result = client.get_request_response_json(hit)
        assert set(result.keys()) == {"request", "response"}

    def test_both_none_when_fields_absent(self) -> None:
        """When hit has no request/response, returns None for both."""
        client = LogClient(backend=MagicMock())
        hit = LogHit.from_source({})
        result = client.get_request_response_json(hit)
        assert result == {"request": None, "response": None}

    def test_returns_extracted_dicts_from_hit(self) -> None:
        """Returns exactly the dicts already extracted in hit."""
        client = LogClient(backend=MagicMock())
        hit = LogHit.from_source(
            {"request": '{"method": "GET"}', "response": {"status": 200}}
        )
        result = client.get_request_response_json(hit)
        assert result["request"] == {"method": "GET"}
        assert result["response"] == {"status": 200}


# ── TestLogClientFromEnv ──────────────────────────────────────────────────────


class TestLogClientFromEnv:
    """Test LogClient.from_env() with patched env and constructors."""

    def test_from_env_kibana_mode_selects_kibana_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_MODE=false → backend is KibanaClient instance."""
        monkeypatch.setenv("OPENSEARCH_MODE", "false")
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        from kibana_mcp.client import KibanaClient
        client = LogClient.from_env()
        assert isinstance(client._backend, KibanaClient)

    def test_from_env_opensearch_mode_selects_opensearch_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENSEARCH_MODE=true → backend is OpenSearchClient instance."""
        monkeypatch.setenv("OPENSEARCH_MODE", "true")
        monkeypatch.setenv("OPENSEARCH_URL", "https://os.example.com")
        from kibana_mcp.os_client import OpenSearchClient
        with patch("kibana_mcp.os_client.OpenSearch", return_value=MagicMock()):
            client = LogClient.from_env()
        assert isinstance(client._backend, OpenSearchClient)

    def test_from_env_custom_candidates_passed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom candidate lists are stored on LogClient after from_env."""
        monkeypatch.setenv("OPENSEARCH_MODE", "false")
        monkeypatch.setenv("KIBANA_URL", "https://kibana.example.com")
        client = LogClient.from_env(
            trace_id_candidates=["my.trace"],
            service_name_candidates=["my.service"],
        )
        assert client.trace_id_candidates == ["my.trace"]
        assert client.service_name_candidates == ["my.service"]
