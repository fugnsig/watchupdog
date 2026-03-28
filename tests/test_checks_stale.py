"""Unit tests for check_stale_jobs in watchupdog.checks."""

from __future__ import annotations

import time

import pytest

from watchupdog.checks import check_stale_jobs
from watchupdog.models import HealthStatus

from .fixtures import SAMPLE_QUEUE_BUSY, SAMPLE_QUEUE_EMPTY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _queue_with_job(prompt_id: str = "prompt-001") -> dict:
    """Single running job using the ComfyUI list format [number, prompt_id, ...]."""
    return {
        "queue_running": [[1, prompt_id, {}, {}]],
        "queue_pending": [],
    }


def _queue_with_jobs(*prompt_ids: str) -> dict:
    return {
        "queue_running": [[i + 1, pid, {}, {}] for i, pid in enumerate(prompt_ids)],
        "queue_pending": [],
    }


# ---------------------------------------------------------------------------
# 1. None queue data returns UNKNOWN
# ---------------------------------------------------------------------------

def test_stale_jobs_none_queue_returns_unknown():
    result = check_stale_jobs(None)
    assert result.status == HealthStatus.UNKNOWN
    assert "unavailable" in result.message.lower()


# ---------------------------------------------------------------------------
# 2. Empty queue returns OK
# ---------------------------------------------------------------------------

def test_stale_jobs_empty_queue_returns_ok():
    result = check_stale_jobs(SAMPLE_QUEUE_EMPTY)
    assert result.status == HealthStatus.OK
    assert "No jobs" in result.message or "running" in result.message.lower()


def test_stale_jobs_empty_queue_clears_running_since():
    running_since = {"old-prompt": time.time() - 600}
    check_stale_jobs(SAMPLE_QUEUE_EMPTY, running_since=running_since)
    assert running_since == {}


# ---------------------------------------------------------------------------
# 3. Running jobs with running_since=None returns OK with staleness note
# ---------------------------------------------------------------------------

def test_stale_jobs_running_since_none_returns_ok():
    result = check_stale_jobs(_queue_with_job(), running_since=None)
    assert result.status == HealthStatus.OK
    assert "staleness tracking requires watch mode" in result.message


def test_stale_jobs_running_since_none_includes_running_ids():
    result = check_stale_jobs(_queue_with_job("prompt-abc"), running_since=None)
    assert "prompt-abc" in result.details.get("running_ids", [])


# ---------------------------------------------------------------------------
# 4. First call with running_since={} returns OK (job just appeared)
# ---------------------------------------------------------------------------

def test_stale_jobs_first_call_returns_ok():
    running_since: dict[str, float] = {}
    result = check_stale_jobs(_queue_with_job("prompt-001"), running_since=running_since)
    assert result.status == HealthStatus.OK


# ---------------------------------------------------------------------------
# 5. After first call, running_since is populated
# ---------------------------------------------------------------------------

def test_stale_jobs_populates_running_since():
    running_since: dict[str, float] = {}
    check_stale_jobs(_queue_with_job("prompt-001"), running_since=running_since)
    assert "prompt-001" in running_since
    assert isinstance(running_since["prompt-001"], float)


def test_stale_jobs_does_not_overwrite_existing_timestamp():
    running_since: dict[str, float] = {}
    check_stale_jobs(_queue_with_job("prompt-001"), running_since=running_since)
    first_seen = running_since["prompt-001"]

    # Calling again should NOT update the timestamp
    check_stale_jobs(_queue_with_job("prompt-001"), running_since=running_since)
    assert running_since["prompt-001"] == first_seen


# ---------------------------------------------------------------------------
# 6. Simulate time passing → WARN after threshold
# ---------------------------------------------------------------------------

def test_stale_jobs_warn_after_threshold():
    running_since: dict[str, float] = {}
    queue = _queue_with_job("prompt-stale")

    # Seed with a timestamp 400 seconds ago (> 5 min = 300 s threshold)
    running_since["prompt-stale"] = time.time() - 400

    result = check_stale_jobs(queue, stale_minutes=5.0, running_since=running_since)
    assert result.status == HealthStatus.WARN
    assert "stale" in result.message.lower()


