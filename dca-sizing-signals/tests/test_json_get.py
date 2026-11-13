"""Fail-closed tests for market_data_ingest/_json_get.py, the stdlib GET + strict JSON seam
shared by edgar_fundamentals.py and dolthub_options.py.

Added after the 2026-11-12 implementation (PR #2242 listed these paths as untested). No
network: urllib.request.urlopen is patched throughout.
"""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from market_data_ingest._json_get import DEFAULT_TIMEOUT_S, get_json_object, parse_strict_json_object

URLOPEN = "market_data_ingest._json_get.urllib.request.urlopen"


def _response(body: str) -> MagicMock:
    response = MagicMock()
    response.read.return_value = body.encode("utf-8")
    response.__enter__.return_value = response
    return response


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.test/x", code, "status", {}, io.BytesIO(body))


# ---------------------------------------------------------------- parse_strict_json_object


def test_parses_a_plain_object():
    assert parse_strict_json_object('{"a": 1, "b": [2]}', source="T") == {"a": 1, "b": [2]}


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1, "a": 2}',  # duplicate key: last-wins would silently pick one value
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": -Infinity}',
        '{"a": 1',  # truncated mid-document
        "not json",
        "",
    ],
)
def test_rejects_non_strict_json(text):
    with pytest.raises(ValueError, match="T response rejected by strict JSON parsing"):
        parse_strict_json_object(text, source="T")


@pytest.mark.parametrize("text", ["[]", "[{}]", '"s"', "1", "null", "true"])
def test_rejects_a_top_level_that_is_not_an_object(text):
    with pytest.raises(ValueError, match="T response is not a JSON object"):
        parse_strict_json_object(text, source="T")


def test_error_message_does_not_echo_the_body():
    # .jules/sentinel.md: an external body never lands in an exception message.
    secret_looking = '{"token": "sk-live-abc", "token": "sk-live-def"}'
    with pytest.raises(ValueError) as excinfo:
        parse_strict_json_object(secret_looking, source="T")
    assert "sk-live" not in str(excinfo.value)


# ---------------------------------------------------------------- get_json_object


def test_success_sends_headers_and_explicit_timeout():
    with patch(URLOPEN, return_value=_response('{"ok": true}')) as urlopen:
        result = get_json_object("https://example.test/x", source="T", headers={"User-Agent": "UA x@y.z"})
    assert result == {"ok": True}
    request = urlopen.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.full_url == "https://example.test/x"
    assert request.get_header("User-agent") == "UA x@y.z"
    assert urlopen.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT_S


def test_custom_timeout_is_passed_through():
    with patch(URLOPEN, return_value=_response("{}")) as urlopen:
        get_json_object("https://example.test/x", source="T", timeout_s=2.5)
    assert urlopen.call_args.kwargs["timeout"] == 2.5


def test_404_becomes_lookup_error_chained_to_the_http_error():
    with patch(URLOPEN, side_effect=_http_error(404)):
        with pytest.raises(LookupError, match="T returned 404") as excinfo:
            get_json_object("https://example.test/x", source="T")
    assert isinstance(excinfo.value.__cause__, urllib.error.HTTPError)


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500, 503])
def test_other_http_errors_propagate_unchanged_and_are_not_retried(code):
    with patch(URLOPEN, side_effect=_http_error(code)) as urlopen:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get_json_object("https://example.test/x", source="T")
    assert excinfo.value.code == code
    assert not isinstance(excinfo.value, LookupError)
    assert urlopen.call_count == 1


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), urllib.error.URLError("refused")])
def test_transport_failures_propagate_and_are_not_retried(exc):
    with patch(URLOPEN, side_effect=exc) as urlopen:
        with pytest.raises(OSError):
            get_json_object("https://example.test/x", source="T")
    assert urlopen.call_count == 1


def test_malformed_body_fails_closed():
    with patch(URLOPEN, return_value=_response('{"a": 1, "a": 2}')):
        with pytest.raises(ValueError, match="strict JSON"):
            get_json_object("https://example.test/x", source="T")


def test_non_utf8_body_fails_closed():
    response = _response("")
    response.read.return_value = b"\xff\xfe{}"
    with patch(URLOPEN, return_value=response):
        with pytest.raises(ValueError):  # UnicodeDecodeError is a ValueError
            get_json_object("https://example.test/x", source="T")
