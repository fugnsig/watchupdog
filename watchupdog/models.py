"""Pydantic v2 models for ComfyUI API responses."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HealthStatus(str, Enum):
    OK = "OK"
    WARN = "WARN"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class DeviceInfo(BaseModel):
    name: str = "Unknown"
    type: str = "cpu"
    index: int = 0
    vram_total: int = 0
    vram_free: int = 0


class SystemStats(BaseModel):
    cpu_utilization: float = 0.0
    ram_total: int = 0
    ram_used: int = 0
    ram_free: int = 0
    disk_free_bytes: int = 0
    disk_total_bytes: int = 0
    devices: list[DeviceInfo] = Field(default_factory=list)


class QueueStats(BaseModel):
    running: list[Any] = Field(default_factory=list)
    pending: list[Any] = Field(default_factory=list)

    @property
    def running_count(self) -> int:
        return len(self.running)

    @property
    def pending_count(self) -> int:
        return len(self.pending)


class JobStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class JobRecord(BaseModel):
    prompt_id: str
    status: JobStatus = JobStatus.UNKNOWN
    completed_at_ms: float | None = None   # ms-since-epoch when job finished
    exec_time_ms: float | None = None      # wall-clock execution duration in ms
    error: str | None = None


class HealthCheckResult(BaseModel):
    name: str
    status: HealthStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class NunchakuInfo(BaseModel):
    dit_loader_present: bool = False
    text_encoder_present: bool = False
    lora_loader_present: bool = False
    wheel_installer_present: bool = False
    precision_mode: str | None = None  # "INT4", "FP4", or None
    fb_cache_enabled: bool = False
    version: str | None = None
    nodes_found: list[str] = Field(default_factory=list)


class GenerationStats(BaseModel):
    total_jobs: int = 0
    completed_jobs: int = 0       # successful only
    cancelled_jobs: int = 0       # user-interrupted — not errors
    failed_jobs: int = 0          # actual errors
    error_rate_pct: float = 0.0
    last_completed_ms: float | None = None   # ms-since-epoch of most recent finish
    avg_exec_time_ms: float | None = None    # mean wall-clock duration of completed jobs


class FullHealthReport(BaseModel):
    overall_status: HealthStatus = HealthStatus.UNKNOWN
    checks: list[HealthCheckResult] = Field(default_factory=list)
    system_stats: SystemStats | None = None
    queue_stats: QueueStats | None = None
    nunchaku: NunchakuInfo | None = None
    generation_stats: GenerationStats | None = None
    alerts: list[str] = Field(default_factory=list)
    comfyui_url: str = ""
    timestamp: str = ""