def test_stale_jobs_warn_message_contains_threshold():
    running_since = {"prompt-stale": time.time() - 400}
    queue = _queue_with_job("prompt-stale")

    result = check_stale_jobs(queue, stale_minutes=5.0, running_since=running_since)
    assert "5" in result.message  # threshold appears in message


def test_stale_jobs_warn_contains_stale_ids_in_details():
    running_since = {"prompt-stale": time.time() - 400}
    queue = _queue_with_job("prompt-stale")

    result = check_stale_jobs(queue, stale_minutes=5.0, running_since=running_since)
    assert "prompt-stale" in result.details.get("stale_ids", [])


def test_stale_jobs_ok_just_under_threshold():
    running_since = {"prompt-fresh": time.time() - 200}
    queue = _queue_with_job("prompt-fresh")

    result = check_stale_jobs(queue, stale_minutes=5.0, running_since=running_since)
    assert result.status == HealthStatus.OK


# ---------------------------------------------------------------------------
# 7. Jobs that leave queue are removed from running_since
# ---------------------------------------------------------------------------

def test_stale_jobs_removes_completed_from_running_since():
    running_since: dict[str, float] = {
        "prompt-done": time.time() - 10,
        "prompt-still-running": time.time() - 10,
    }
    # Only prompt-still-running is in queue now
    queue = _queue_with_job("prompt-still-running")

    check_stale_jobs(queue, running_since=running_since)

    assert "prompt-done" not in running_since
    assert "prompt-still-running" in running_since


def test_stale_jobs_removes_all_when_queue_empties():
    running_since = {
        "a": time.time() - 10,
        "b": time.time() - 10,
    }
    check_stale_jobs(SAMPLE_QUEUE_EMPTY, running_since=running_since)
    assert running_since == {}


# ---------------------------------------------------------------------------
# 8. 10 minutes running with 5 min threshold → WARN with correct message
# ---------------------------------------------------------------------------

def test_stale_jobs_ten_min_with_five_min_threshold():
    running_since = {"prompt-long": time.time() - 600}  # 10 minutes ago
    queue = _queue_with_job("prompt-long")

    result = check_stale_jobs(queue, stale_minutes=5.0, running_since=running_since)

    assert result.status == HealthStatus.WARN
    # Message should contain stale count and threshold
    assert "1 stale" in result.message
    assert "5" in result.message  # threshold value
    # Elapsed ~10m should appear
    assert "10" in result.message or "9" in result.message  # allow for slight timing variance


# ---------------------------------------------------------------------------
# 9. Multiple jobs — only stale ones flagged
# ---------------------------------------------------------------------------

def test_stale_jobs_only_stale_flagged_when_mixed():
    now = time.time()
    running_since = {
        "prompt-old": now - 400,   # stale (> 5 min)
        "prompt-new": now - 60,    # fresh (< 5 min)
    }
    queue = _queue_with_jobs("prompt-old", "prompt-new")

    result = check_stale_jobs(queue, stale_minutes=5.0, running_since=running_since)

    assert result.status == HealthStatus.WARN
    stale_ids = result.details.get("stale_ids", [])
    assert "prompt-old" in stale_ids
    assert "prompt-new" not in stale_ids


# ---------------------------------------------------------------------------
# 10. SAMPLE_QUEUE_BUSY integration
# ---------------------------------------------------------------------------

def test_stale_jobs_sample_queue_busy_first_call():
    """First call with SAMPLE_QUEUE_BUSY should be OK (jobs just seen)."""
    running_since: dict[str, float] = {}
    result = check_stale_jobs(SAMPLE_QUEUE_BUSY, running_since=running_since)
    assert result.status == HealthStatus.OK
    assert "prompt-001" in running_since


def test_stale_jobs_sample_queue_busy_becomes_stale():
    """Seed SAMPLE_QUEUE_BUSY job as old → should warn."""
    running_since = {"prompt-001": time.time() - 400}
    result = check_stale_jobs(SAMPLE_QUEUE_BUSY, stale_minutes=5.0, running_since=running_since)
    assert result.status == HealthStatus.WARN
