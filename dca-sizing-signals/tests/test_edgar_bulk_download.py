"""Tests for ``download_companyfacts_zip`` in the not-yet-written part of market_data_ingest/edgar_bulk.py.

Written BEFORE the implementation (TDD), the download half of step 3 of docs/fundamentals_feed_scope.md.
Do not implement until these are reviewed and approved.

Behaviour pinned here, all derived from the repo's rules and the scope document, not from code:

* One GET of the SEC's nightly bulk file, with the contact-identifying User-Agent the repo already requires
  (no default ships, a blank one is refused BEFORE any request) and an explicit timeout.
* The file is streamed to a temporary file beside the destination and renamed into place only after it is
  complete (length matches the server's Content-Length), is under a size cap, and is a real zip. Any failure
  leaves an existing destination file exactly as it was and leaves no temporary file behind.
* A 404 is ``LookupError`` and other HTTP errors and network errors propagate unchanged (the same rules as
  ``_json_get``). No response text is ever copied into an exception message (.jules/sentinel.md).

The URL is the SEC's documented location as the author remembers it. It was NOT confirmed against a live
request (the SEC refuses a request without a contact User-Agent, and none is configured in the build
environment). Confirm with one live download on the operator's Mac before relying on it.
"""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from market_data_ingest.edgar_bulk import (
    COMPANYFACTS_ZIP_URL,
    download_companyfacts_zip,
    read_bulk_companyfacts,
)

UA = "Acme Research ops@acme.example"
URLOPEN = "market_data_ingest.edgar_bulk.urllib.request.urlopen"


def zip_bytes() -> bytes:
    buffer = io.BytesIO()
    document = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
        {"start": None, "end": "2025-12-31", "val": 5, "fy": 2025, "fp": "FY", "form": "10-K",
         "filed": "2026-02-11", "frame": None, "accn": "A-1"}]}}}}}
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CIK" + "111".zfill(10) + ".json", json.dumps(document))
    return buffer.getvalue()


class FakeResponse:
    """The slice of an HTTP response the downloader may use: ``read(n)``, ``headers`` and context management."""

    def __init__(self, body: bytes, *, content_length: str | None = "auto"):
        self._buffer = io.BytesIO(body)
        self.headers = {} if content_length is None else {
            "Content-Length": str(len(body)) if content_length == "auto" else content_length
        }

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def temp_files(directory: Path, keep: str) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != keep)


# --- the happy path ----------------------------------------------------------------------------------------


def test_the_download_is_written_to_the_destination_and_its_size_returned(tmp_path):
    body = zip_bytes()
    dest = tmp_path / "companyfacts.zip"
    with patch(URLOPEN, return_value=FakeResponse(body)):
        written = download_companyfacts_zip(dest, user_agent=UA)
    assert written == len(body)
    assert dest.read_bytes() == body
    assert temp_files(tmp_path, "companyfacts.zip") == []


def test_the_downloaded_file_can_be_read_by_the_bulk_reader(tmp_path):
    dest = tmp_path / "companyfacts.zip"
    with patch(URLOPEN, return_value=FakeResponse(zip_bytes())):
        download_companyfacts_zip(dest, user_agent=UA)
    result = read_bulk_companyfacts(dest, ciks=["111"])
    assert result.facts["111".zfill(10)]["us-gaap:Assets"][0].val == 5


def test_one_get_goes_to_the_sec_with_the_user_agent_and_an_explicit_timeout(tmp_path):
    with patch(URLOPEN, return_value=FakeResponse(zip_bytes())) as urlopen:
        download_companyfacts_zip(tmp_path / "f.zip", user_agent=UA, timeout_s=17)
    assert urlopen.call_count == 1
    request = urlopen.call_args.args[0]
    assert request.full_url == COMPANYFACTS_ZIP_URL
    assert request.get_method() == "GET"
    assert request.get_header("User-agent") == UA
    assert urlopen.call_args.kwargs["timeout"] == 17


def test_missing_parent_directories_are_created(tmp_path):
    dest = tmp_path / "a" / "b" / "companyfacts.zip"
    with patch(URLOPEN, return_value=FakeResponse(zip_bytes())):
        download_companyfacts_zip(dest, user_agent=UA)
    assert dest.exists()


