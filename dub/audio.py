"""音频读写与基础运算（numpy + soundfile，不依赖系统 ffmpeg）。"""

from __future__ import annotations

import math
from math import gcd

import numpy as np


def load(path: str, sr: int | None = None, mono: bool = False) -> tuple[np.ndarray, int]:
    """读 wav → float32 (n, ch)。"""
    import soundfile as sf

    data, native = sf.read(path, always_2d=True, dtype="float32")
    if sr and sr != native:
        data = resample(data, native, sr)
        native = sr
    if mono:
        data = data.mean(axis=1, keepdims=True)
    return data, native


def save(path: str, data: np.ndarray, sr: int) -> None:
    import soundfile as sf

    if data.ndim == 1:
        data = data[:, None]
    sf.write(path, np.clip(data, -1.0, 1.0), sr, subtype="PCM_16")


def resample(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return data
    try:
        # scipy 的 polyphase 比 librosa 的 soxr 路径在长音频上快一个量级
        from scipy.signal import resample_poly

        g = gcd(int(src_sr), int(dst_sr))
        up, down = int(dst_sr) // g, int(src_sr) // g
        chans = [resample_poly(data[:, c], up, down) for c in range(data.shape[1])]
        n = min(len(c) for c in chans)
        return np.stack([c[:n] for c in chans], axis=1).astype(np.float32)
    except Exception:
        import librosa

        chans = [librosa.resample(data[:, c], orig_sr=src_sr, target_sr=dst_sr) for c in range(data.shape[1])]
        n = min(len(c) for c in chans)
        return np.stack([c[:n] for c in chans], axis=1).astype(np.float32)


def time_stretch(data: np.ndarray, sr: int, rate: float) -> np.ndarray:
    """变速不变调。rate>1 变快。"""
    if abs(rate - 1.0) < 0.01:
        return data
    try:
        import librosa

        chans = [librosa.effects.time_stretch(data[:, c], rate=rate) for c in range(data.shape[1])]
        n = min(len(c) for c in chans)
        return np.stack([c[:n] for c in chans], axis=1).astype(np.float32)
    except Exception:
        # 退化：简单重采样（会变调，但至少时长对得上）
        idx = np.arange(0, len(data), rate)
        idx = np.clip(idx, 0, len(data) - 1).astype(np.int64)
        return data[idx]


def pitch_shift(data: np.ndarray, sr: int, semitones: float) -> np.ndarray:
    """变调不变速。semitones>0 升 key；负值降 key（男声低沉显沧桑）。"""
    if abs(semitones) < 0.01:
        return data
    import librosa

    chans = [librosa.effects.pitch_shift(data[:, c], sr=sr, n_steps=semitones)
             for c in range(data.shape[1])]
    n = min(len(c) for c in chans)
    return np.stack([c[:n] for c in chans], axis=1).astype(np.float32)


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20.0))


def peak_normalize(data: np.ndarray, target_dbfs: float = -1.0) -> np.ndarray:
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak < 1e-9:
        return data
    target = db_to_gain(target_dbfs)
    return data * (target / peak)


def seconds_to_samples(sec: float, sr: int) -> int:
    return int(round(sec * sr))


def silence(seconds: float, sr: int, channels: int = 1) -> np.ndarray:
    return np.zeros((max(1, seconds_to_samples(seconds, sr)), channels), dtype=np.float32)


def place_fade(track: np.ndarray, clip: np.ndarray, start_sample: int, fade_ms: float = 8.0) -> None:
    """把 clip 叠加到 track 的指定位置，带短淡入淡出，原地修改。"""
    if start_sample >= len(track) or clip.size == 0:
        return
    n = min(len(clip), len(track) - start_sample)
    clip = clip[:n]
    fade = min(int(fade_ms / 1000 * 48000), n // 4)
    if fade > 0:
        env = np.ones(n, dtype=np.float32)
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        env[:fade] = ramp
        env[-fade:] = ramp[::-1]
        clip = clip * env[:, None]
    track[start_sample : start_sample + n] += clip


def db_envelope(n: int, segments: list[tuple[int, int]], sr: int, duck_db: float,
                attack_ms: float = 120.0, release_ms: float = 250.0) -> np.ndarray:
    """生成增益包络：说话段压 duck_db，过渡用线性斜坡。"""
    env = np.ones(n, dtype=np.float32)
    a = max(1, int(attack_ms / 1000 * sr))
    r = max(1, int(release_ms / 1000 * sr))
    g = db_to_gain(duck_db)
    for s, e in segments:
        s = max(0, min(n, s))
        e = max(0, min(n, e))
        if e <= s:
            continue
        env[s:e] = np.minimum(env[s:e], g)
        # 起音
        s0 = max(0, s - a)
        if s > s0:
            env[s0:s] = np.minimum(env[s0:s], np.linspace(1.0, g, s - s0, dtype=np.float32))
        # 释放
        e1 = min(n, e + r)
        if e1 > e:
            env[e:e1] = np.minimum(env[e:e1], np.linspace(g, 1.0, e1 - e, dtype=np.float32))
    return env
