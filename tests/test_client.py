"""Tests for the ComfyUI async HTTP client using httpx mock (respx)."""

from __future__ import annotations

import pytest
import pytest_asyncio

try:
    import respx
    import httpx

    _RESPX_OK = True
except ImportError:
    _RESPX_OK = False

from watchupdog.client import ComfyUIClient

pytestmark = pytest.mark.skipif(not _RESPX_OK, reason="respx not installed")

BASE = "http://127.0.0.1:8189"


@pytest.mark.asyncio
async def test_ping_success():
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(
            return_value=httpx.Response(200, json={"devices": [], "cpu_utilization": 0})
        )
        async with ComfyUIClient(BASE) as client:
            assert await client.ping() is True


@pytest.mark.asyncio
async def test_ping_failure():
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(side_effect=httpx.ConnectError("refused"))
        async with ComfyUIClient(BASE) as client:
            assert await client.ping() is False


@pytest.mark.asyncio
async def test_get_system_stats():
    payload = {"cpu_utilization": 42.5, "devices": [], "ram_total": 0, "ram_used": 0}
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(return_value=httpx.Response(200, json=payload))
        async with ComfyUIClient(BASE) as client:
            data = await client.get_system_stats()
    assert data is not None
    assert data["cpu_utilization"] == 42.5


@pytest.mark.asyncio
async def test_get_queue():
    payload = {"queue_running": [], "queue_pending": []}
    with respx.mock(base_url=BASE) as mock:
        mock.get("/queue").mock(return_value=httpx.Response(200, json=payload))
        async with ComfyUIClient(BASE) as client:
            data = await client.get_queue()
    assert data is not None
    assert data["queue_running"] == []


@pytest.mark.asyncio
async def test_get_history():
    payload = {"prompt-001": {"status": {"messages": []}, "outputs": {}}}
    with respx.mock(base_url=BASE) as mock:
        mock.get("/history").mock(return_value=httpx.Response(200, json=payload))
        async with ComfyUIClient(BASE) as client:
            data = await client.get_history()
    assert data is not None
    assert "prompt-001" in data


@pytest.mark.asyncio
async def test_get_returns_none_on_timeout():
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(side_effect=httpx.TimeoutException("timeout"))
        async with ComfyUIClient(BASE) as client:
            data = await client.get_system_stats()
    assert data is None


@pytest.mark.asyncio
async def test_fetch_all_partial_failure():
    """fetch_all should return Nones for failed endpoints gracefully."""
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(
            return_value=httpx.Response(200, json={"devices": [], "cpu_utilization": 0})
        )
        mock.get("/queue").mock(side_effect=httpx.ConnectError("refused"))
        mock.get("/history").mock(side_effect=httpx.ConnectError("refused"))
        mock.get("/object_info").mock(side_effect=httpx.ConnectError("refused"))

        async with ComfyUIClient(BASE) as client:
            result = await client.fetch_all()

    assert result["system_stats"] is not None
    assert result["queue"] is None
    assert result["history"] is None


@pytest.mark.asyncio
async def test_get_object_info():
    payload = {"KSampler": {"input": {}, "output": []}}
    with respx.mock(base_url=BASE) as mock:
        mock.get("/object_info").mock(return_value=httpx.Response(200, json=payload))
        async with ComfyUIClient(BASE) as client:
            data = await client.get_object_info()
    assert data is not None
    assert "KSampler" in data


@pytest.mark.asyncio
async def test_http_error_returns_none():
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(return_value=httpx.Response(500))
        async with ComfyUIClient(BASE) as client:
            data = await client.get_system_stats()
    assert data is None


@pytest.mark.asyncio
async def test_malformed_json_returns_none():
    """200 OK with non-JSON body — response.json() raises JSONDecodeError caught by _get()."""
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(
            return_value=httpx.Response(200, content=b"{{not valid json!!!")
        )
        async with ComfyUIClient(BASE) as client:
            data = await client.get_system_stats()
    assert data is None


@pytest.mark.asyncio
async def test_read_error_mid_response_returns_none():
    """Dropped connection mid-response raises httpx.ReadError — caught as NetworkError."""
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(side_effect=httpx.ReadError("connection dropped"))
        async with ComfyUIClient(BASE) as client:
            data = await client.get_system_stats()
    assert data is None


@pytest.mark.asyncio
async def test_read_error_on_queue_returns_none():
    """ReadError on /queue — _get() returns None, fetch_all still returns dict."""
    with respx.mock(base_url=BASE) as mock:
        mock.get("/system_stats").mock(
            return_value=httpx.Response(200, json={"devices": [], "cpu_utilization": 0})
        )
        mock.get("/queue").mock(side_effect=httpx.ReadError("drop"))
        mock.get("/history").mock(side_effect=httpx.ReadError("drop"))
        mock.get("/object_info").mock(side_effect=httpx.ReadError("drop"))
        async with ComfyUIClient(BASE) as client:
            result = await client.fetch_all()
    assert result["system_stats"] is not None
    assert result["queue"] is None
    assert result["history"] is None
    assert result["object_info"] is None


@pytest.mark.asyncio
async def test_fetch_all_all_malformed_json():
    """fetch_all with every endpoint returning malformed JSON — all keys are None."""
    with respx.mock(base_url=BASE) as mock:
        for path in ("/system_stats", "/queue", "/history", "/object_info"):
            mock.get(path).mock(
                return_value=httpx.Response(200, content=b"[[[bad")
            )
        async with ComfyUIClient(BASE) as client:
            result = await client.fetch_all()
    assert result["system_stats"] is None
    assert result["queue"] is None
    assert result["history"] is None
    assert result["object_info"] is None


@pytest.mark.asyncio
async def test_fetch_all_all_500():
    """fetch_all with every endpoint returning HTTP 500 — all keys are None."""
    with respx.mock(base_url=BASE) as mock:
        for path in ("/system_stats", "/queue", "/history", "/object_info"):
            mock.get(path).mock(return_value=httpx.Response(500))
        async with ComfyUIClient(BASE) as client:
            result = await client.fetch_all()
    assert all(v is None for v in result.values())


@pytest.mark.asyncio
async def test_probe_for_live_url_returns_none_when_all_fail():
    """probe_for_live_url returns None when every port is down."""
    from watchupdog.client import probe_for_live_url

    with respx.mock() as mock:
        mock.get(url__regex=r"/system_stats$").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = await probe_for_live_url("http://127.0.0.1:8188", timeout=0.1)
    assert result is None


@pytest.mark.asyncio
async def test_probe_for_live_url_returns_first_live():
    """probe_for_live_url returns the first port that gives HTTP 200."""
    from watchupdog.client import probe_for_live_url

    with respx.mock() as mock:
        # Only port 8189 responds
        mock.get("http://127.0.0.1:8189/system_stats").mock(
            return_value=httpx.Response(200, json={"devices": []})
        )
        mock.get(url__regex=r"/system_stats$").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = await probe_for_live_url("http://127.0.0.1:8188", timeout=0.5)
    assert result == "http://127.0.0.1:8189"
