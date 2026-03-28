"""Shared test fixtures and sample API payloads."""

from __future__ import annotations

SAMPLE_SYSTEM_STATS = {
    "cpu_utilization": 25.0,
    "ram_total": 34_359_738_368,  # 32 GB
    "ram_used": 8_589_934_592,    # 8 GB
    "devices": [
        {
            "name": "NVIDIA GeForce RTX 4090",
            "type": "cuda",
            "index": 0,
            "vram_total": 25_769_803_776,  # 24 GB
            "vram_free": 19_327_352_832,   # ~18 GB free
            "torch_vram_total": 25_769_803_776,
            "torch_vram_free": 19_327_352_832,
        }
    ],
}

SAMPLE_SYSTEM_STATS_HIGH_VRAM = {
    **SAMPLE_SYSTEM_STATS,
    "devices": [
        {
            **SAMPLE_SYSTEM_STATS["devices"][0],
            "vram_free": 256 * 1024 * 1024,  # Only 256 MB free -> ~99%
        }
    ],
}

SAMPLE_QUEUE_EMPTY = {
    "queue_running": [],
    "queue_pending": [],
}

SAMPLE_QUEUE_BUSY = {
    "queue_running": [
        [1, "prompt-001", {}, {}],
    ],
    "queue_pending": [
        [i, f"prompt-{i:03d}", {}, {}] for i in range(2, 15)
    ],
}

SAMPLE_HISTORY_ALL_OK = {
    f"prompt-{i:03d}": {
        "status": {
            "messages": [
                ["execution_success", {"timestamp": 1_700_000_000.0 + i * 10}],
            ]
        },
        "outputs": {},
    }
    for i in range(20)
}

SAMPLE_HISTORY_WITH_ERRORS = {
    **{
        f"prompt-ok-{i:03d}": {
            "status": {
                "messages": [
                    ["execution_success", {"timestamp": 1_700_000_000.0 + i * 10}],
                ]
            },
            "outputs": {},
        }
        for i in range(8)
    },
    "prompt-err-001": {
        "status": {
            "messages": [
                [
                    "execution_error",
                    {
                        "exception_message": "CUDA out of memory",
                        "timestamp": 1_700_000_200.0,
                    },
                ]
            ]
        },
        "outputs": {},
    },
    "prompt-err-002": {
        "status": {
            "messages": [
                [
                    "execution_error",
                    {
                        "exception_message": "RuntimeError: out of memory",
                        "timestamp": 1_700_000_300.0,
                    },
                ]
            ]
        },
        "outputs": {},
    },
}

SAMPLE_OBJECT_INFO_WITH_NUNCHAKU = {
    "NunchakuFluxDiTLoader": {
        "input": {
            "required": {
                "model": [["svdq-int4_r32-flux.1-dev.safetensors", "svdq-fp4_r32-flux.1-dev.safetensors"]],
                "fb_cache": [["enable", "disable"]],
            }
        },
        "description": "Nunchaku FLUX DiT Loader v0.3.2",
        "output": [],
        "name": "NunchakuFluxDiTLoader",
    },
    "NunchakuTextEncoderLoader": {
        "input": {"required": {"clip_name": [["clip_l.safetensors"]]}},
        "description": "",
        "output": [],
        "name": "NunchakuTextEncoderLoader",
    },
    "NunchakuFluxLoraLoader": {
        "input": {"required": {}},
        "description": "",
        "output": [],
        "name": "NunchakuFluxLoraLoader",
    },
    "KSampler": {
        "input": {"required": {}},
        "output": [],
        "name": "KSampler",
    },
}

SAMPLE_OBJECT_INFO_NO_NUNCHAKU = {
    "KSampler": {
        "input": {"required": {}},
        "output": [],
        "name": "KSampler",
    },
    "CheckpointLoaderSimple": {
        "input": {"required": {}},
        "output": [],
        "name": "CheckpointLoaderSimple",
    },
}

SAMPLE_OBJECT_INFO_WHEEL_ONLY = {
    "NunchakuWheelInstaller": {
        "input": {"required": {}},
        "output": [],
        "name": "NunchakuWheelInstaller",
    },
}
