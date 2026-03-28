"""Unit tests for watchupdog.webhooks."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from watchupdog.models import FullHealthReport, HealthStatus
from watchupdog.webhooks import (
    _COLOUR,
    _build_discord_payload,
    _build_ntfy_payload,
    _last_fired,
    fire_webhooks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _critical_report() -> FullHealthReport:
    report = FullHealthReport(
        comfyui_url="http://localhost:8189",
        timestamp="2026-01-01 00:00:00 UTC",
    )
    report.overall_status = HealthStatus.CRITICAL
    report.alerts = ["VRAM critical", "Queue overloaded"]
    return report


def _warn_report() -> FullHealthReport:
    report = FullHealthReport(
        comfyui_url="http://localhost:8189",
        timestamp="2026-01-01 00:00:00 UTC",
    )
    report.overall_status = HealthStatus.WARN
    report.alerts = ["High error rate"]
    return report


def _ok_report() -> FullHealthReport:
    report = FullHealthReport(
        comfyui_url="http://localhost:8189",
        timestamp="2026-01-01 00:00:00 UTC",
    )
    report.overall_status = HealthStatus.OK
    report.alerts = []
    return report


# ---------------------------------------------------------------------------
# _build_discord_payload
# ---------------------------------------------------------------------------

def test_discord_payload_has_embeds_key():
    payload = _build_discord_payload(_critical_report())
    assert "embeds" in payload
    assert isinstance(payload["embeds"], list)
    assert len(payload["embeds"]) >= 1


def test_discord_payload_critical_color():
    payload = _build_discord_payload(_critical_report())
    color = payload["embeds"][0]["color"]
    assert color == _COLOUR["CRITICAL"]


def test_discord_payload_warn_color():
    payload = _build_discord_payload(_warn_report())
    color = payload["embeds"][0]["color"]
    assert color == _COLOUR["WARN"]


def test_discord_payload_ok_color():
    payload = _build_discord_payload(_ok_report())
    color = payload["embeds"][0]["color"]
    assert color == _COLOUR["OK"]


def test_discord_payload_title_contains_status():
    payload = _build_discord_payload(_critical_report())
    title = payload["embeds"][0]["title"]
    assert "CRITICAL" in title


def test_discord_payload_alerts_in_fields():
    report = _critical_report()
    payload = _build_discord_payload(report)
    fields = payload["embeds"][0]["fields"]
    alerts_field = next((f for f in fields if f["name"] == "Alerts"), None)
    assert alerts_field is not None
    assert "VRAM critical" in alerts_field["value"]


def test_discord_payload_comfyui_url_in_fields():
    payload = _build_discord_payload(_critical_report())
    fields = payload["embeds"][0]["fields"]
    instance_field = next((f for f in fields if f["name"] == "Instance"), None)
    assert instance_field is not None
    assert "http://localhost:8189" in instance_field["value"]


def test_discord_payload_empty_alerts_shows_none():
    payload = _build_discord_payload(_ok_report())
    fields = payload["embeds"][0]["fields"]
    alerts_field = next((f for f in fields if f["name"] == "Alerts"), None)
    assert alerts_field is not None
    assert "None" in alerts_field["value"]


def test_discord_payload_many_alerts_truncated():
    report = _critical_report()
    report.alerts = [f"Alert {i}" for i in range(12)]
    payload = _build_discord_payload(report)
    fields = payload["embeds"][0]["fields"]
    alerts_field = next(f for f in fields if f["name"] == "Alerts")
    assert "more" in alerts_field["value"]


# ---------------------------------------------------------------------------
# _build_ntfy_payload
# ---------------------------------------------------------------------------

def test_ntfy_payload_critical_priority():
    body, headers = _build_ntfy_payload(_critical_report())
    assert headers["Priority"] == "urgent"


def test_ntfy_payload_warn_priority():
    body, headers = _build_ntfy_payload(_warn_report())
    assert headers["Priority"] == "high"


def test_ntfy_payload_title_contains_status():
    body, headers = _build_ntfy_payload(_critical_report())
    assert "CRITICAL" in headers["Title"]


def test_ntfy_payload_body_contains_url():
    body, headers = _build_ntfy_payload(_critical_report())
    assert "http://localhost:8189" in body


def test_ntfy_payload_body_contains_alerts():
    body, headers = _build_ntfy_payload(_critical_report())
    assert "VRAM critical" in body
    assert "Queue overloaded" in body


def test_ntfy_payload_has_content_type():
    body, headers = _build_ntfy_payload(_critical_report())
    assert headers["Content-Type"] == "text/plain"


def test_ntfy_payload_has_tags():
    body, headers = _build_ntfy_payload(_critical_report())
    assert "Tags" in headers


# ---------------------------------------------------------------------------
# fire_webhooks — returns empty when no URLs
# ---------------------------------------------------------------------------

def test_fire_webhooks_no_urls_returns_empty():
    msgs = fire_webhooks(_critical_report(), discord_url=None, ntfy_url=None)
    assert msgs == []


def test_fire_webhooks_empty_string_url_returns_empty():
    msgs = fire_webhooks(_critical_report(), discord_url="", ntfy_url="")
    assert msgs == []


def test_fire_webhooks_ok_status_does_not_fire():
    # OK status exits before any HTTP call is made; no mock needed
    msgs = fire_webhooks(
        _ok_report(),
        discord_url="https://discord.com/api/webhooks/123/abc",
        ntfy_url=None,
    )
    assert msgs == []


# ---------------------------------------------------------------------------
# fire_webhooks — rate limiting
# ---------------------------------------------------------------------------

def test_fire_webhooks_rate_limit_prevents_double_fire(monkeypatch):
    """Calling twice within min_interval should only fire once."""
    _last_fired.clear()

    call_count = 0
    fake_response = MagicMock()
    fake_response.status_code = 204

    def counting_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return fake_response

    discord_url = "https://discord.com/api/webhooks/999/test-rate-limit"

    with patch("httpx.post", side_effect=counting_post):
        fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=300.0)
        fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=300.0)

    assert call_count == 1


def test_fire_webhooks_rate_limit_allows_after_interval(monkeypatch):
    """After interval expires, a second call should fire."""
    _last_fired.clear()

    call_count = 0
    fake_response = MagicMock()
    fake_response.status_code = 204

    def counting_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return fake_response

    discord_url = "https://discord.com/api/webhooks/999/test-rate-allow"

    with patch("httpx.post", side_effect=counting_post):
        fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)
        fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    assert call_count == 2


# ---------------------------------------------------------------------------
# fire_webhooks — Discord POST details
# ---------------------------------------------------------------------------

def test_fire_webhooks_discord_posts_to_correct_url():
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/123/testtoken"
    fake_response = MagicMock()
    fake_response.status_code = 204

    with patch("httpx.post", return_value=fake_response) as mock_post:
        fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args[0][0] == discord_url or call_args.kwargs.get("url") == discord_url


def test_fire_webhooks_discord_payload_has_embeds():
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/123/testtoken2"
    captured = {}
    fake_response = MagicMock()
    fake_response.status_code = 204

    def capture_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return fake_response

    with patch("httpx.post", side_effect=capture_post):
        fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    assert captured.get("url") == discord_url
    assert "embeds" in captured.get("json", {})


def test_fire_webhooks_returns_success_message_on_204():
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/123/testtoken3"
    fake_response = MagicMock()
    fake_response.status_code = 204

    with patch("httpx.post", return_value=fake_response):
        msgs = fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    assert any("Discord" in m for m in msgs)
    assert any("204" in m for m in msgs)


# ---------------------------------------------------------------------------
# fire_webhooks — ntfy POST details
# ---------------------------------------------------------------------------

def test_fire_webhooks_ntfy_headers_include_title_and_priority():
    _last_fired.clear()

    ntfy_url = "https://ntfy.sh/my-test-topic"
    captured = {}
    fake_response = MagicMock()
    fake_response.status_code = 200

    def capture_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["content"] = kwargs.get("content")
        return fake_response

    with patch("httpx.post", side_effect=capture_post):
        fire_webhooks(_critical_report(), discord_url=None, ntfy_url=ntfy_url, min_interval=0.0)

    assert captured.get("url") == ntfy_url
    headers = captured.get("headers", {})
    assert "Title" in headers
    assert "Priority" in headers
    assert headers["Priority"] == "urgent"


def test_fire_webhooks_ntfy_on_warn_only_fires_when_enabled():
    _last_fired.clear()

    ntfy_url = "https://ntfy.sh/my-warn-topic"
    fake_response = MagicMock()
    fake_response.status_code = 200

    # on_warn=False (default) — should NOT fire for WARN
    msgs = fire_webhooks(_warn_report(), discord_url=None, ntfy_url=ntfy_url, on_warn=False, min_interval=0.0)
    assert msgs == []

    # on_warn=True — should fire for WARN
    _last_fired.clear()
    with patch("httpx.post", return_value=fake_response):
        msgs = fire_webhooks(_warn_report(), discord_url=None, ntfy_url=ntfy_url, on_warn=True, min_interval=0.0)
    assert len(msgs) > 0


# ---------------------------------------------------------------------------
# fire_webhooks — fault injection: HTTP errors, timeouts, dropped connections
# ---------------------------------------------------------------------------

def test_fire_webhooks_discord_http_500_returns_error_message():
    """Discord returns HTTP 500 — fire_webhooks logs the error, does not raise."""
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/999/fault-500"
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.content = b"Internal Server Error"

    with patch("httpx.post", return_value=fake_response):
        msgs = fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    assert len(msgs) == 1
    assert "500" in msgs[0]
    assert "Discord" in msgs[0]


def test_fire_webhooks_discord_timeout_returns_error_message():
    """Discord POST times out — caught as Exception, returns error message."""
    import httpx as _httpx
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/999/fault-timeout"

    with patch("httpx.post", side_effect=_httpx.TimeoutException("timed out")):
        msgs = fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    assert len(msgs) == 1
    assert "Discord" in msgs[0]
    assert "error" in msgs[0].lower() or "timed" in msgs[0].lower()


def test_fire_webhooks_discord_connection_error_returns_error_message():
    """Discord POST fails with ConnectError — caught, returns error message."""
    import httpx as _httpx
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/999/fault-connect"

    with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
        msgs = fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    assert len(msgs) == 1
    assert "Discord" in msgs[0]


def test_fire_webhooks_discord_read_error_returns_error_message():
    """Dropped connection mid-response (ReadError) — caught, returns error message."""
    import httpx as _httpx
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/999/fault-read"

    with patch("httpx.post", side_effect=_httpx.ReadError("connection dropped")):
        msgs = fire_webhooks(_critical_report(), discord_url=discord_url, ntfy_url=None, min_interval=0.0)

    assert len(msgs) == 1
    assert "Discord" in msgs[0]


def test_fire_webhooks_ntfy_http_500_returns_error_message():
    """ntfy returns HTTP 500 — logged, no exception raised."""
    _last_fired.clear()

    ntfy_url = "https://ntfy.sh/fault-500-topic"
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.content = b"Server Error"

    with patch("httpx.post", return_value=fake_response):
        msgs = fire_webhooks(_critical_report(), discord_url=None, ntfy_url=ntfy_url, min_interval=0.0)

    assert len(msgs) == 1
    assert "500" in msgs[0]
    assert "ntfy" in msgs[0]


def test_fire_webhooks_ntfy_timeout_returns_error_message():
    """ntfy POST times out — caught, returns error message."""
    import httpx as _httpx
    _last_fired.clear()

    ntfy_url = "https://ntfy.sh/fault-timeout-topic"

    with patch("httpx.post", side_effect=_httpx.TimeoutException("timed out")):
        msgs = fire_webhooks(_critical_report(), discord_url=None, ntfy_url=ntfy_url, min_interval=0.0)

    assert len(msgs) == 1
    assert "ntfy" in msgs[0]


def test_fire_webhooks_ntfy_read_error_returns_error_message():
    """ntfy dropped connection mid-response — caught, returns error message."""
    import httpx as _httpx
    _last_fired.clear()

    ntfy_url = "https://ntfy.sh/fault-read-topic"

    with patch("httpx.post", side_effect=_httpx.ReadError("drop")):
        msgs = fire_webhooks(_critical_report(), discord_url=None, ntfy_url=ntfy_url, min_interval=0.0)

    assert len(msgs) == 1
    assert "ntfy" in msgs[0]


def test_fire_webhooks_both_urls_independent_faults():
    """Discord times out but ntfy succeeds — both results in the message list."""
    import httpx as _httpx
    _last_fired.clear()

    discord_url = "https://discord.com/api/webhooks/999/fault-mixed"
    ntfy_url = "https://ntfy.sh/fault-mixed-topic"

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.content = b""

    call_count = 0

    def selective_post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "discord" in url:
            raise _httpx.TimeoutException("timeout")
        return ok_response

    with patch("httpx.post", side_effect=selective_post):
        msgs = fire_webhooks(
            _critical_report(),
            discord_url=discord_url,
            ntfy_url=ntfy_url,
            min_interval=0.0,
        )

    assert call_count == 2
    assert len(msgs) == 2
    discord_msg = next(m for m in msgs if "Discord" in m)
    ntfy_msg = next(m for m in msgs if "ntfy" in m)
    assert "error" in discord_msg.lower() or "timeout" in discord_msg.lower()
    assert "200" in ntfy_msg
