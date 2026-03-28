"""
Missing-field resilience tests for all health check functions.

Each test sends valid JSON that is missing fields WatchupDog normally expects,
or sends fields with wrong types (string where int is expected, null where list
is expected, etc.), and asserts that the result is a valid HealthCheckResult
— no unhandled exceptions, and no misleading "OK" when data was absent.

Endpoint → check mapping:
  /system_stats  → check_connectivity, check_vram_health, check_ram_health
  /queue         → check_queue_health, check_stale_jobs
  /history       → check_error_rate
  /object_info   → check_model_files, detect_nunchaku
"""

from __future__ import annotations

import pytest

from watchupdog.checks import (
    check_connectivity,
    check_error_rate,
    check_model_files,
    check_queue_health,
    check_ram_health,
    check_stale_jobs,
    check_vram_health,
    _parse_history,
    _parse_system_stats,
)
from watchupdog.models import HealthStatus
from watchupdog.nunchaku import detect_nunchaku


# ---------------------------------------------------------------------------
# /system_stats — _parse_system_stats defensive parsing
# ---------------------------------------------------------------------------

def test_parse_system_stats_empty_dict():
    s = _parse_system_stats({})
    assert s.devices == []
    assert s.cpu_utilization == 0.0
    assert s.ram_total == 0


def test_parse_system_stats_devices_null():
    s = _parse_system_stats({"devices": None})
    assert s.devices == []


def test_parse_system_stats_devices_not_a_list():
    """devices as a string — normalised to empty list."""
    s = _parse_system_stats({"devices": "gpu0"})
    assert s.devices == []


def test_parse_system_stats_devices_contains_null_entries():
    """Non-dict entries in devices are skipped."""
    s = _parse_system_stats({"devices": [None, 42, "gpu"]})
    assert s.devices == []


def test_parse_system_stats_device_missing_all_vram_fields():
    """Device present but no VRAM fields — defaults to 0."""
    s = _parse_system_stats({"devices": [{"name": "GPU0", "type": "cuda"}]})
    assert len(s.devices) == 1
    assert s.devices[0].vram_total == 0
    assert s.devices[0].vram_free == 0


def test_parse_system_stats_device_string_vram():
    """vram_total is a string — treated as 0, no exception."""
    s = _parse_system_stats({"devices": [
        {"name": "GPU0", "type": "cuda", "vram_total": "8GB", "vram_free": "2GB"}
    ]})
    assert s.devices[0].vram_total == 0


def test_parse_system_stats_cpu_utilization_wrong_type():
    s = _parse_system_stats({"cpu_utilization": "high", "devices": []})
    assert s.cpu_utilization == 0.0


def test_parse_system_stats_ram_as_strings():
    s = _parse_system_stats({"ram_total": "32GB", "ram_used": "8GB", "devices": []})
    assert s.ram_total == 0
    assert s.ram_used == 0


# ---------------------------------------------------------------------------
# check_connectivity — any non-None dict passes (by design)
# ---------------------------------------------------------------------------

def test_connectivity_empty_dict_passes():
    """Empty dict from server still means we got a response — connectivity OK."""
    result = check_connectivity({})
    assert result.status == HealthStatus.OK


def test_connectivity_unrecognised_keys_passes():
    """A dict with unrecognised keys — not a ComfyUI structure — still returns OK.

    The connectivity check only answers "did we get JSON back?".
    Structure validation is the job of _detect_url/_check() in interactive_menu.
    """
    result = check_connectivity({"status": "running", "version": "1.0"})
    assert result.status == HealthStatus.OK


# ---------------------------------------------------------------------------
# check_vram_health — missing / wrong-type VRAM fields
# ---------------------------------------------------------------------------

def test_vram_empty_dict_shows_cpu_only():
    """No devices key → CPU-only mode, not misleading 0/0 GB OK."""
    check, _ = check_vram_health({})
    assert check.status == HealthStatus.OK
    assert "cpu" in check.message.lower()


def test_vram_devices_null_shows_cpu_only():
    check, _ = check_vram_health({"devices": None})
    assert check.status == HealthStatus.OK
    assert "cpu" in check.message.lower()


def test_vram_cuda_device_missing_vram_total_returns_unknown():
    """CUDA device present but vram_total absent → UNKNOWN, not 'VRAM OK: 0.0 GB'."""
    check, _ = check_vram_health({"devices": [{"name": "RTX 4090", "type": "cuda"}]})
    assert check.status == HealthStatus.UNKNOWN
    assert "unavailable" in check.message.lower() or "0 bytes" in check.message


def test_vram_cuda_device_string_vram_total_returns_unknown():
    """CUDA device with string vram_total → parsed as 0 → UNKNOWN."""
    check, _ = check_vram_health({"devices": [
        {"name": "RTX 4090", "type": "cuda", "vram_total": "24GB", "vram_free": "10GB"}
    ]})
    assert check.status == HealthStatus.UNKNOWN


