"""
Fault injection tests for the urllib-based HTTP calls in env_checks and
interactive_menu.  Each call site is exercised against:

  - timeout / socket.timeout
  - HTTP 500 (urllib.error.HTTPError)
  - malformed JSON (valid HTTP 200 but body is garbage)
  - dropped connection mid-response (http.client.RemoteDisconnected)

All tests use unittest.mock so no live server is required.
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import urllib.error
import urllib.request
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_urlopen_200(body: bytes):
    """Return a mock that urllib.request.urlopen returns (context manager)."""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@contextmanager
def _mock_socket_listening():
    """Patch socket.create_connection so _check_port_process thinks port is open."""
    sock = MagicMock()
    sock.__enter__ = lambda s: s
    sock.__exit__ = MagicMock(return_value=False)
    with patch("socket.create_connection", return_value=sock):
        yield


# ---------------------------------------------------------------------------
# env_checks._check_port_process — urllib /system_stats fault injection
# ---------------------------------------------------------------------------

from watchupdog.env_checks import _check_port_process, STATUS_OK, STATUS_WARN, STATUS_FAIL


def _rows_by_check(rows, check_name: str):
    return [r for r in rows if r.check == check_name]


def test_env_check_port_open_valid_json():
    """Baseline: open port + valid JSON → STATUS_OK for /system_stats row."""
    body = json.dumps({"devices": [], "cpu_utilization": 0}).encode()
    with _mock_socket_listening():
        with patch("urllib.request.urlopen", return_value=_fake_urlopen_200(body)):
            rows = _check_port_process("http://127.0.0.1:8188")

    stats_rows = _rows_by_check(rows, "ComfyUI /system_stats")
    assert stats_rows, "Expected a /system_stats row"
    assert stats_rows[0].status == STATUS_OK


def test_env_check_port_open_http_500():
    """/system_stats returns HTTP 500 — exception caught, row is WARN."""
    with _mock_socket_listening():
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://127.0.0.1:8188/system_stats", 500,
                "Internal Server Error", {}, None,
            ),
        ):
            rows = _check_port_process("http://127.0.0.1:8188")

    stats_rows = _rows_by_check(rows, "ComfyUI /system_stats")
    assert stats_rows, "Expected a /system_stats row"
    assert stats_rows[0].status == STATUS_WARN
    assert "failed" in stats_rows[0].detail.lower() or "500" in stats_rows[0].detail


def test_env_check_port_open_timeout():
    """/system_stats times out — exception caught, row is WARN."""
    with _mock_socket_listening():
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket.timeout("timed out")),
        ):
            rows = _check_port_process("http://127.0.0.1:8188")

    stats_rows = _rows_by_check(rows, "ComfyUI /system_stats")
    assert stats_rows
    assert stats_rows[0].status == STATUS_WARN


def test_env_check_port_open_malformed_json():
    """/system_stats returns 200 but non-JSON body — JSONDecodeError caught, row is WARN."""
    with _mock_socket_listening():
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen_200(b"{{not valid json!!!"),
        ):
            rows = _check_port_process("http://127.0.0.1:8188")

    stats_rows = _rows_by_check(rows, "ComfyUI /system_stats")
    assert stats_rows
    assert stats_rows[0].status == STATUS_WARN


def test_env_check_port_open_dropped_connection():
    """/system_stats drops connection mid-response — RemoteDisconnected caught, row is WARN."""
    with _mock_socket_listening():
        with patch(
            "urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("connection dropped"),
        ):
            rows = _check_port_process("http://127.0.0.1:8188")

    stats_rows = _rows_by_check(rows, "ComfyUI /system_stats")
    assert stats_rows
    assert stats_rows[0].status == STATUS_WARN


def test_env_check_port_closed():
    """Port not open — no urlopen called, STATUS_FAIL row for port."""
    with patch("socket.create_connection", side_effect=ConnectionRefusedError("refused")):
        rows = _check_port_process("http://127.0.0.1:8188")

    port_rows = _rows_by_check(rows, "Port 8188")
    assert port_rows
    assert port_rows[0].status == STATUS_FAIL
    # No /system_stats row when port is closed
    assert not _rows_by_check(rows, "ComfyUI /system_stats")


# ---------------------------------------------------------------------------
# interactive_menu._fetch_comfyui_python — urllib fault injection
# ---------------------------------------------------------------------------

from watchupdog.interactive_menu import _fetch_comfyui_python


def test_fetch_comfyui_python_success():
    """Valid JSON with python_version — returns version string."""
    body = json.dumps({"python_version": "3.11.5 (tags/...)"}).encode()
    with patch("urllib.request.urlopen", return_value=_fake_urlopen_200(body)):
        result = _fetch_comfyui_python("http://127.0.0.1:8188")
    assert result == "3.11.5"


def test_fetch_comfyui_python_timeout():
    """URLError(timeout) — returns None, no exception propagated."""
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(socket.timeout("timed out")),
    ):
        result = _fetch_comfyui_python("http://127.0.0.1:8188")
    assert result is None


def test_fetch_comfyui_python_http_500():
    """HTTPError 500 — returns None."""
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "http://127.0.0.1:8188/system_stats", 500, "Error", {}, None
        ),
    ):
        result = _fetch_comfyui_python("http://127.0.0.1:8188")
    assert result is None


def test_fetch_comfyui_python_malformed_json():
    """200 OK but non-JSON body — JSONDecodeError caught, returns None."""
    with patch("urllib.request.urlopen", return_value=_fake_urlopen_200(b"[[[bad json")):
        result = _fetch_comfyui_python("http://127.0.0.1:8188")
    assert result is None


def test_fetch_comfyui_python_dropped_connection():
    """RemoteDisconnected mid-response — returns None."""
    with patch(
        "urllib.request.urlopen",
        side_effect=http.client.RemoteDisconnected("drop"),
    ):
        result = _fetch_comfyui_python("http://127.0.0.1:8188")
    assert result is None


def test_fetch_comfyui_python_missing_version_field():
    """200 OK valid JSON but no python_version field — returns None."""
    body = json.dumps({"devices": [], "cpu_utilization": 0.0}).encode()
    with patch("urllib.request.urlopen", return_value=_fake_urlopen_200(body)):
        result = _fetch_comfyui_python("http://127.0.0.1:8188")
    assert result is None


# ---------------------------------------------------------------------------
# interactive_menu._probe_connectivity — urllib fault injection
# ---------------------------------------------------------------------------

from watchupdog.interactive_menu import _probe_connectivity, _conn_state


def test_probe_connectivity_success():
    """Successful response — _conn_state[url] = True."""
    url = "http://127.0.0.1:18200"
    _conn_state.pop(url, None)

    with patch("urllib.request.urlopen", return_value=MagicMock()):
        _probe_connectivity(url)

    assert _conn_state.get(url) is True


def test_probe_connectivity_timeout():
    """Timeout — _conn_state[url] = False, no exception propagated."""
    url = "http://127.0.0.1:18201"
    _conn_state.pop(url, None)

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(socket.timeout("timed out")),
    ):
        _probe_connectivity(url)

    assert _conn_state.get(url) is False


def test_probe_connectivity_http_500():
    """HTTP 500 — _conn_state[url] = False."""
    url = "http://127.0.0.1:18202"
    _conn_state.pop(url, None)

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            f"{url}/system_stats", 500, "Error", {}, None
        ),
    ):
        _probe_connectivity(url)

    assert _conn_state.get(url) is False


def test_probe_connectivity_connection_refused():
    """Connection refused — _conn_state[url] = False."""
    url = "http://127.0.0.1:18203"
    _conn_state.pop(url, None)

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        _probe_connectivity(url)

    assert _conn_state.get(url) is False


def test_probe_connectivity_dropped_connection():
    """RemoteDisconnected — _conn_state[url] = False."""
    url = "http://127.0.0.1:18204"
    _conn_state.pop(url, None)

    with patch(
        "urllib.request.urlopen",
        side_effect=http.client.RemoteDisconnected("drop"),
    ):
        _probe_connectivity(url)

    assert _conn_state.get(url) is False
