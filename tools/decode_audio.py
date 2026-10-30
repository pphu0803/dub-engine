#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decode_audio.py — 把任意媒体容器解成 ASR/TTS 要的 PCM

不依赖系统 ffmpeg：PyAV 自带解码器。产出 16 kHz 单声道 wav（喂 ASR）
或 24 kHz 单声道 wav（喂 TTS 参考），顺带报电平和静音比例，
避免"跑完了才发现下到的是纯静音"。

用法：
  python tools/decode_audio.py --in data/xxx.m4s
  python tools/decode_audio.py --in data/xxx.m4s --rate 24000 --out data/ref.wav
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import wave

import av


def decode(src: str, dst: str, rate: int, channels: int) -> dict:
    info = {"src": src, "dst": dst, "rate": rate, "duration_s": 0.0,
            "frames": 0, "rms_dbfs": float("-inf"), "peak_dbfs": float("-inf"),
            "silence_ratio": 0.0, "codec": "", "src_rate": 0, "src_channels": 0}

    container = av.open(src)
    if not container.streams.audio:
        raise RuntimeError("文件里没有音频流")
    stream = container.streams.audio[0]
    info["codec"] = stream.codec_context.name
    info["src_rate"] = stream.codec_context.sample_rate
    info["src_channels"] = stream.codec_context.channels

    resampler = av.AudioResampler(format="s16", layout="mono" if channels == 1 else "stereo", rate=rate)
    out = av.open(dst, "w")
    ost = out.add_stream("pcm_s16le", rate=rate)
    ost.layout = "mono" if channels == 1 else "stereo"

    total_samples = 0
    sum_sq = 0.0
    peak = 0
    silent_frames = 0
    total_frames = 0

    def handle(frame):
        nonlocal total_samples, sum_sq, peak, silent_frames, total_frames
        if frame is None:
            return
        # s16 单声道是 packed 格式，plane[0] 就是全部样本
        raw = bytes(frame.planes[0])
        n = min(len(raw) // 2, frame.samples)
        if n == 0:
            return
        samples = struct.unpack_from("<%dh" % n, raw, 0)
        total_samples += n
        local_peak = 0
        local_energy = 0.0
        for s in samples:
            a = abs(s)
            if a > local_peak:
                local_peak = a
            local_energy += s * s
        peak = max(peak, local_peak)
        sum_sq += local_energy

        # 按帧判静音（16 kHz 下 1024 点 ≈ 64 ms），比按样本判更贴近 ASR 的感知
        frame_rms = math.sqrt(local_energy / n) / 32768.0
        frame_db = 20 * math.log10(frame_rms) if frame_rms > 0 else -120.0
        total_frames += 1
        if frame_db < -50:
            silent_frames += 1

    for frame in container.decode(stream):
        for f in resampler.resample(frame):
            handle(f)
        for pkt in ost.encode(f):
            out.mux(pkt)
    for f in resampler.resample(None):
        handle(f)

    # flush encoder
    for pkt in ost.encode(None):
        out.mux(pkt)
    out.close()
    container.close()

    frame_count = 0
    with wave.open(dst, "rb") as w:
        frame_count = w.getnframes()
        info["duration_s"] = frame_count / float(w.getframerate())
        info["rate"] = w.getframerate()
        info["channels"] = w.getnchannels()

    if total_samples:
        rms = math.sqrt(sum_sq / total_samples) / 32768.0
        info["rms_dbfs"] = 20 * math.log10(rms) if rms > 0 else float("-inf")
        info["peak_dbfs"] = 20 * math.log10(peak / 32768.0) if peak > 0 else float("-inf")
        info["silence_ratio"] = silent_frames / total_frames if total_frames else 0.0
    info["frames"] = frame_count
    return info


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="媒体文件 → PCM wav")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--out", dest="dst", default="")
    p.add_argument("--rate", type=int, default=16000, choices=[16000, 22050, 24000, 44100, 48000])
    p.add_argument("--channels", type=int, default=1, choices=[1, 2])
    args = p.parse_args(argv)

    if not os.path.exists(args.src):
        print(f"找不到文件: {args.src}", file=sys.stderr)
        return 2

    dst = args.dst or os.path.splitext(args.src)[0] + f"_{args.rate//1000}k.wav"
    info = decode(args.src, dst, args.rate, args.channels)

    print()
    print("=" * 56)
    print(f"源        : {info['src']}")
    print(f"源编码    : {info['codec']} {info['src_rate']} Hz / {info['src_channels']} ch")
    print(f"输出      : {info['dst']}")
    print(f"时长      : {info['duration_s']:.1f} s  ({info['rate']} Hz / {info['channels']} ch)")
    print(f"RMS       : {info['rms_dbfs']:.1f} dBFS")
    print(f"峰值      : {info['peak_dbfs']:.1f} dBFS")
    print(f"静音帧占比 : {info['silence_ratio']*100:.1f}%（帧级，<-50 dBFS）")
    print("=" * 56)
    if info["rms_dbfs"] < -50:
        print("⚠ 电平过低，可能下到的是静音或未解密的流", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
