"""说话人声学画像：基频（F0）+ 频谱质心 → 性别 / 大致年龄段。

目的不是高保真刻画，是给音色替换把关：老年男声不能换成年轻女声。
判据（voiced 帧的 pyin F0，单位 Hz）：
  儿童  > 240
  女声  168 - 240（质心偏低判成熟）
  男声   85 - 168（质心偏低 + F0 抖动大判年长，仅粗分）
灰区 160-172 由频谱质心辅助裁决。
"""

from __future__ import annotations

from . import audio

# (性别, 年龄段) → 描述
AGE_ORDER = ["child", "young", "mature"]


def f0_stats(wav16k_mono) -> tuple[float, float]:
    """返回 (voiced 帧的 F0 均值, F0 标准差)。无 voiced 帧返回 (0, 0)。"""
    import librosa
    import numpy as np

    f0, voiced, _ = librosa.pyin(
        wav16k_mono, fmin=70, fmax=500, sr=16000, frame_length=1024
    )
    v = f0[voiced & ~np.isnan(f0)]
    if len(v) < 10:
        return 0.0, 0.0
    return float(np.mean(v)), float(np.std(v))


def spectral_centroid_hz(wav16k_mono) -> float:
    import numpy as np

    spec = np.abs(np.fft.rfft(wav16k_mono * np.hanning(len(wav16k_mono))))
    freqs = np.fft.rfftfreq(len(wav16k_mono), 1 / 16000)
    if spec.sum() < 1e-9:
        return 0.0
    return float((freqs * spec).sum() / spec.sum())


def classify(f0: float, f0std: float, centroid: float) -> tuple[str, str]:
    """(gender, age)。gender: f/m/x（x=不确定）。age: child/young/mature。"""
    if f0 == 0.0:
        return "x", "young"
    if f0 > 240:
        return "f", "child"
    if f0 > 172:
        gender = "f"
    elif f0 < 160:
        gender = "m"
    else:  # 灰区：质心高偏女
        gender = "f" if centroid > 1800 else "m"
    # 年龄粗分：女声 F0 偏低 + 质心低 → 成熟；男声 F0 抖动大 + 质心低 → 年长
    if gender == "f":
        age = "mature" if f0 < 190 and centroid < 1500 else "young"
    else:
        age = "mature" if (f0std > 30 or centroid < 1100) else "young"
    return gender, age


def speaker_profiles(vocals_path: str, segments: list) -> dict[int, dict]:
    """每个 speaker 一份画像 {spk: {"f0":…, "f0std":…, "centroid":…, "gender":…, "age":…}}。
    用该说话人所有段的去静音拼接，比单段稳。"""
    import numpy as np

    data, sr = audio.load(vocals_path, sr=16000, mono=True)
    by_spk: dict[int, list] = {}
    for s in segments:
        by_spk.setdefault(s.speaker, []).append(data[int(s.start * sr):int(s.end * sr), 0])
    out = {}
    for spk, chunks in by_spk.items():
        wav = np.concatenate(chunks)
        # 去首尾静音，避免把间奏算进质心
        rms = np.sqrt(np.convolve(wav**2, np.ones(1600) / 1600, "same"))
        if rms.max() > 1e-6:
            keep = rms > rms.max() * 0.1
            wav = wav[keep] if keep.sum() > sr else wav
        f0, f0std = f0_stats(wav)
        cen = spectral_centroid_hz(wav)
        g, a = classify(f0, f0std, cen)
        out[spk] = {"f0": round(f0, 1), "f0std": round(f0std, 1),
                    "centroid": round(cen), "gender": g, "age": a}
        print(f"    [profile] spk{spk}: F0={f0:.0f}Hz ±{f0std:.0f} 质心={cen:.0f}Hz → {g}/{a}")
    return out
