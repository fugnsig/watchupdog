"""Async httpx wrapper for the ComfyUI REST API."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx

# Ports to try when the configured URL doesn't respond
_PROBE_PORTS = (8188, 8189, 7860, 7861, 8080, 8000, 3000, 3001)


async def probe_for_live_url(primary_url: str, timeout: float = 1.5) -> str | None:
    """
    Try common ComfyUI ports and return the first URL that responds to
    /system_stats.  Returns None if nothing responds.
    Skips the primary_url (already tried by the caller).
    """
    import re
    primary_host = re.sub(r":\d+$", "", primary_url.rstrip("/").split("//", 1)[-1])

    async def _try(url: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as c:
                r = await c.get(f"{url}/system_stats")
                if r.status_code == 200:
                    return url
        except Exception:
            pass
        return None

    tasks = [
        _try(f"http://{primary_host}:{port}")
        for port in _PROBE_PORTS
        if f"http://{primary_host}:{port}" != primary_url.rstrip("/")
    ]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        if result:
            return result
    return None


class ComfyUIClient:
    """Thin async client for ComfyUI's HTTP endpoints."""

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ComfyUIClient":
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str) -> Any | None:
        """GET request; returns parsed JSON or None on connection/HTTP error."""
        if self._client is None:
            raise RuntimeError("ComfyUIClient must be used as an async context manager")
        try:
            response = await self._client.get(path)
            response.raise_for_status()
            return response.json()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError,
                httpx.HTTPStatusError):
            return None
        except Exception as exc:
            # Unexpected error — most likely a JSON decode failure caused by the
            # server returning HTML (e.g. a reverse proxy error page) instead of
            # JSON.  ComfyUI IS reachable in that case, but the connectivity check
            # will still show CRITICAL because we return None.  Write to stderr so
            # the problem is diagnosable without changing the return-type contract.
            print(
                f"[watchupdog] Unexpected error fetching {path}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return None

    async def ping(self) -> bool:
        """Return True if ComfyUI responds to /system_stats."""
        result = await self._get("/system_stats")
        return result is not None

    async def get_system_stats(self) -> dict[str, Any] | None:
        return await self._get("/system_stats")

    async def get_queue(self) -> dict[str, Any] | None:
        return await self._get("/queue")

    async def get_history(self, max_items: int = 50) -> dict[str, Any] | None:
        return await self._get(f"/history?max_items={max_items}")

    async def get_object_info(self) -> dict[str, Any] | None:
        return await self._get("/object_info")

    async def fetch_all(self, history_jobs: int = 50) -> dict[str, Any]:
        """Fetch all endpoints concurrently; each key is None on failure.

        Uses return_exceptions=True so that a CancelledError or other
        BaseException raised by one endpoint (not caught by except Exception)
        cannot cancel the remaining fetches or propagate to the caller.
        Any BaseException result is normalised to None.
        """
        results = await asyncio.gather(
            self.get_system_stats(),
            self.get_queue(),
            self.get_history(history_jobs),
            self.get_object_info(),
            return_exceptions=True,
        )
        system, queue, history, object_info = (
            None if isinstance(r, BaseException) else r
            for r in results
        )
        return {
            "system_stats": system,
            "queue": queue,
            "history": history,
            "object_info": object_info,
        }


