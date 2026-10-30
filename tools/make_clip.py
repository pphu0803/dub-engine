#!/usr/bin/env python3
"""make_clip.py — 从任意媒体里切一段做测试素材（用 ffmpeg 快速 seek）。

用法：
  python tools/make_clip.py --in data/xxx.m4s --out data/clip120.wav --start 30 --seconds 120
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", default="data/clip.wav")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--seconds", type=float, default=120.0)
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(os.path.abspath(args.dst)) or ".", exist_ok=True)
    cmd = [
        ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(args.start), "-t", str(args.seconds),
        "-i", args.src, "-vn", "-ac", "2", "-ar", "44100", args.dst,
    ]
    print(" ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("ffmpeg 失败", file=sys.stderr)
        return r.returncode
    size = os.path.getsize(args.dst) / 1e6
    print(f"ok -> {args.dst}  {size:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
