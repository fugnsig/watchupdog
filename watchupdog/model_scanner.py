"""Dynamic model directory scanner — works with any ComfyUI setup."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".pkl"}

# Maps models/ subdirectory name → human label
_DIR_LABELS: dict[str, str] = {
    "checkpoints": "Checkpoint",
    "diffusion_models": "Diffusion Model",
    "unet": "UNet",
    "loras": "LoRA",
    "vae": "VAE",
    "text_encoders": "Text Encoder",
    "clip": "CLIP",
    "clip_vision": "CLIP Vision",
    "controlnet": "ControlNet",
    "embeddings": "Embedding",
    "hypernetworks": "Hypernetwork",
    "upscale_models": "Upscaler",
    "ipadapter": "IP-Adapter",
    "style_models": "Style Model",
    "gligen": "GLIGEN",
    "photomaker": "PhotoMaker",
    "insightface": "InsightFace",
    "ultralytics": "YOLO/Ultralytics",
    "yolo": "YOLO",
    "t5": "T5",
    "pulid": "PuLID",
    "sams": "SAM",
    "onnx": "ONNX",
}

# Quantisation / precision patterns, checked in order (first match wins)
_QUANT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsvdq\b", re.I),                   "SVDQuant"),
    (re.compile(r"\bfp4\b",  re.I),                   "FP4"),
    (re.compile(r"\bint4\b", re.I),                   "INT4"),
    (re.compile(r"e4m3fn|e5m2|\bfp8\b", re.I),        "FP8"),
    (re.compile(r"\bq8_0\b|\bq8\b",     re.I),        "Q8"),
    (re.compile(r"\bq4_k_m\b|\bq4_k\b|\bq4\b", re.I),"Q4"),
    (re.compile(r"\bnf4\b|\bbnb\b",     re.I),        "BNB-NF4"),
    (re.compile(r"\bgguf\b",            re.I),         "GGUF"),
    (re.compile(r"\bfp16\b",            re.I),         "FP16"),
    (re.compile(r"\bbf16\b",            re.I),         "BF16"),
    (re.compile(r"\bawq\b",             re.I),         "AWQ"),
]

# Rough model family hints from filename
_FAMILY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"flux\.?1?[\-_]dev",   re.I), "FLUX.1-dev"),
    (re.compile(r"flux\.?1?[\-_]schnell",re.I),"FLUX.1-schnell"),
    (re.compile(r"\bflux\b",            re.I), "FLUX"),
    (re.compile(r"\bsdxl\b|stable.diffusion.xl|sd_xl", re.I), "SDXL"),
    (re.compile(r"\bsd3\b|stable.diffusion.3", re.I), "SD3"),
    (re.compile(r"\bsd1[._\-]?5\b|v1[\-_]5", re.I),   "SD1.5"),
    (re.compile(r"\bsd2\b|v2[\-_]1",    re.I), "SD2"),
    (re.compile(r"\bwanvideo\b|\bwan\b", re.I),"WanVideo"),
    (re.compile(r"\bhunyan\b",          re.I), "HunyuanVideo"),
    (re.compile(r"\bcogvideo\b",        re.I), "CogVideo"),
    (re.compile(r"\bsvd\b",             re.I), "SVD"),
    (re.compile(r"\bplayground\b",      re.I), "Playground"),
    (re.compile(r"\bkandinsky\b",       re.I), "Kandinsky"),
    (re.compile(r"\bkolors\b",          re.I), "Kolors"),
]


def detect_quant(name: str) -> str | None:
    for pat, label in _QUANT_PATTERNS:
        if pat.search(name):
            return label
    return None


def detect_family(name: str) -> str | None:
    for pat, label in _FAMILY_PATTERNS:
        if pat.search(name):
            return label
    return None


@dataclass
class ModelFile:
    path: Path
    category: str
    size_bytes: int
    quant: str | None = None
    family: str | None = None

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def relative_name(self) -> str:
        """Name relative to the category dir, preserving subdirs (e.g. flux/lora.safetensors)."""
        try:
            # path is absolute; category dir is two levels up (models/<category>/)
            return str(self.path.relative_to(self.path.parents[max(0, len(self.path.parts) - len(self.path.relative_to(self.path.parents[0]).parts) - 1)]))
        except Exception:
            return self.path.name


@dataclass
class ModelScanResult:
    by_category: dict[str, list[ModelFile]] = field(default_factory=dict)

    @property
    def total_files(self) -> int:
        return sum(len(v) for v in self.by_category.values())

    @property
    def total_size_gb(self) -> float:
        return sum(f.size_gb for files in self.by_category.values() for f in files)

    @property
    def categories(self) -> list[str]:
        return sorted(self.by_category)

    def files_in(self, category: str) -> list[ModelFile]:
        return self.by_category.get(category, [])


def scan_models(comfyui_root: Path) -> ModelScanResult:
    """
    Recursively scan all model directories under comfyui_root/models/.
    Returns a ModelScanResult grouped by category, sorted largest-first.
    """
    result = ModelScanResult()
    models_dir = comfyui_root / "models"
    if not models_dir.exists():
        return result

    try:
        subdirs = sorted(models_dir.iterdir())
    except (PermissionError, OSError):
        return result
    for subdir in subdirs:
        if not subdir.is_dir():
            continue
        category = _DIR_LABELS.get(subdir.name, subdir.name.replace("_", " ").title())
        files: list[ModelFile] = []

        try:
            entries = list(subdir.rglob("*"))
        except (PermissionError, OSError):
            entries = []
        for f in entries:
            if not f.is_file():
                continue
            if f.suffix.lower() not in MODEL_EXTENSIONS:
                continue
            # Skip tiny placeholder files
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size < 1024 * 10:  # < 10 KB is almost certainly a stub
                continue

            files.append(ModelFile(
                path=f,
                category=category,
                size_bytes=size,
                quant=detect_quant(f.name),
                family=detect_family(f.name),
            ))

        if files:
            files.sort(key=lambda m: m.size_bytes, reverse=True)
            result.by_category[category] = files

    return result


def scan_models_from_object_info(object_info: dict) -> dict[str, list[str]]:
    """
    Extract all model filenames visible to ComfyUI from /object_info.
    Returns {category_hint: [filename, ...]} based on which input widgets expose them.

    Works for ANY set of nodes — no hardcoded node names.
    """
    from collections import defaultdict

    # Map of known input-key → category label
    _KEY_HINTS: dict[str, str] = {
        "model": "Model",
        "unet_name": "Diffusion Model",
        "ckpt_name": "Checkpoint",
        "vae_name": "VAE",
        "clip_name": "CLIP",
        "clip_name1": "CLIP",
        "clip_name2": "CLIP",
        "lora_name": "LoRA",
        "controlnet_name": "ControlNet",
        "style_model_name": "Style Model",
        "embedding": "Embedding",
        "upscale_model": "Upscaler",
        "ipadapter": "IP-Adapter",
        "ip_adapter_name": "IP-Adapter",
    }

    found: dict[str, set[str]] = defaultdict(set)

    for node_data in object_info.values():
        if not isinstance(node_data, dict):
            continue
        # `input` may be absent, null, or non-dict in unusual node definitions
        input_raw = node_data.get("input")
        if not isinstance(input_raw, dict):
            continue
        for group in input_raw.values():
            if not isinstance(group, dict):
                continue
            for key, spec in group.items():
                if not isinstance(spec, list) or not spec:
                    continue
                choices = spec[0]
                if not isinstance(choices, list):
                    continue
                category = _KEY_HINTS.get(key.lower(), None)
                for item in choices:
                    if isinstance(item, str) and any(item.lower().endswith(ext) for ext in MODEL_EXTENSIONS):
                        label = category or "Model"
                        found[label].add(item)

    return {k: sorted(v) for k, v in found.items()}
