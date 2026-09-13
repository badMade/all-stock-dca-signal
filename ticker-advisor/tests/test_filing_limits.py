"""SEC document downloads and excerpt preprocessing have explicit resource bounds."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import signals


_DOCUMENT_LIMIT = 2 * 1024 * 1024


def _hits(count=1):
    return {"hits": {"hits": [
        {"_id": f"0001193125-26-012345:filing-{index}.htm",
         "_source": {"ciks": ["123"], "display_names": ["ACME Corp (ACME)"], "file_type": "10-K"}}
        for index in range(count)
    ]}}


class _StreamedResponse:
    def __init__(self, chunks, *, headers=None, encoding="utf-8", error=None):
        self.chunks = chunks
        self.headers = headers or {}
        self.encoding = encoding
        self.status_code = 200
        self.error = error
        self.closed = False
        self.read_chunks = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def iter_content(self, chunk_size):
        assert 0 < chunk_size <= 65536
        for chunk in self.chunks:
            self.read_chunks += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    @property
    def text(self):
        raise AssertionError("The entire unbounded response must not be materialized")


def _install_responses(monkeypatch, responses):
    import requests

    pending = iter(responses)
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        response = next(pending)
        hook = kwargs.get("hooks", {}).get("response")
        if hook is not None:
            hook(response)
        return response

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(signals._sec_rate_limiter, "acquire", lambda: None)
    return calls


def test_normal_document_stream_preserves_excerpt_encoding_timeout_and_cleanup(monkeypatch):
    response = _StreamedResponse(
        [b"<html><head>hidden</head><body><p>Profit ", b"caf\xe9</p></body></html>"],
        encoding="iso-8859-1",
    )
    calls = _install_responses(monkeypatch, [response])
    errors = []
    items = signals.fetch_sec_filings("ACME", fetcher=_hits, errors=errors, timeout=7.0)
    assert [item["text"] for item in items] == ["Profit café"]
    assert errors == []
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["timeout"] == 7.0
    assert "User-Agent" in calls[0][1]["headers"]
    assert response.closed


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}, {"Content-Length": "invalid"}])
def test_streamed_overflow_stops_reading_closes_and_keeps_other_filings(monkeypatch, headers):
    oversized = _StreamedResponse(
        [b"x" * 65536] * (_DOCUMENT_LIMIT // 65536) + [b"overflow", AssertionError("must not read another chunk")],
        headers=headers,
    )
    normal = _StreamedResponse([b"<body>Normal issuer earnings.</body>"])
    _install_responses(monkeypatch, [oversized, normal])
    errors = []
    items = signals.fetch_sec_filings("ACME", fetcher=lambda: _hits(2), errors=errors)
    assert [item["text"] for item in items] == ["Normal issuer earnings."]
    assert len(errors) == 1 and "exceeds" in errors[0] and "byte" in errors[0]
    assert oversized.read_chunks == _DOCUMENT_LIMIT // 65536 + 1
    assert oversized.closed and normal.closed


def test_oversized_content_length_refuses_before_reading_and_closes(monkeypatch):
    response = _StreamedResponse([AssertionError("must not read body")],
                                 headers={"Content-Length": str(_DOCUMENT_LIMIT + 1)})
    _install_responses(monkeypatch, [response])
    errors = []
    assert signals.fetch_sec_filings("ACME", fetcher=_hits, errors=errors) == []
    assert len(errors) == 1 and "exceeds" in errors[0]
    assert response.read_chunks == 0
    assert response.closed


@pytest.mark.parametrize("status_error", [False, True])
def test_failed_stream_or_status_closes_connection(monkeypatch, status_error):
    error = RuntimeError("temporary SEC failure")
    response = _StreamedResponse([error], error=error if status_error else None)
    _install_responses(monkeypatch, [response])
    errors = []
    assert signals.fetch_sec_filings("ACME", fetcher=_hits, errors=errors) == []
    assert len(errors) == 1 and "temporary SEC failure" in errors[0]
    assert response.closed


def test_redirect_is_refused_before_requests_materializes_its_body(monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter

    class NoBodyRead:
        closed = False

        def read(self, *args, **kwargs):
            raise AssertionError("redirect body must not be materialized")

        def close(self):
            self.closed = True

        def release_conn(self):
            pass

    raw = NoBodyRead()

    def fake_send(_adapter, request, **kwargs):
        response = requests.Response()
        response.status_code = 302
        response.headers["Location"] = "https://www.sec.gov/redirect-target"
        response.url = request.url
        response.request = request
        response.raw = raw
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    monkeypatch.setattr(signals._sec_rate_limiter, "acquire", lambda: None)
    errors = []
    assert signals.fetch_sec_filings("ACME", fetcher=_hits, errors=errors) == []
    assert len(errors) == 1 and "redirect refused" in errors[0]
    assert raw.closed


def test_exact_byte_limit_is_accepted_and_excerpt_remains_bounded(monkeypatch):
    body = b"<body>" + b"x" * (_DOCUMENT_LIMIT - 13) + b"</body>"
    response = _StreamedResponse([body[start:start + 65536] for start in range(0, len(body), 65536)])
    _install_responses(monkeypatch, [response])
    errors = []
    items = signals.fetch_sec_filings("ACME", fetcher=_hits, errors=errors)
    assert [item["text"] for item in items] == ["x" * 4000]
    assert not errors
    assert response.closed


def test_large_injected_cleaner_input_refuses_before_preprocessing(monkeypatch):
    class NoCleaning:
        def sub(self, *args):
            raise AssertionError("Oversized input must be rejected before cleaning")

    monkeypatch.setattr(signals, "_WHITESPACE_RUN_PATTERN", NoCleaning())
    with pytest.raises(ValueError, match="exceeds.*character"):
        signals._extract_filing_snippet("x" * (_DOCUMENT_LIMIT + 1))


def test_oversized_injected_document_is_reported_without_aborting_other_hits():
    documents = iter(["x" * (_DOCUMENT_LIMIT + 1), "<body>Usable second filing.</body>"])
    errors = []
    items = signals.fetch_sec_filings("ACME", fetcher=lambda: _hits(2),
                                     document_fetcher=lambda _url: next(documents), errors=errors)
    assert [item["text"] for item in items] == ["Usable second filing."]
    assert len(errors) == 1 and "exceeds" in errors[0]


@pytest.mark.parametrize("token", ["<", "<script>", "<body>"])
def test_unclosed_markup_preprocessing_finishes_within_bounded_time(token):
    # A subprocess timeout makes the original quadratic scans fail promptly,
    # without timing a fast function against a fragile microbenchmark limit.
    code = (
        "import sys\n"
        "from signals import _extract_filing_snippet\n"
        "token = sys.argv[1]\n"
        "result = _extract_filing_snippet(token * (500_000 if token == '<' else 100_000))\n"
        "assert result == ('<' * 4000 if token == '<' else None)\n"
    )
    env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    subprocess.run([sys.executable, "-c", code, token], cwd=Path(signals.__file__).parent,
                   env=env, check=True, capture_output=True, timeout=5)
