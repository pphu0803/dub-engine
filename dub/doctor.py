"""环境自检：告诉你缺什么、怎么装、能不能用 GPU。"""

from __future__ import annotations

import importlib
import os
import shutil
import sys


def _dist(name: str) -> str:
    try:
        m = importlib.import_module(name)
        return getattr(m, "__version__", "ok")
    except Exception as exc:  # noqa: BLE001
        return f"MISSING ({type(exc).__name__})"


def ffmpeg_path() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def gpu_info() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            mem = torch.cuda.get_device_properties(0).total_memory / 2**30
            hip = getattr(torch.version, "hip", None)
            return f"{name} / {mem:.1f} GB / {'ROCm ' + hip if hip else 'CUDA ' + str(torch.version.cuda)}"
        return "torch 可用但无加速设备（CPU only）"
    except Exception as exc:  # noqa: BLE001
        return f"torch 不可用（{type(exc).__name__}）"


def run() -> int:
    print(f"python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"ffmpeg      : {ffmpeg_path() or '未找到（可用 imageio-ffmpeg 提供）'}")
    print(f"GPU         : {gpu_info()}")
    print()
    print("依赖：")
    for mod, hint in [
        ("numpy", "pip install numpy"),
        ("soundfile", "pip install soundfile"),
        ("av", "pip install av"),
        ("librosa", "pip install librosa"),
        ("scipy", "pip install scipy"),
        ("torch", "装 ROCm/CUDA 版 torch"),
        ("torchaudio", "与 torch 配套"),
        ("demucs", "pip install demucs"),
        ("faster_whisper", "pip install faster-whisper"),
        ("transformers", "pip install transformers"),
        ("edge_tts", "pip install edge-tts"),
    ]:
        v = _dist(mod)
        flag = "ok " if not v.startswith("MISSING") else "-- "
        print(f"  {flag}{mod:16} {v}")
        if v.startswith("MISSING"):
            print(f"     -> {hint}")

    print()
    print("LLM 配置（翻译阶段）：")
    for k in ("DUB_LLM_BASE", "DUB_LLM_KEY", "DUB_LLM_MODEL"):
        v = os.environ.get(k, "")
        shown = (v[:6] + "…") if v and k.endswith("KEY") else v
        print(f"  {k:14} {shown or '(未设置)'}")
    print("  CosyVoice（可选）：DUB_COSYVOICE_DIR / DUB_COSYVOICE_MODEL / --ref-wav")
    return 0