def test_vram_non_dict_device_entries_skipped():
    """Non-dict device entries skipped → CPU-only mode."""
    check, _ = check_vram_health({"devices": [None, 42, "gpu"]})
    assert check.status == HealthStatus.OK
    assert "cpu" in check.message.lower()


def test_vram_device_missing_type_treated_as_cpu():
    """Device without 'type' field defaults to 'cpu' — not counted as GPU.

    Result is CPU-only mode, which is conservative: no false VRAM alert.
    """
    check, _ = check_vram_health({"devices": [
        {"name": "SomeDevice", "vram_total": 16 * 1024**3, "vram_free": 4 * 1024**3}
    ]})
    # Device has no 'type', defaults to 'cpu' → non_cpu is empty → CPU-only
    assert check.status == HealthStatus.OK
    assert "cpu" in check.message.lower()


def test_vram_real_cuda_device_with_valid_data_still_ok():
    """Sanity check: valid CUDA device with real data still reports OK."""
    check, _ = check_vram_health({"devices": [
        {"name": "RTX 4090", "type": "cuda",
         "vram_total": 24 * 1024**3, "vram_free": 20 * 1024**3}
    ]})
    assert check.status == HealthStatus.OK
    assert "VRAM OK" in check.message


# ---------------------------------------------------------------------------
# check_queue_health — missing / wrong-type queue fields
# ---------------------------------------------------------------------------

def test_queue_empty_dict():
    """Both queue fields absent → treated as empty → OK."""
    check, stats = check_queue_health({})
    assert check.status == HealthStatus.OK
    assert stats.pending_count == 0
    assert stats.running_count == 0


def test_queue_running_null():
    check, stats = check_queue_health({"queue_running": None, "queue_pending": []})
    assert check.status == HealthStatus.OK
    assert stats.running_count == 0


def test_queue_running_as_string():
    """queue_running is a string — normalised to [] — OK."""
    check, stats = check_queue_health({"queue_running": "busy", "queue_pending": []})
    assert check.status == HealthStatus.OK
    assert stats.running_count == 0


def test_queue_pending_as_integer():
    """queue_pending is an int — normalised to [] — OK."""
    check, stats = check_queue_health({"queue_running": [], "queue_pending": 7})
    assert check.status == HealthStatus.OK
    assert stats.pending_count == 0


# ---------------------------------------------------------------------------
# check_stale_jobs — missing / wrong-type queue fields
# ---------------------------------------------------------------------------

def test_stale_jobs_empty_dict():
    """Both fields absent → empty running → OK."""
    result = check_stale_jobs({})
    assert result.status == HealthStatus.OK


def test_stale_jobs_running_is_string():
    result = check_stale_jobs({"queue_running": "processing"})
    assert result.status == HealthStatus.OK


def test_stale_jobs_running_entries_non_parseable():
    """queue_running entries that are not list/tuple or dict — IDs unparseable.

    Job count reported correctly, IDs fall back to 'unknown' — no exception.
    """
    result = check_stale_jobs({"queue_running": [42, True, "job1"]}, running_since={})
    assert result.status == HealthStatus.OK


# ---------------------------------------------------------------------------
# _parse_history / check_error_rate — missing / wrong-type history fields
# ---------------------------------------------------------------------------

def test_parse_history_empty_dict():
    assert _parse_history({}) == []


def test_parse_history_top_level_list():
    """Server returned a list instead of dict — treated as empty."""
    assert _parse_history(["job1", "job2"]) == []  # type: ignore[arg-type]


def test_parse_history_null_job_value():
    """Job entry is null — skipped."""
    jobs = _parse_history({"job1": None})
    assert jobs == []


def test_parse_history_job_status_as_string():
    """status is a string, not a dict — job counted as UNKNOWN status."""
    jobs = _parse_history({"job1": {"status": "success", "outputs": {}}})
    assert len(jobs) == 1
    from watchupdog.models import JobStatus
    assert jobs[0].status == JobStatus.UNKNOWN


def test_parse_history_messages_not_a_list():
    """messages is a string — normalised to [] — job counted as UNKNOWN."""
    from watchupdog.models import JobStatus
    jobs = _parse_history({"job1": {"status": {"messages": "done"}}})
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.UNKNOWN


def test_parse_history_message_entries_are_dicts():
    """Each message entry is a dict instead of [type, data] — skipped."""
    from watchupdog.models import JobStatus
    jobs = _parse_history({"job1": {"status": {"messages": [
        {"type": "execution_success", "data": {}}
    ]}}})
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.UNKNOWN