def test_a_server_that_sends_no_content_length_is_still_accepted(tmp_path):
    body = zip_bytes()
    dest = tmp_path / "companyfacts.zip"
    with patch(URLOPEN, return_value=FakeResponse(body, content_length=None)):
        assert download_companyfacts_zip(dest, user_agent=UA) == len(body)


# --- the User-Agent rule --------------------------------------------------------------------------------------


@pytest.mark.parametrize("user_agent", [None, "", "   "])
def test_a_missing_or_blank_user_agent_is_refused_before_any_request(tmp_path, monkeypatch, user_agent):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    dest = tmp_path / "companyfacts.zip"
    with patch(URLOPEN) as urlopen:
        with pytest.raises(ValueError):
            download_companyfacts_zip(dest, user_agent=user_agent)
    urlopen.assert_not_called()
    assert not dest.exists()


def test_the_user_agent_may_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", UA)
    with patch(URLOPEN, return_value=FakeResponse(zip_bytes())) as urlopen:
        download_companyfacts_zip(tmp_path / "f.zip")
    assert urlopen.call_args.args[0].get_header("User-agent") == UA


# --- a failed download never damages what is already there -------------------------------------------------


def assert_untouched(tmp_path: Path, dest: Path) -> None:
    assert dest.read_bytes() == b"old"
    assert temp_files(tmp_path, dest.name) == []


@pytest.fixture
def existing(tmp_path):
    dest = tmp_path / "companyfacts.zip"
    dest.write_bytes(b"old")
    return tmp_path, dest


def test_a_body_that_is_not_a_zip_is_refused(existing):
    tmp_path, dest = existing
    with patch(URLOPEN, return_value=FakeResponse(b"<html>blocked</html>")):
        with pytest.raises(ValueError, match="zip"):
            download_companyfacts_zip(dest, user_agent=UA)
    assert_untouched(tmp_path, dest)


def test_a_truncated_download_is_refused(existing):
    tmp_path, dest = existing
    body = zip_bytes()
    with patch(URLOPEN, return_value=FakeResponse(body, content_length=str(len(body) + 50))):
        with pytest.raises(ValueError, match="length"):
            download_companyfacts_zip(dest, user_agent=UA)
    assert_untouched(tmp_path, dest)


def test_a_download_over_the_size_cap_is_refused(existing):
    tmp_path, dest = existing
    with patch(URLOPEN, return_value=FakeResponse(zip_bytes())):
        with pytest.raises(ValueError, match="cap"):
            download_companyfacts_zip(dest, user_agent=UA, max_bytes=10)
    assert_untouched(tmp_path, dest)


def test_a_404_is_a_lookup_error(existing):
    tmp_path, dest = existing
    error = urllib.error.HTTPError(COMPANYFACTS_ZIP_URL, 404, "Not Found", {}, None)
    with patch(URLOPEN, side_effect=error):
        with pytest.raises(LookupError):
            download_companyfacts_zip(dest, user_agent=UA)
    assert_untouched(tmp_path, dest)


def test_other_http_errors_propagate_unchanged(existing):
    tmp_path, dest = existing
    error = urllib.error.HTTPError(COMPANYFACTS_ZIP_URL, 403, "Forbidden", {}, None)
    with patch(URLOPEN, side_effect=error):
        with pytest.raises(urllib.error.HTTPError):
            download_companyfacts_zip(dest, user_agent=UA)
    assert_untouched(tmp_path, dest)


def test_a_network_failure_propagates_as_oserror(existing):
    tmp_path, dest = existing
    with patch(URLOPEN, side_effect=urllib.error.URLError("unreachable")):
        with pytest.raises(OSError):
            download_companyfacts_zip(dest, user_agent=UA)
    assert_untouched(tmp_path, dest)


def test_no_response_text_is_copied_into_an_error_message(existing):
    tmp_path, dest = existing
    with patch(URLOPEN, return_value=FakeResponse(b"SECRET-BODY-TEXT")):
        with pytest.raises(ValueError) as excinfo:
            download_companyfacts_zip(dest, user_agent=UA)
    assert "SECRET-BODY-TEXT" not in str(excinfo.value)