def test_parse_history_message_entry_too_short():
    """Message entry is a 1-element list — skipped."""
    from watchupdog.models import JobStatus
    jobs = _parse_history({"job1": {"status": {"messages": [
        ["execution_success"]   # only 1 element, needs >= 2
    ]}}})
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.UNKNOWN


def test_parse_history_bad_timestamp_still_parses_status():
    """Message has bad timestamp but valid type — job status parsed, exec_time is None."""
    from watchupdog.models import JobStatus
    jobs = _parse_history({"job1": {"status": {"messages": [
        ["execution_start",   {"timestamp": "not-a-float"}],
        ["execution_success", {"timestamp": "also-bad"}],
    ]}}})
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.SUCCESS
    assert jobs[0].exec_time_ms is None


def test_check_error_rate_empty_dict():
    check, stats = check_error_rate({})
    assert check.status == HealthStatus.UNKNOWN
    assert "No job history" in check.message


def test_check_error_rate_all_unknown_jobs():
    """All jobs have unparseable status — counted but all UNKNOWN — no error rate."""
    check, stats = check_error_rate({
        "job1": {"status": "done"},
        "job2": {"status": "done"},
    })
    # Jobs exist but none are SUCCESS/ERROR/INTERRUPTED → error_rate = 0 → OK
    assert check.status == HealthStatus.OK
    assert stats.total_jobs == 2
    assert stats.failed_jobs == 0


# ---------------------------------------------------------------------------
# check_model_files / scan_models_from_object_info — missing fields
# ---------------------------------------------------------------------------

def test_model_files_empty_object_info():
    """Empty dict → no models discovered → WARN."""
    check = check_model_files({})
    assert check.status == HealthStatus.WARN
    assert "no model" in check.message.lower()


def test_model_files_null_node_values():
    """Node values are null — skipped — no models discovered → WARN."""
    check = check_model_files({"KSampler": None, "CLIPTextEncode": None})
    assert check.status == HealthStatus.WARN


def test_model_files_node_with_null_input():
    """Node has input=null — skipped."""
    check = check_model_files({
        "LoadCheckpoint": {"input": None, "output": []},
    })
    assert check.status == HealthStatus.WARN


def test_model_files_node_input_not_a_dict():
    """input is a list instead of dict — skipped."""
    check = check_model_files({
        "LoadCheckpoint": {"input": ["ckpt_name", ["model.safetensors"]], "output": []},
    })
    assert check.status == HealthStatus.WARN


def test_model_files_spec_is_not_a_list():
    """Spec entry is a string, not [choices, opts] list — skipped."""
    check = check_model_files({
        "LoadCheckpoint": {
            "input": {"required": {"ckpt_name": "model.safetensors"}},
        }
    })
    assert check.status == HealthStatus.WARN


def test_model_files_spec_choices_is_string():
    """spec[0] is a string not a list — skipped, no crash."""
    check = check_model_files({
        "LoadCheckpoint": {
            "input": {"required": {"ckpt_name": ["model.safetensors"]}},
        }
    })
    # spec[0] is "model.safetensors" (a string), not a list → skipped
    assert check.status == HealthStatus.WARN


def test_model_files_valid_ckpt_discovered():
    """Sanity check: valid structure with a safetensors file → OK."""
    check = check_model_files({
        "CheckpointLoaderSimple": {
            "input": {
                "required": {
                    "ckpt_name": [["v1-5-pruned.safetensors"], {}]
                }
            }
        }
    })
    assert check.status == HealthStatus.OK
    assert "1 models" in check.message or "Checkpoint" in check.message


# ---------------------------------------------------------------------------
# detect_nunchaku — missing / wrong-type fields in /object_info
# ---------------------------------------------------------------------------

def test_nunchaku_empty_object_info():
    info = detect_nunchaku({})
    assert not info.nodes_found
    assert not info.dit_loader_present


def test_nunchaku_null_node_value():
    """Node present in object_info but its value is null — handled."""
    info = detect_nunchaku({"NunchakuFluxDiTLoader": None})
    # Node name is found (key exists), but metadata extraction gets empty dict
    assert "NunchakuFluxDiTLoader" in info.nodes_found
    assert info.dit_loader_present
    assert info.version is None   # no metadata to parse


def test_nunchaku_node_input_null():
    """Node with input=null — precision/fb_cache detection skips gracefully."""
    info = detect_nunchaku({
        "NunchakuFluxDiTLoader": {"input": None, "description": "v0.3.2"},
    })
    assert info.dit_loader_present
    assert info.precision_mode is None
    assert not info.fb_cache_enabled


def test_nunchaku_non_dict_nodes_skipped():
    """Non-Nunchaku nodes with non-dict values don't crash precision detection."""
    info = detect_nunchaku({
        "KSampler": None,
        "CLIPTextEncode": 42,
        "NunchakuFluxDiTLoader": {"input": None},
    })
    assert info.dit_loader_present
    assert info.precision_mode is None
